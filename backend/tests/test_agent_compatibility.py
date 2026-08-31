from __future__ import annotations

from app.agent import (
    AnalysisReport,
    AnalysisState,
    EvidenceCitation,
    Hypothesis,
    IssueIdentity,
    RepositoryIdentity,
)
from app.agent.compatibility import (
    load_persisted_analysis_state,
    load_persisted_report,
)
from app.analysis.checkpoints import build_checkpoint_serializer


SHA = "a" * 40


def _citation() -> EvidenceCitation:
    return EvidenceCitation(
        commit_sha=SHA,
        path="src/parser.py",
        start_line=1,
        end_line=1,
        excerpt="return value\n",
        explanation="The parser returns the value.",
    )


def _report() -> AnalysisReport:
    citation = _citation()
    return AnalysisReport(
        outcome="root_cause_identified",
        issue_summary="Parser regression.",
        observed_behavior="The value is returned.",
        expected_behavior="The value should be rejected.",
        primary_hypothesis=Hypothesis(
            statement="Validation is missing.",
            confidence=0.8,
            evidence=(citation.summary(),),
        ),
        evidence=(citation,),
        confidence=0.8,
    )


def _legacy_report_payload() -> dict[str, object]:
    report = _report()
    payload = report.model_dump(mode="json")
    payload["primary_hypothesis"]["evidence"] = [
        report.evidence[0].model_dump(mode="json")
    ]
    return payload


def test_legacy_full_hypothesis_citations_upgrade_to_compact_references() -> None:
    """Breaks if reports persisted before the compact-ref contract return 500."""
    loaded = load_persisted_report(_legacy_report_payload())

    assert loaded.outcome == "root_cause_identified"
    assert loaded.primary_hypothesis is not None
    assert loaded.primary_hypothesis.evidence == (_citation().summary(),)


def test_legacy_report_outside_new_size_limits_downgrades_safely() -> None:
    """Breaks if old valid text limits make upgraded rows unreadable."""
    payload = _legacy_report_payload()
    payload["issue_summary"] = "x" * 4_001

    loaded = load_persisted_report(payload)

    assert loaded.outcome == "insufficient_evidence"
    assert loaded.primary_hypothesis is None
    assert loaded.evidence == ()
    assert "legacy" in loaded.uncertainties[0].casefold()


def test_legacy_checkpoint_reports_and_hypotheses_upgrade_before_recovery() -> None:
    """Breaks if revision recovery cannot parse a pre-upgrade checkpoint."""
    report = _report()
    state = AnalysisState(
        analysis_id="analysis-legacy",
        repository=RepositoryIdentity(
            owner="owner", repository="repo", commit_sha=SHA
        ),
        issue=IssueIdentity(
            number=1,
            title="Parser regression",
            body="The parser returns an invalid value.",
            html_url="https://github.com/owner/repo/issues/1",
        ),
        hypotheses=(report.primary_hypothesis,),
        evidence=report.evidence,
        report=report,
        original_report=report,
        report_history=(report,),
    )
    payload = state.model_dump(mode="json")
    full = report.evidence[0].model_dump(mode="json")
    payload["hypotheses"][0]["evidence"] = [full]
    payload["report"] = _legacy_report_payload()
    payload["original_report"] = _legacy_report_payload()
    payload["report_history"] = [_legacy_report_payload()]

    loaded = load_persisted_analysis_state(payload)

    assert loaded.hypotheses[0].evidence == (_citation().summary(),)
    assert loaded.report is not None
    assert loaded.report.primary_hypothesis is not None
    assert loaded.report.primary_hypothesis.evidence == (_citation().summary(),)


def test_model_construct_checkpoint_round_trip_cannot_bypass_legacy_limits() -> None:
    """Breaks if LangGraph-restored BaseModel instances skip compatibility checks."""
    report = _report()
    report_values = {
        field: getattr(report, field) for field in AnalysisReport.model_fields
    }
    report_values["issue_summary"] = "x" * 4_001
    legacy_report = AnalysisReport.model_construct(**report_values)
    valid_state = AnalysisState(
        analysis_id="analysis-constructed",
        repository=RepositoryIdentity(
            owner="owner", repository="repo", commit_sha=SHA
        ),
        issue=IssueIdentity(
            number=2,
            title="Parser regression",
            body="The parser returns an invalid value.",
            html_url="https://github.com/owner/repo/issues/2",
        ),
    )
    state_values = {
        field: getattr(valid_state, field) for field in AnalysisState.model_fields
    }
    state_values["report"] = legacy_report
    serializer = build_checkpoint_serializer()
    restored = serializer.loads_typed(serializer.dumps_typed(state_values))

    assert isinstance(restored, dict)

    loaded = load_persisted_analysis_state(restored)

    assert loaded.report is not None
    assert loaded.report.outcome == "insufficient_evidence"
    assert len(loaded.report.issue_summary) <= 4_000
