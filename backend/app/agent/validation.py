from __future__ import annotations

from pydantic import Field

from app.investigation import PythonRepositoryIndex
from app.investigation.errors import InvestigationError

from .models import (
    AnalysisReport,
    EvidenceCitation,
    EvidenceCitationSummary,
    FrozenModel,
    Hypothesis,
)


class EvidenceValidationIssue(FrozenModel):
    citation: EvidenceCitationSummary
    code: str
    safe_error: str


class EvidenceValidationResult(FrozenModel):
    valid: tuple[EvidenceCitation, ...] = Field(default=(), max_length=64)
    invalid: tuple[EvidenceValidationIssue, ...] = Field(default=(), max_length=64)


def validate_evidence(
    index: PythonRepositoryIndex,
    citations: tuple[EvidenceCitation, ...],
) -> EvidenceValidationResult:
    valid: list[EvidenceCitation] = []
    invalid: list[EvidenceValidationIssue] = []
    expected_sha = index.snapshot.commit_sha
    for citation in citations:
        if citation.commit_sha != expected_sha:
            invalid.append(
                EvidenceValidationIssue(
                    citation=citation.summary(),
                    code="commit_mismatch",
                    safe_error="Citation commit does not match the immutable snapshot.",
                )
            )
            continue
        try:
            source = index.read_code(
                citation.path, citation.start_line, citation.end_line
            )
        except (InvestigationError, ValueError):
            invalid.append(
                EvidenceValidationIssue(
                    citation=citation.summary(),
                    code="source_mismatch",
                    safe_error="Citation source path or line range is not available.",
                )
            )
            continue
        if (
            source.location.commit_sha != citation.commit_sha
            or source.location.path != citation.path
            or source.location.start_line != citation.start_line
            or source.location.end_line != citation.end_line
            or source.excerpt != citation.excerpt
        ):
            invalid.append(
                EvidenceValidationIssue(
                    citation=citation.summary(),
                    code="excerpt_mismatch",
                    safe_error="Citation excerpt does not exactly match the snapshot.",
                )
            )
            continue
        valid.append(citation)
    return EvidenceValidationResult(valid=tuple(valid), invalid=tuple(invalid))


def downgrade_unsubstantiated_report(
    index: PythonRepositoryIndex,
    report: AnalysisReport,
) -> tuple[AnalysisReport, EvidenceValidationResult]:
    candidates = _deduplicate_citations(
        report.evidence
        + tuple(
            citation
            for hypothesis in (
                *((report.primary_hypothesis,) if report.primary_hypothesis else ()),
                *report.alternative_hypotheses,
            )
            for citation in hypothesis.evidence
        )
    )
    validation = validate_evidence(index, candidates)
    valid_keys = {_citation_key(item) for item in validation.valid}

    primary = _filter_hypothesis(report.primary_hypothesis, valid_keys)
    alternatives = tuple(
        _filter_hypothesis(item, valid_keys) for item in report.alternative_hypotheses
    )
    filtered_alternatives = tuple(item for item in alternatives if item is not None)
    if primary is None or not primary.evidence:
        uncertainty = "No valid evidence remains for the primary hypothesis."
        uncertainties = _deduplicate_strings(report.uncertainties + (uncertainty,))
        return (
            report.model_copy(
                update={
                    "outcome": "insufficient_evidence",
                    "primary_hypothesis": None,
                    "alternative_hypotheses": filtered_alternatives,
                    "evidence": validation.valid,
                    "impacted_files": (),
                    "implementation_steps": (),
                    "confidence": min(report.confidence, 0.25),
                    "uncertainties": uncertainties,
                }
            ),
            validation,
        )
    return (
        report.model_copy(
            update={
                "primary_hypothesis": primary,
                "alternative_hypotheses": filtered_alternatives,
                "evidence": validation.valid,
            }
        ),
        validation,
    )


def _filter_hypothesis(
    hypothesis: Hypothesis | None,
    valid_keys: set[tuple[str, str, int, int, str]],
) -> Hypothesis | None:
    if hypothesis is None:
        return None
    evidence = tuple(
        item for item in hypothesis.evidence if _citation_key(item) in valid_keys
    )
    return hypothesis.model_copy(update={"evidence": evidence})


def _deduplicate_citations(
    citations: tuple[EvidenceCitation, ...],
) -> tuple[EvidenceCitation, ...]:
    unique: dict[tuple[str, str, int, int, str], EvidenceCitation] = {}
    for item in citations:
        unique.setdefault(_citation_key(item), item)
    return tuple(unique.values())


def _citation_key(item: EvidenceCitation) -> tuple[str, str, int, int, str]:
    return (
        item.commit_sha,
        item.path,
        item.start_line,
        item.end_line,
        item.excerpt,
    )


def _deduplicate_strings(items: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(items))
