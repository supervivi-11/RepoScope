from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from statistics import median

from app.agent import validate_evidence
from app.investigation import PythonRepositoryIndex
from app.investigation.errors import InvestigationError

from .contracts import (
    BenchmarkCase,
    BenchmarkGold,
    BenchmarkResult,
    EvaluationSummary,
    SystemMetrics,
)


def _rounded(value: float) -> float:
    return round(value, 6)


def score_benchmark(
    *,
    cases: tuple[BenchmarkCase, ...],
    gold: tuple[BenchmarkGold, ...],
    results: tuple[BenchmarkResult, ...],
    indexes: dict[str, PythonRepositoryIndex],
) -> EvaluationSummary:
    if not cases:
        raise ValueError("at least one benchmark case is required")
    case_by_id = _unique(cases, "case_id", "case")
    gold_by_id = _unique(gold, "case_id", "gold")
    if set(case_by_id) != set(gold_by_id):
        raise ValueError("gold must contain exactly one row for every case")
    systems = tuple(sorted({item.system for item in results}))
    if not systems:
        raise ValueError("exactly one result per case and system is required")
    grouped: dict[str, dict[str, BenchmarkResult]] = defaultdict(dict)
    for result in results:
        if result.case_id not in case_by_id or result.case_id in grouped[result.system]:
            raise ValueError("exactly one result per case and system is required")
        grouped[result.system][result.case_id] = result
    if any(set(grouped[system]) != set(case_by_id) for system in systems):
        raise ValueError("exactly one result per case and system is required")
    for case_id, case in case_by_id.items():
        index = indexes.get(case_id)
        if index is None or index.snapshot.commit_sha != case.pre_fix_commit_sha:
            raise ValueError("every case requires its matching pre-fix index")
        for path in gold_by_id[case_id].gold_files:
            try:
                index.read_code(path, 1, 1)
            except (InvestigationError, ValueError) as exc:
                raise ValueError("gold file is absent from the pre-fix snapshot") from exc

    aggregates: list[SystemMetrics] = []
    for system in systems:
        recalls: list[float] = []
        reciprocal_ranks: list[float] = []
        emitted = valid = successful = 0
        for case_id in case_by_id:
            result = grouped[system][case_id]
            gold_files = set(gold_by_id[case_id].gold_files)
            if result.status == "failed":
                recalls.append(0.0)
                reciprocal_ranks.append(0.0)
                continue
            successful += 1
            recalls.append(len(gold_files.intersection(result.predicted_files[:5])) / len(gold_files))
            rank = next(
                (index for index, path in enumerate(result.predicted_files, start=1) if path in gold_files),
                None,
            )
            reciprocal_ranks.append(0.0 if rank is None else 1.0 / rank)
            emitted += len(result.citations)
            if result.citations:
                index = indexes.get(case_id)
                if index is None:
                    raise ValueError("citation scoring requires the matching pre-fix index")
                valid += len(validate_evidence(index, result.citations).valid)
        hallucinated = emitted - valid
        system_results = tuple(grouped[system][case_id] for case_id in case_by_id)
        latencies = tuple(item.usage.latency_ms for item in system_results)
        input_tokens = tuple(item.usage.input_tokens for item in system_results)
        output_tokens = tuple(item.usage.output_tokens for item in system_results)
        costs = tuple(item.usage.estimated_cost_usd for item in system_results)
        aggregates.append(
            SystemMetrics(
                system=system,
                case_count=len(cases),
                successful_cases=successful,
                file_recall_at_5=_rounded(sum(recalls) / len(recalls)),
                mrr=_rounded(sum(reciprocal_ranks) / len(reciprocal_ranks)),
                citations_emitted=emitted,
                citations_valid=valid,
                hallucinated_citations=hallucinated,
                citation_validity=None if emitted == 0 else _rounded(valid / emitted),
                citation_hallucination_rate=None if emitted == 0 else _rounded(hallucinated / emitted),
                median_latency_ms=(
                    float(median(latencies)) if all(item is not None for item in latencies) else None
                ),
                total_input_tokens=(
                    sum(item for item in input_tokens if item is not None)
                    if all(item is not None for item in input_tokens)
                    else None
                ),
                total_output_tokens=(
                    sum(item for item in output_tokens if item is not None)
                    if all(item is not None for item in output_tokens)
                    else None
                ),
                estimated_cost_usd=(
                    sum((item for item in costs if item is not None), Decimal("0"))
                    if all(item is not None for item in costs)
                    else None
                ),
            )
        )
    by_system = {item.system: item for item in aggregates}
    paired = "issue_only" in by_system and "reposcope" in by_system
    return EvaluationSummary(
        schema_version="reposcope.eval.summary.v1",
        case_count=len(cases),
        systems=tuple(aggregates),
        file_recall_at_5_delta=(
            _rounded(by_system["reposcope"].file_recall_at_5 - by_system["issue_only"].file_recall_at_5)
            if paired
            else None
        ),
        mrr_delta=(
            _rounded(by_system["reposcope"].mrr - by_system["issue_only"].mrr)
            if paired
            else None
        ),
    )


def _unique(rows: tuple[object, ...], field: str, label: str) -> dict[str, object]:
    mapped = {str(getattr(item, field)): item for item in rows}
    if len(mapped) != len(rows):
        raise ValueError(f"{label} rows must be unique")
    return mapped
