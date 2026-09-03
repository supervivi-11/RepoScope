from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.agent import (
    AnalysisReport,
    EvidenceCitation,
    Hypothesis,
    ImpactedFile,
    ProposedTest,
    downgrade_unsubstantiated_report,
    validate_evidence,
)
from app.ingestion import RepositorySnapshot
from app.investigation import PythonRepositoryIndex


SHA = "a" * 40
SOURCE = "def parse(value):\n    return value\n"


@pytest.mark.parametrize("bad", ["unknown", "wrong_sha", "explicit_fabrication", "bad_tool"])
def test_reference_binding_never_repairs_fabricated_or_unseen_evidence(tmp_path: Path, bad: str) -> None:
    from app.agent.validation import bind_tool_evidence

    citation = _citation()
    reference = citation.summary()
    supplied = ()
    available = (citation,)
    if bad == "unknown":
        reference = reference.model_copy(update={"path": "src/unseen.py"})
    elif bad == "wrong_sha":
        reference = reference.model_copy(update={"commit_sha": "b" * 40})
    elif bad == "explicit_fabrication":
        supplied = (citation.model_copy(update={"excerpt": "fabricated\n"}),)
    else:
        available = (citation.model_copy(update={"excerpt": "fabricated\n"}),)
    report = AnalysisReport(
        outcome="root_cause_identified", issue_summary="Bug", observed_behavior="Invalid",
        expected_behavior="Valid", confidence=0.9,
        primary_hypothesis=Hypothesis(statement="Missing check", confidence=0.9, evidence=(reference,)),
        evidence=supplied,
    )
    index = _index(tmp_path)
    bound = bind_tool_evidence(index, report, available)
    sanitized, _ = downgrade_unsubstantiated_report(index, bound)
    assert sanitized.outcome == "insufficient_evidence"
    assert sanitized.evidence == ()


def _index(tmp_path: Path) -> PythonRepositoryIndex:
    root = tmp_path / "snapshot"
    source = root / "src" / "parser.py"
    source.parent.mkdir(parents=True)
    source.write_text(SOURCE, encoding="utf-8", newline="")
    snapshot = RepositorySnapshot.create(
        owner="octo",
        repository="demo",
        commit_sha=SHA,
        root_path=root,
        indexed_byte_count=len(SOURCE.encode()),
        created_at=datetime(2026, 8, 29, tzinfo=UTC),
    )
    return PythonRepositoryIndex.build(snapshot)


def _citation(**overrides: object) -> EvidenceCitation:
    values: dict[str, object] = {
        "commit_sha": SHA,
        "path": "src/parser.py",
        "start_line": 1,
        "end_line": 2,
        "excerpt": SOURCE,
        "explanation": "The function returns the value without validation.",
    }
    values.update(overrides)
    return EvidenceCitation(**values)


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"commit_sha": "b" * 40}, "commit_mismatch"),
        ({"path": "src/other.py"}, "source_mismatch"),
        ({"start_line": 3, "end_line": 3, "excerpt": "missing\n"}, "source_mismatch"),
        ({"excerpt": "def parse(value):\n    return None\n"}, "excerpt_mismatch"),
    ],
)
def test_evidence_validation_rejects_wrong_identity_range_or_excerpt(
    tmp_path: Path, overrides: dict[str, object], code: str
) -> None:
    """Breaks if a citation can drift from the immutable indexed snapshot."""
    result = validate_evidence(_index(tmp_path), (_citation(**overrides),))

    assert result.valid == ()
    assert len(result.invalid) == 1
    assert result.invalid[0].code == code
    assert "Traceback" not in result.invalid[0].safe_error


def test_evidence_validation_accepts_only_an_exact_read_code_match(tmp_path: Path) -> None:
    """Breaks if exact immutable evidence is accidentally filtered or rewritten."""
    citation = _citation()
    result = validate_evidence(_index(tmp_path), (citation,))

    assert result.valid == (citation,)
    assert result.invalid == ()


