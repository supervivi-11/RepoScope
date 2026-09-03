"""Closed-schema, write-only-to-model diagnostics for serial frozen evaluations."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Annotated, Literal, get_args

from pydantic import Field, model_validator

from app.agent import AnalysisState, ALLOWED_TOOL_NAMES
from .contracts import BenchmarkCase, BenchmarkResult
from .jsonl import read_jsonl, write_jsonl
from .real_contracts import RealEvaluationModel
from .errors import EvaluationRunAbort

DIAGNOSTIC_FILENAME = "diagnostics.v1.jsonl"
Node = Literal["understand_issue", "begin_round", "select_tool", "execute_tool",
               "critique_evidence", "compose_report", "validate_report", "prepare_review"]
Event = Literal["issue_understood", "evidence_round_started", "investigation_completed",
                "tool_succeeded", "tool_failed", "tool_budget_exhausted", "evidence_sufficient",
                "evidence_insufficient", "round_budget_exhausted", "report_composed",
                "report_evidence_bound", "report_downgraded", "citation_rejected",
                "citation_validated", "review_ready", "model_failed", "unknown"]
Tool = Literal["get_repository_map", "search_code", "read_code", "find_symbol",
               "find_references", "get_recent_commits", "get_related_issues", "unknown"]
Rejection = Literal["commit_mismatch", "source_mismatch", "excerpt_mismatch"]
Reason = Literal["supported_primary", "model_report_insufficient", "primary_absent",
                 "primary_references_absent", "primary_unvalidated"]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class DiagnosticWriteAbort(EvaluationRunAbort):
    pass


class DiagnosticStep(RealEvaluationModel):
    sequence: int = Field(ge=1, le=64)
    node: Node
    tool_calls: int = Field(ge=0, le=12)
    evidence_rounds: int = Field(ge=0, le=2)
    model_attempts: int = Field(ge=0, le=128)
    events: tuple[Event, ...] = Field(default=(), max_length=130)
    tool: Tool | None = None
    tool_succeeded: bool | None = None
    tool_citation_count: int = Field(default=0, ge=0, le=32)
    evidence_before: int = Field(ge=0, le=64)
    evidence_after: int = Field(ge=0, le=64)
    hypothesis_count: int = Field(ge=0, le=8)
    hypothesis_reference_count: int = Field(ge=0)
    hypothesis_tool_matches: int = Field(ge=0)
    critique_sufficient: bool | None = None
    report_outcome: Literal["root_cause_identified", "insufficient_evidence"] | None = None
    primary_reference_ids: tuple[Digest, ...] = Field(default=(), max_length=64)
    report_evidence_ids: tuple[Digest, ...] = Field(default=(), max_length=64)
    impacted_file_count: int = Field(default=0, ge=0, le=64)
    bound_count: int = Field(default=0, ge=0, le=64)
    valid_count: int = Field(default=0, ge=0, le=64)
    rejection_codes: tuple[Rejection, ...] = Field(default=(), max_length=64)
    report_reason: Reason | None = None


class CaseDiagnostic(RealEvaluationModel):
    schema_version: Literal["reposcope.eval.diagnostic.v1"] = "reposcope.eval.diagnostic.v1"
    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,99}$")
    dataset_digest: Digest
    commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    status: Literal["in_progress", "complete", "aborted"] = "in_progress"
    error_code: Literal["analysis_aborted"] | None = None
    steps: tuple[DiagnosticStep, ...] = Field(default=(), max_length=64)

    @model_validator(mode="after")
    def valid_prefix(self):
        if tuple(step.sequence for step in self.steps) != tuple(range(1, len(self.steps) + 1)):
            raise ValueError("diagnostic sequence is not contiguous")
        previous = (0, 0, 0)
        previous_node = None
        evidence_after = 0
        allowed_next = {
            None: {"understand_issue"}, "understand_issue": {"begin_round"},
            "begin_round": {"select_tool"}, "select_tool": {"execute_tool", "critique_evidence"},
            "execute_tool": {"select_tool", "critique_evidence"},
            "critique_evidence": {"begin_round", "compose_report"},
            "compose_report": {"validate_report"}, "validate_report": {"prepare_review"},
            "prepare_review": set(),
        }
        for step in self.steps:
            if step.node not in allowed_next[previous_node] or step.evidence_before != evidence_after:
                raise ValueError("diagnostic node order or evidence continuity mismatch")
            current = (step.tool_calls, step.evidence_rounds, step.model_attempts)
            if any(after < before for before, after in zip(previous, current)):
                raise ValueError("diagnostic counters regressed")
            if (current[0] - previous[0] != int(step.node == "execute_tool")
                or current[1] - previous[1] != int(step.node == "begin_round")):
                raise ValueError("diagnostic budget counters do not match nodes")
            previous = current
            previous_node = step.node
            evidence_after = step.evidence_after
        if (self.status == "aborted") != (self.error_code is not None):
            raise ValueError("diagnostic abort requires a fixed error code")
        if self.status == "complete" and (
            not self.steps or self.steps[0].node != "understand_issue"
            or self.steps[-1].node != "prepare_review"
            or sum(step.node == "validate_report" for step in self.steps) != 1
            or sum(step.node == "compose_report" for step in self.steps) != 1
            or self.steps[-1].report_outcome is None
        ):
            raise ValueError("diagnostic case is incomplete")
        return self


def _reference_id(reference) -> str:
    # Hash only locations, never excerpts or model prose. Not anonymization.
    value = (reference.commit_sha, reference.path, reference.start_line, reference.end_line)
    return hashlib.sha256(json.dumps(value, separators=(",", ":")).encode()).hexdigest()


def project_step(node: Node, before: AnalysisState, update: dict, details: dict, *, sequence: int) -> DiagnosticStep:
    after = before.model_copy(update=update)
    tool = after.tool_history[-1] if node == "execute_tool" and after.tool_history else None
    tool_refs = {_reference_id(c) for result in after.tool_history if result.succeeded for c in result.citations}
    hypothesis_refs = [c for h in after.hypotheses for c in h.evidence]
    report = after.report
    reason = None
    if node == "validate_report":
        original = before.report
        if original.outcome == "insufficient_evidence":
            reason = "model_report_insufficient"
        elif original.primary_hypothesis is None:
            reason = "primary_absent"
        elif not original.primary_hypothesis.evidence:
            reason = "primary_references_absent"
        elif report.primary_hypothesis is None:
            reason = "primary_unvalidated"
        else:
            reason = "supported_primary"
    return DiagnosticStep(
        sequence=sequence, node=node,
        tool_calls=after.counters.tool_calls, evidence_rounds=after.counters.evidence_rounds,
        model_attempts=after.counters.model_attempts,
        events=tuple(e.kind if e.kind in get_args(Event) else "unknown" for e in after.events[len(before.events):]),
        tool=(tool.tool_name if tool.tool_name in ALLOWED_TOOL_NAMES else "unknown") if tool else None,
        tool_succeeded=tool.succeeded if tool else None,
        tool_citation_count=len(tool.citations) if tool else 0,
        evidence_before=len(before.evidence), evidence_after=len(after.evidence),
        hypothesis_count=len(after.hypotheses), hypothesis_reference_count=len(hypothesis_refs),
        hypothesis_tool_matches=sum(_reference_id(c) in tool_refs for c in hypothesis_refs),
        critique_sufficient=after.critique_sufficient if node == "critique_evidence" else None,
        report_outcome=report.outcome if report else None,
        primary_reference_ids=tuple(_reference_id(c) for c in report.primary_hypothesis.evidence) if report and report.primary_hypothesis else (),
        report_evidence_ids=tuple(_reference_id(c) for c in report.evidence) if report else (),
        impacted_file_count=len(report.impacted_files) if report else 0,
        bound_count=details.get("bound_count", 0), valid_count=details.get("valid_count", 0),
        rejection_codes=details.get("rejection_codes", ()), report_reason=reason,
    )


class DiagnosticJournal:
    """One writer, no resume. Completed node functions are not checkpoint commits."""
    def __init__(self, path: Path, *, dataset_digest: str) -> None:
        self.path = path.absolute()
        if self.path.exists() or self.path.is_symlink():
            raise ValueError("diagnostic journal must be new")
        self.dataset_digest = dataset_digest
        self._rows: dict[str, CaseDiagnostic] = {}
        write_jsonl(self.path, ())  # Preflight disk availability before model calls.

    def _save(self, row: CaseDiagnostic) -> None:
        try:
            row = CaseDiagnostic.model_validate(row.model_dump())
            rows = {**self._rows, row.case_id: row}
            if len(rows) > 6 or self.path.is_symlink():
                raise ValueError("diagnostic journal boundary exceeded")
            write_jsonl(self.path, tuple(rows.values()))
        except (ValueError, OSError) as exc:
            raise DiagnosticWriteAbort("Diagnostic persistence failed safely.") from exc
        self._rows = rows

    def start(self, case: BenchmarkCase):
        if case.split != "development" or case.case_id in self._rows:
            raise ValueError("diagnostics require a new development case")
        self._save(CaseDiagnostic(case_id=case.case_id, dataset_digest=self.dataset_digest, commit_sha=case.pre_fix_commit_sha))

        def observer(node, before, update, details):
            row = self._rows[case.case_id]
            if row.status != "in_progress":
                raise ValueError("diagnostic case is already terminal")
            try:
                step = project_step(node, before, update, details, sequence=len(row.steps) + 1)
            except (ValueError, TypeError, AttributeError) as exc:
                raise DiagnosticWriteAbort("Diagnostic projection failed safely.") from exc
            self._save(row.model_copy(update={"steps": (*row.steps, step)}))
        return observer

    def finish(self, case_id: str, *, aborted: bool = False) -> None:
        row = self._rows[case_id]
        self._save(row.model_copy(update={"status": "aborted" if aborted else "complete", "error_code": "analysis_aborted" if aborted else None}))


def validate_diagnostic_artifact(path: Path, *, results: tuple[BenchmarkResult, ...], dataset_digest: str, cases: tuple[BenchmarkCase, ...] | None = None) -> None:
    if path.is_symlink():
        raise ValueError("diagnostic artifact must not be a symlink")
    rows = read_jsonl(path, CaseDiagnostic)
    by_case = {row.case_id: row for row in rows}
    if set(by_case) != {result.case_id for result in results} or len(rows) != 6:
        raise ValueError("diagnostics must cover the six predictions")
    for result in results:
        row = by_case[result.case_id]
        if row.status != "complete" or row.dataset_digest != dataset_digest:
            raise ValueError("diagnostic completion or provenance mismatch")
        final = row.steps[-1]
        if (final.report_outcome != result.report_outcome
            or final.tool_calls != result.usage.tool_calls
            or final.model_attempts != result.usage.model_attempts
            or final.report_evidence_ids != tuple(_reference_id(c) for c in result.citations)):
            raise ValueError("diagnostics disagree with final prediction")
    if cases is not None and any(by_case[case.case_id].commit_sha != case.pre_fix_commit_sha for case in cases):
        raise ValueError("diagnostic snapshot mismatch")


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Inspect diagnostic counters only; no model or gold is used.")
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    try:
        rows = read_jsonl(args.path, CaseDiagnostic)
    except ValueError:
        parser.exit(2, "Diagnostic input failed validation.\n")
    for row in rows:
        reasons = [step.report_reason for step in row.steps if step.report_reason]
        print(json.dumps({"case_id": row.case_id, "status": row.status, "completed_nodes": len(row.steps), "report_reasons": reasons}, sort_keys=True))


if __name__ == "__main__":
    main()
