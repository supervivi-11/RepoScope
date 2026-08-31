from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.agent import (
    AnalysisEvent,
    AnalysisPhase,
    AnalysisReport,
    AnalysisStatus,
    EvidenceCitation,
    EvidenceCitationSummary,
    FeedbackCommand,
    Hypothesis,
    InvestigationBudget,
    ToolRequest,
)
from app.report_limits import (
    EVIDENCE_EXCERPT_MAX_BYTES,
    EVIDENCE_EXCERPT_MAX_CHARS,
    HYPOTHESIS_EVIDENCE_REFS_MAX,
    REPORT_REQUIRED_TEXT_MAX_CHARS,
    REPORT_SERIALIZED_MAX_BYTES,
)


SHA = "a" * 40


def _citation(**overrides: object) -> EvidenceCitation:
    values: dict[str, object] = {
        "commit_sha": SHA,
        "path": "src/parser.py",
        "start_line": 1,
        "end_line": 2,
        "excerpt": "def parse(value):\n    return value\n",
        "explanation": "The parser returns the unvalidated value.",
    }
    values.update(overrides)
    return EvidenceCitation(**values)


def test_agent_contracts_are_frozen_extra_forbidding_and_validate_ranges() -> None:
    """Breaks if persisted report records can drift or accept ambiguous evidence."""
    citation = _citation()

    with pytest.raises(ValidationError):
        citation.path = "other.py"
    with pytest.raises(ValidationError):
        EvidenceCitation(**{**citation.model_dump(), "unknown": True})
    with pytest.raises(ValidationError):
        _citation(start_line=3, end_line=2)
    with pytest.raises(ValidationError):
        Hypothesis(
            statement="Cause",
            confidence=1.01,
            evidence=(citation,),
        )


def test_budget_defaults_and_v1_maxima_cannot_be_exceeded() -> None:
    """Breaks if callers or a model can expand the v1 investigation limits."""
    assert InvestigationBudget().model_dump() == {
        "max_tool_calls": 12,
        "max_evidence_rounds": 2,
        "model_retries": 2,
        "max_user_revisions": 1,
    }

    for field, value in (
        ("max_tool_calls", 13),
        ("max_evidence_rounds", 3),
        ("model_retries", 3),
        ("max_user_revisions", 2),
    ):
        with pytest.raises(ValidationError):
            InvestigationBudget(**{field: value})


def test_feedback_revision_requires_nonblank_text_and_accept_rejects_text() -> None:
    """Breaks if ambiguous feedback can enter the one-revision workflow."""
    assert FeedbackCommand(action="accept").text is None
    assert FeedbackCommand(action="revise", text="Check the async path.").text == (
        "Check the async path."
    )

    with pytest.raises(ValidationError):
        FeedbackCommand(action="revise", text="   ")
    with pytest.raises(ValidationError):
        FeedbackCommand(action="accept", text="unexpected")


def test_events_expose_only_observable_safe_fields() -> None:
    """Breaks if event schema gains prompts, credentials, or hidden reasoning fields."""
    event = AnalysisEvent(
        sequence=1,
        phase=AnalysisPhase.INVESTIGATING,
        status=AnalysisStatus.INVESTIGATING,
        kind="tool_failed",
        tool_name="read_code",
        safe_error="Tool execution failed safely.",
        tool_calls=1,
        evidence_rounds=1,
        model_attempts=2,
    )

    assert set(event.model_dump()) == {
        "sequence",
        "phase",
        "status",
        "kind",
        "tool_name",
        "citation",
        "safe_error",
        "tool_calls",
        "evidence_rounds",
        "model_attempts",
    }
    assert "prompt" not in event.model_dump_json()
    assert "reasoning" not in event.model_dump_json()


def test_report_requires_confidence_in_closed_unit_interval() -> None:
    """Breaks if confidence values cannot be consumed as calibrated probabilities."""
    report = AnalysisReport(
        outcome="root_cause_identified",
        issue_summary="Parser accepts an unsupported value.",
        observed_behavior="The value is returned unchanged.",
        expected_behavior="The value should be validated.",
        primary_hypothesis=Hypothesis(
            statement="Validation is missing.",
            confidence=0.8,
            evidence=(_citation().summary(),),
        ),
        evidence=(_citation(),),
        confidence=0.8,
    )
    assert report.confidence == 0.8

    with pytest.raises(ValidationError):
        AnalysisReport(**{**report.model_dump(), "confidence": -0.01})