def test_report_validation_keeps_only_hypothesis_refs_resolved_by_top_level_evidence(
    tmp_path: Path,
) -> None:
    """Breaks if hypotheses can smuggle unresolved full evidence payloads."""
    valid = _citation()
    unresolved = valid.model_copy(update={"start_line": 2, "end_line": 2})
    report = AnalysisReport(
        outcome="root_cause_identified",
        issue_summary="Parser regression.",
        observed_behavior="A value is returned.",
        expected_behavior="It should be rejected.",
        primary_hypothesis=Hypothesis(
            statement="The parser lacks validation.",
            confidence=0.9,
            evidence=(valid.summary(), unresolved.summary()),
        ),
        alternative_hypotheses=(
            Hypothesis(
                statement="A second explanation is possible.",
                confidence=0.5,
                evidence=(unresolved.summary(),),
            ),
        ),
        evidence=(valid,),
        implementation_steps=("Add validation.",),
        confidence=0.9,
    )

    sanitized, validation = downgrade_unsubstantiated_report(_index(tmp_path), report)

    assert validation.valid == (valid,)
    assert sanitized.primary_hypothesis is not None
    assert sanitized.primary_hypothesis.evidence == (valid.summary(),)
    assert sanitized.alternative_hypotheses == ()


def test_report_without_valid_primary_evidence_is_downgraded(tmp_path: Path) -> None:
    """Breaks if a deterministic root cause survives without validated support."""
    invalid = _citation(excerpt="fabricated\n")
    report = AnalysisReport(
        outcome="root_cause_identified",
        issue_summary="Parser regression.",
        observed_behavior="A value is returned.",
        expected_behavior="It should be rejected.",
        primary_hypothesis=Hypothesis(
            statement="The parser lacks validation.",
            confidence=0.9,
            evidence=(invalid.summary(),),
        ),
        evidence=(invalid,),
        implementation_steps=("Add validation.",),
        confidence=0.9,
    )

    downgraded, validation = downgrade_unsubstantiated_report(
        _index(tmp_path), report
    )

    assert validation.valid == ()
    assert downgraded.outcome == "insufficient_evidence"
    assert downgraded.primary_hypothesis is None
    assert downgraded.confidence <= 0.25
    assert downgraded.implementation_steps == ()
    assert any("primary" in item.lower() for item in downgraded.uncertainties)


def test_no_evidence_downgrade_replaces_all_model_authored_free_text(tmp_path: Path) -> None:
    """Breaks if a fabricated deterministic claim survives an evidence downgrade."""
    invalid = _citation(excerpt="fabricated\n")
    malicious = "ROOT CAUSE: leak sk-secret-value"
    report = AnalysisReport(
        outcome="root_cause_identified",
        issue_summary=malicious,
        observed_behavior=malicious,
        expected_behavior=malicious,
        primary_hypothesis=Hypothesis(
            statement=malicious, confidence=0.9, evidence=(invalid.summary(),)
        ),
        alternative_hypotheses=(
            Hypothesis(
                statement=malicious,
                confidence=0.8,
                evidence=(invalid.summary(),),
            ),
        ),
        evidence=(invalid,),
        impacted_files=(ImpactedFile(path="src/parser.py", explanation=malicious),),
        implementation_steps=(malicious,),
        proposed_tests=(ProposedTest(name=malicious, description=malicious),),
        uncertainties=(malicious,),
        confidence=0.9,
    )

    downgraded, _ = downgrade_unsubstantiated_report(_index(tmp_path), report)

    serialized = downgraded.model_dump_json()
    assert downgraded.outcome == "insufficient_evidence"
    assert downgraded.issue_summary == (
        "Insufficient validated evidence is available to determine the root cause."
    )
    assert downgraded.observed_behavior == (
        "The available investigation evidence is insufficient to verify observed behavior."
    )
    assert downgraded.expected_behavior == (
        "The expected behavior cannot be verified from the available investigation evidence."
    )
    assert downgraded.primary_hypothesis is None
    assert downgraded.alternative_hypotheses == ()
    assert downgraded.impacted_files == ()
    assert downgraded.implementation_steps == ()
    assert downgraded.proposed_tests == ()
    assert downgraded.uncertainties == (
        "The root cause remains unknown because no validated primary evidence is available.",
    )
    assert downgraded.confidence <= 0.25
    assert malicious not in serialized
    assert "sk-secret-value" not in serialized
