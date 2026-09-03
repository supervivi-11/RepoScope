from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, Literal

from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field, field_validator

from app.agent import (
    AnalysisReport,
    AnalysisState,
    InvestigationBudget,
    IssueIdentity,
    ModelGateway,
    ModelPhase,
    build_analysis_state,
    build_investigation_graph,
    invoke_structured,
)
from app.ingestion import RepositoryCoordinates, RepositorySnapshot
from app.investigation import InvestigationTools, PythonRepositoryIndex
from app.investigation.index import _normalize_relative_path

from .contracts import EvaluationModel, EvaluationPrediction, EvaluationUsage
from .deepseek_provider import InMemoryCallLedger
from .real_contracts import DeepSeekRunConfig, ProviderCallUsage
from .runners import IssueOnlyInput


class IssueOnlyModelOutput(EvaluationModel):
    predicted_files: tuple[str, ...] = Field(default=(), max_length=20)
    report_outcome: Literal["root_cause_identified", "insufficient_evidence"]

    @field_validator("predicted_files")
    @classmethod
    def _ranked_normalized_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(_normalize_relative_path(item) for item in value)
        if normalized != value or len(set(normalized)) != len(normalized):
            raise ValueError("predicted files must be normalized and unique")
        return value

class RealIssueOnlyPredictor:
    def __init__(
        self,
        *,
        model: ModelGateway,
        configuration: DeepSeekRunConfig,
        ledger: InMemoryCallLedger,
    ) -> None:
        self._model = model
        self._configuration = configuration
        self._ledger = ledger

    async def __call__(self, value: IssueOnlyInput) -> EvaluationPrediction:
        start_index = len(self._ledger.records)
        started = perf_counter()
        call = await invoke_structured(
            self._model,
            phase=ModelPhase.ISSUE_ONLY_PREDICTION,
            response_model=IssueOnlyModelOutput,
            context={
                "repository_url": value.repo_url,
                "issue_number": value.issue_number,
                "issue_title": value.issue_title,
                "issue_body": value.issue_body,
                "instruction": (
                    "Rank at most 20 likely Python production files using only the issue."
                ),
            },
            retries=self._configuration.model_retries,
        )
        output = IssueOnlyModelOutput.model_validate(call.value)
        records = self._ledger.records[start_index:]
        usage = _aggregate_usage(
            records,
            latency_ms=max(0, round((perf_counter() - started) * 1000)),
            tool_calls=0,
            model_attempts=call.attempts,
            rate_card_version=self._configuration.rate_card_version,
        )
        return EvaluationPrediction(
            predicted_files=output.predicted_files,
            citations=(),
            report_outcome=output.report_outcome,
            model_id=_model_id(records),
            usage=usage,
        )


class _OfflineEvaluationHistory:
    """No mutable GitHub history is available in a frozen-source benchmark."""

    async def fetch_recent_commits(self, *args: Any, **kwargs: Any) -> None:
        raise ValueError("Live history is unavailable during frozen evaluation.")

    async def fetch_related_issues(self, *args: Any, **kwargs: Any) -> None:
        raise ValueError("Live history is unavailable during frozen evaluation.")