def test_report_contract_uses_compact_evidence_refs_and_tight_text_limits() -> None:
    """Breaks if runtime reports drift from the public bounded response contract."""
    citation = _citation(
        excerpt="😀" * EVIDENCE_EXCERPT_MAX_CHARS,
        explanation="Evidence reaches the exact public boundary.",
    )
    assert len(citation.excerpt) == EVIDENCE_EXCERPT_MAX_CHARS
    assert len(citation.excerpt.encode("utf-8")) == EVIDENCE_EXCERPT_MAX_BYTES

    hypothesis = Hypothesis(
        statement="x" * 2_000,
        confidence=0.7,
        evidence=(citation.summary(),) * HYPOTHESIS_EVIDENCE_REFS_MAX,
    )
    report = AnalysisReport(
        outcome="root_cause_identified",
        issue_summary="s" * REPORT_REQUIRED_TEXT_MAX_CHARS,
        observed_behavior="Observed",
        expected_behavior="Expected",
        primary_hypothesis=hypothesis,
        evidence=(citation,),
        confidence=0.7,
    )

    assert isinstance(report.primary_hypothesis.evidence[0], EvidenceCitationSummary)

    with pytest.raises(ValidationError):
        _citation(excerpt="😀" * (EVIDENCE_EXCERPT_MAX_CHARS + 1))
    with pytest.raises(ValidationError):
        Hypothesis(
            statement="x" * 2_001,
            confidence=0.7,
            evidence=(citation.summary(),),
        )
    with pytest.raises(ValidationError):
        Hypothesis(
            statement="Within bound",
            confidence=0.7,
            evidence=(citation.summary(),) * (HYPOTHESIS_EVIDENCE_REFS_MAX + 1),
        )
    with pytest.raises(ValidationError):
        Hypothesis(
            statement="Full citations are no longer duplicated under hypotheses.",
            confidence=0.7,
            evidence=(citation.model_dump(),),
        )
    with pytest.raises(ValidationError):
        AnalysisReport(
            **{**report.model_dump(), "issue_summary": "s" * (REPORT_REQUIRED_TEXT_MAX_CHARS + 1)}
        )


def test_serialized_report_cap_counts_actual_json_bytes_without_giant_allocations() -> None:
    """Breaks if escaped strings can exceed the finite persistence/API report cap."""
    citation = _citation(
        excerpt="\\" * 7_000,
        explanation="Large but still bounded citation text.",
    )
    accepted = AnalysisReport(
        outcome="root_cause_identified",
        issue_summary="Escaped evidence remains under the serialized report cap.",
        observed_behavior="Observed",
        expected_behavior="Expected",
        primary_hypothesis=Hypothesis(
            statement="Evidence is referenced compactly.",
            confidence=0.8,
            evidence=(citation.summary(),),
        ),
        evidence=(citation,) * 64,
        confidence=0.8,
    )
    assert len(accepted.model_dump_json().encode("utf-8")) < REPORT_SERIALIZED_MAX_BYTES

    oversized = _citation(
        excerpt="\\" * EVIDENCE_EXCERPT_MAX_CHARS,
        explanation="Escaping each backslash must count against the JSON byte cap.",
    )
    with pytest.raises(ValidationError):
        AnalysisReport(
            outcome="root_cause_identified",
            issue_summary="Escaped evidence exceeds the serialized report cap.",
            observed_behavior="Observed",
            expected_behavior="Expected",
            primary_hypothesis=Hypothesis(
                statement="Evidence is referenced compactly.",
                confidence=0.8,
                evidence=(oversized.summary(),),
            ),
            evidence=(oversized,) * 64,
            confidence=0.8,
        )


def test_evidence_paths_share_the_task_three_normalizer() -> None:
    """Breaks if citation identity and tool path checks diverge."""
    assert _citation(path="src\\parser.py").path == "src/parser.py"
    for path in ("C:/private.py", " src/parser.py", "src/../parser.py", "src//parser.py"):
        with pytest.raises(ValidationError):
            _citation(path=path)


def test_tool_request_arguments_are_deeply_immutable_and_json_checkpoint_safe() -> None:
    """Breaks if a model request can mutate after checkpointing or carry arbitrary objects."""
    raw_arguments = {"path": "src/parser.py", "start_line": 1, "end_line": 2}
    request = ToolRequest(tool_name="read_code", arguments=raw_arguments)
    raw_arguments["path"] = "src/secret.py"

    assert request.arguments_dict() == {
        "end_line": 2,
        "path": "src/parser.py",
        "start_line": 1,
    }
    with pytest.raises((TypeError, ValidationError)):
        request.arguments[0].value = "mutated"
    assert ToolRequest.model_validate_json(request.model_dump_json()) == request

    with pytest.raises(ValidationError):
        ToolRequest(
            tool_name="read_code",
            arguments=(
                {"key": "path", "value": "src/parser.py"},
                {"key": "path", "value": "src/other.py"},
            ),
        )
    with pytest.raises(ValidationError):
        ToolRequest(tool_name="read_code", arguments={"path": {"nested": True}})
    with pytest.raises(ValidationError):
        ToolRequest(tool_name="read_code", arguments={"path": object()})
