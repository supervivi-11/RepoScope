from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.agent import (
    AnalysisReport,
    EvidenceCitation,
    Hypothesis,
    downgrade_unsubstantiated_report,
    validate_evidence,
)
from app.ingestion import RepositorySnapshot
from app.investigation import PythonRepositoryIndex


SHA = "a" * 40
SOURCE = "def parse(value):\n    return value\n"


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
            evidence=(invalid,),
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
