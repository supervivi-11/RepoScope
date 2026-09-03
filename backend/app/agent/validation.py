from __future__ import annotations

from pydantic import Field, ValidationError

from app.investigation import PythonRepositoryIndex
from app.investigation.errors import InvestigationError

from .models import (
    AnalysisReport,
    EvidenceCitation,
    EvidenceCitationSummary,
    FrozenModel,
    Hypothesis,
)


_INSUFFICIENT_EVIDENCE_SUMMARY = (
    "Insufficient validated evidence is available to determine the root cause."
)
_INSUFFICIENT_EVIDENCE_OBSERVED = (
    "The available investigation evidence is insufficient to verify observed behavior."
)
_INSUFFICIENT_EVIDENCE_EXPECTED = (
    "The expected behavior cannot be verified from the available investigation evidence."
)
_INSUFFICIENT_EVIDENCE_UNCERTAINTY = (
    "The root cause remains unknown because no validated primary evidence is available."
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
    candidates = _deduplicate_citations(report.evidence)
    validation = validate_evidence(index, candidates)
    valid_keys = {_summary_key(item.summary()) for item in validation.valid}

    primary = _filter_hypothesis(report.primary_hypothesis, valid_keys)
    alternatives = tuple(
        _filter_hypothesis(item, valid_keys) for item in report.alternative_hypotheses
    )
    filtered_alternatives = tuple(item for item in alternatives if item is not None)
    if primary is None or not primary.evidence:
        return (
            report.model_copy(
                update={
                    "outcome": "insufficient_evidence",
                    "issue_summary": _INSUFFICIENT_EVIDENCE_SUMMARY,
                    "observed_behavior": _INSUFFICIENT_EVIDENCE_OBSERVED,
                    "expected_behavior": _INSUFFICIENT_EVIDENCE_EXPECTED,
                    "primary_hypothesis": None,
                    "alternative_hypotheses": (),
                    "evidence": (),
                    "impacted_files": (),
                    "implementation_steps": (),
                    "proposed_tests": (),
                    "confidence": min(report.confidence, 0.25),
                    "uncertainties": (_INSUFFICIENT_EVIDENCE_UNCERTAINTY,),
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


def bind_tool_evidence(
    index: PythonRepositoryIndex,
    report: AnalysisReport,
    tool_evidence: tuple[EvidenceCitation, ...],
) -> AnalysisReport:
    """Resolve explicit hypothesis references, never invent support for a claim.

    An explicit report citation (even a fabricated one) takes precedence and is
    left for the normal validator to reject. Only missing payloads may be bound
    from exact, previously read tool evidence; critique-authored text is excluded.
    """
    existing = {_summary_key(item.summary()) for item in report.evidence}
    references = tuple(
        reference
        for hypothesis in (report.primary_hypothesis, *report.alternative_hypotheses)
        if hypothesis is not None
        for reference in hypothesis.evidence
    )
    requested = {_summary_key(item) for item in references} - existing
    candidates = tuple(
        item for item in _deduplicate_citations(tool_evidence)
        if _summary_key(item.summary()) in requested
    )
    # Process individually: tool history can contain more than 64 excerpts.
    available: dict[tuple[str, str, int, int], EvidenceCitation] = {}
    for candidate in candidates:
        if validate_evidence(index, (candidate,)).valid:
            available.setdefault(_summary_key(candidate.summary()), candidate)
    bound = report
    for reference in references:
        key = _summary_key(reference)
        if key in existing or key not in available:
            continue
        payload = bound.model_dump()
        payload["evidence"] = (*bound.evidence, available[key])
        try:
            bound = AnalysisReport.model_validate(payload)
        except ValidationError:
            # Preserve the public count and serialized byte limits, fail closed.
            break
        existing.add(key)
    return bound


def _filter_hypothesis(
    hypothesis: Hypothesis | None,
    valid_keys: set[tuple[str, str, int, int]],
) -> Hypothesis | None:
    if hypothesis is None:
        return None
    evidence = tuple(
        item for item in hypothesis.evidence if _summary_key(item) in valid_keys
    )
    if not evidence:
        return None
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


def _summary_key(item: EvidenceCitationSummary) -> tuple[str, str, int, int]:
    return (
        item.commit_sha,
        item.path,
        item.start_line,
        item.end_line,
    )