class RealRepoScopeAnalyzer:
    def __init__(
        self,
        *,
        model: ModelGateway,
        configuration: DeepSeekRunConfig,
        ledger: InMemoryCallLedger,
        github: Any,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._model = model
        self._configuration = configuration
        self._ledger = ledger
        # Keep constructor compatibility, but do not retain or delegate the live
        # client. A created-at filter cannot freeze later edits to Issue bodies.
        self._github = _OfflineEvaluationHistory()
        self._clock = clock or (lambda: datetime.now(UTC))

    async def __call__(self, case: Any, snapshot_root: Path) -> EvaluationPrediction:
        start_index = len(self._ledger.records)
        started = perf_counter()
        coordinates = RepositoryCoordinates.parse(case.repo_url)
        indexed_bytes = sum(
            path.stat().st_size for path in snapshot_root.rglob("*") if path.is_file()
        )
        snapshot = RepositorySnapshot.create(
            owner=coordinates.owner,
            repository=coordinates.repository,
            commit_sha=case.pre_fix_commit_sha,
            root_path=snapshot_root,
            indexed_byte_count=indexed_bytes,
            created_at=self._clock(),
        )
        index = PythonRepositoryIndex.build(snapshot)
        tools = InvestigationTools(index, self._github)
        budget = InvestigationBudget(
            max_tool_calls=self._configuration.max_tool_calls,
            max_evidence_rounds=self._configuration.max_evidence_rounds,
            model_retries=self._configuration.model_retries,
            max_user_revisions=0,
        )
        graph = build_investigation_graph(
            tools=tools,
            model=self._model,
            budget=budget,
            checkpointer=InMemorySaver(),
        )
        state = build_analysis_state(
            analysis_id=f"eval-{case.case_id}",
            tools=tools,
            issue=IssueIdentity(
                number=case.issue_number,
                title=case.issue_title,
                body=case.issue_body,
                html_url=f"{case.repo_url}/issues/{case.issue_number}",
            ),
        )
        output = await graph.ainvoke(
            state,
            {"configurable": {"thread_id": f"eval-{case.case_id}"}},
        )
        final_state = AnalysisState.model_validate(
            {
                field: output[field]
                for field in AnalysisState.model_fields
                if field in output
            }
        )
        if "Model output could not be completed safely." in final_state.safe_errors:
            raise RuntimeError("RepoScope evaluation encountered a model failure")
        if final_state.report is None:
            raise RuntimeError("RepoScope evaluation completed without a report")
        records = self._ledger.records[start_index:]
        return EvaluationPrediction(
            predicted_files=_rank_report_files(final_state.report),
            citations=final_state.report.evidence,
            report_outcome=final_state.report.outcome,
            model_id=_model_id(records),
            usage=_aggregate_usage(
                records,
                latency_ms=max(0, round((perf_counter() - started) * 1000)),
                tool_calls=final_state.counters.tool_calls,
                model_attempts=final_state.counters.model_attempts,
                rate_card_version=self._configuration.rate_card_version,
            ),
        )


def _rank_report_files(report: AnalysisReport) -> tuple[str, ...]:
    candidates: list[str] = [item.path for item in report.impacted_files]
    if report.primary_hypothesis is not None:
        candidates.extend(item.path for item in report.primary_hypothesis.evidence)
    candidates.extend(item.path for item in report.evidence)
    for hypothesis in report.alternative_hypotheses:
        candidates.extend(item.path for item in hypothesis.evidence)
    return tuple(dict.fromkeys(candidates))[:20]


def _model_id(records: tuple[ProviderCallUsage, ...]) -> str | None:
    successful = tuple(item for item in records if item.status == "success")
    returned = {item.returned_model for item in records if item.returned_model}
    if not successful or len(returned) != 1 or any(
        item.returned_model is None for item in records
    ):
        return None
    return next(iter(returned))


def _aggregate_usage(
    records: tuple[ProviderCallUsage, ...],
    *,
    latency_ms: int,
    tool_calls: int,
    model_attempts: int,
    rate_card_version: str,
) -> EvaluationUsage:
    input_values = tuple(item.input_tokens for item in records)
    output_values = tuple(item.output_tokens for item in records)
    cost_values = tuple(item.estimated_cost_usd for item in records)
    return EvaluationUsage(
        latency_ms=latency_ms,
        input_tokens=(
            sum(value for value in input_values if value is not None)
            if records and all(value is not None for value in input_values)
            else None
        ),
        output_tokens=(
            sum(value for value in output_values if value is not None)
            if records and all(value is not None for value in output_values)
            else None
        ),
        estimated_cost_usd=(
            sum((value for value in cost_values if value is not None), Decimal("0"))
            if records and all(value is not None for value in cost_values)
            else None
        ),
        tool_calls=tool_calls,
        model_attempts=model_attempts,
        rate_card_version=rate_card_version if records else None,
    )
