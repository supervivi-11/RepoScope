from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from decimal import Decimal

from .contracts import BenchmarkResult, EvaluationUsage
from .real_contracts import DeepSeekRunConfig, ProviderCallUsage


_PREFLIGHT_PHASES = (
    "issue_understanding",
    "tool_selection",
    "evidence_critique",
    "report_composition",
    "issue_only_prediction",
)


def validate_complete_prediction_evidence(
    *,
    configuration: DeepSeekRunConfig,
    dataset_digest: str,
    issue_only: tuple[BenchmarkResult, ...],
    reposcope: tuple[BenchmarkResult, ...],
    call_usage: tuple[ProviderCallUsage, ...],
    started_at: datetime,
    finished_at: datetime,
) -> None:
    results = (*issue_only, *reposcope)
    if len(issue_only) != 6 or len(reposcope) != 6:
        raise ValueError("complete predictions require six results per system")
    issue_ids = {item.case_id for item in issue_only}
    repo_ids = {item.case_id for item in reposcope}
    if len(issue_ids) != 6 or issue_ids != repo_ids:
        raise ValueError("complete predictions require the same six cases")
    if any(item.system != "issue_only" for item in issue_only) or any(
        item.system != "reposcope" for item in reposcope
    ):
        raise ValueError("prediction result system does not match its file")
    if any(
        item.status != "ok"
        or item.safe_error_code is not None
        or item.dataset_digest != dataset_digest
        or item.runner_id != configuration.runner_version
        for item in results
    ):
        raise ValueError("only complete successful predictions can be manifested")
    if not call_usage:
        raise ValueError("complete predictions require a non-empty provider ledger")
    if any(
        item.started_at < started_at or item.finished_at > finished_at
        for item in call_usage
    ):
        raise ValueError("provider calls fall outside the manifested UTC window")
    expected_call_ids = tuple(
        f"call-{number:04d}" for number in range(1, len(call_usage) + 1)
    )
    if tuple(item.call_id for item in call_usage) != expected_call_ids:
        raise ValueError("provider call ledger must be complete and ordered")
    if sum(item.total_tokens or 0 for item in call_usage) > configuration.max_total_tokens:
        raise ValueError("provider call ledger exceeds the fixed token budget")
    if any(
        item.requested_model != configuration.requested_model
        or item.input_tokens is None
        or item.output_tokens is None
        or item.returned_model is None
        for item in call_usage
    ):
        raise ValueError("provider call ledger is incomplete or unverifiable")
    _validate_retry_sequences(call_usage, max_retries=configuration.model_retries)
    preflight = tuple(
        item
        for item in call_usage
        if item.system == "preflight" and item.status == "success"
    )
    if (
        len(preflight) != len(_PREFLIGHT_PHASES)
        or tuple(item.phase for item in preflight) != _PREFLIGHT_PHASES
        or any(item.case_id != "schema-preflight" for item in preflight)
    ):
        raise ValueError("provider ledger lacks the fixed successful preflight")
    grouped: dict[tuple[str, str], list[ProviderCallUsage]] = defaultdict(list)
    for item in call_usage:
        if item.system != "preflight":
            grouped[(item.case_id, item.system)].append(item)
    expected_groups = {(item.case_id, item.system) for item in results}
    if set(grouped) != expected_groups:
        raise ValueError("provider ledger does not map exactly to prediction results")
    by_key = {(item.case_id, item.system): item for item in results}
    for key, records in grouped.items():
        result = by_key[key]
        returned_models = {item.returned_model for item in records}
        if len(returned_models) != 1 or result.model_id != next(iter(returned_models)):
            raise ValueError("prediction model provenance does not match call ledger")
        input_tokens = sum(item.input_tokens or 0 for item in records)
        output_tokens = sum(item.output_tokens or 0 for item in records)
        costs = tuple(item.estimated_cost_usd for item in records)
        cost: Decimal | None = (
            sum((item for item in costs if item is not None), Decimal("0"))
            if all(item is not None for item in costs)
            else None
        )
        if (
            result.usage.input_tokens != input_tokens
            or result.usage.output_tokens != output_tokens
            or result.usage.estimated_cost_usd != cost
            or result.usage.model_attempts != len(records)
            or result.usage.rate_card_version != configuration.rate_card_version
            or result.usage.latency_ms is None
            or result.usage.latency_ms < sum(item.latency_ms for item in records)
        ):
            raise ValueError("prediction usage does not match provider call ledger")


def _validate_retry_sequences(
    records: tuple[ProviderCallUsage, ...], *, max_retries: int
) -> None:
    pending: ProviderCallUsage | None = None
    for item in records:
        if item.attempt > max_retries + 1:
            raise ValueError("provider retry sequence exceeds the fixed retry budget")
        if item.attempt == 1:
            if pending is not None:
                raise ValueError("provider retry sequence ended without success")
        elif (
            pending is None
            or item.case_id != pending.case_id
            or item.system != pending.system
            or item.phase != pending.phase
            or item.attempt != pending.attempt + 1
        ):
            raise ValueError("provider retry sequence is not contiguous")

        if item.status == "failed":
            if item.safe_error_code != "schema_error":
                raise ValueError("provider retry sequence contains a non-retryable failure")
            pending = item
        else:
            pending = None
    if pending is not None:
        raise ValueError("provider retry sequence ended without success")


def provider_backend_drift(
    call_usage: tuple[ProviderCallUsage, ...], requested_model: str
) -> bool | None:
    known = tuple(item.returned_model for item in call_usage if item.returned_model)
    fingerprints = {
        item.system_fingerprint
        for item in call_usage
        if item.system_fingerprint is not None
    }
    if any(model != requested_model for model in known) or len(set(known)) > 1:
        return True
    if len(fingerprints) > 1:
        return True
    if len(known) != len(call_usage):
        return None
    return False


def aggregate_provider_usage(
    records: tuple[ProviderCallUsage, ...], *, rate_card_version: str
) -> EvaluationUsage:
    input_values = tuple(item.input_tokens for item in records)
    output_values = tuple(item.output_tokens for item in records)
    costs = tuple(item.estimated_cost_usd for item in records)
    return EvaluationUsage(
        latency_ms=sum(item.latency_ms for item in records),
        input_tokens=(
            sum(item for item in input_values if item is not None)
            if all(item is not None for item in input_values)
            else None
        ),
        output_tokens=(
            sum(item for item in output_values if item is not None)
            if all(item is not None for item in output_values)
            else None
        ),
        estimated_cost_usd=(
            sum((item for item in costs if item is not None), Decimal("0"))
            if all(item is not None for item in costs)
            else None
        ),
        model_attempts=len(records),
        rate_card_version=rate_card_version if records else None,
    )
