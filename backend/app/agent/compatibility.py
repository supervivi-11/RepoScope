from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ValidationError

from .models import AnalysisReport, AnalysisState, EvidenceCitation, Hypothesis


_LEGACY_REPORT_UNCERTAINTY = (
    "A legacy report exceeded the current public contract and requires re-analysis."
)


def load_persisted_report(value: Any) -> AnalysisReport:
    """Load current or pre-compact-reference report rows without weakening writes."""
    candidate = _mapping(value)
    try:
        return AnalysisReport.model_validate(candidate)
    except (TypeError, ValueError, ValidationError):
        pass
    try:
        payload = _mapping(value)
        payload["primary_hypothesis"] = _compact_hypothesis(
            payload.get("primary_hypothesis")
        )
        payload["alternative_hypotheses"] = [
            item
            for item in (
                _compact_hypothesis(candidate)
                for candidate in payload.get("alternative_hypotheses", ())
            )
            if item is not None
        ]
        return AnalysisReport.model_validate(payload)
    except (TypeError, ValueError, ValidationError):
        return _legacy_insufficient_report()


def load_persisted_analysis_state(value: Any) -> AnalysisState:
    """Upgrade legacy report and hypothesis shapes at checkpoint read boundaries."""
    candidate = _mapping(value)
    try:
        return AnalysisState.model_validate(candidate)
    except (TypeError, ValueError, ValidationError):
        pass

    payload = _mapping(candidate)
    for key in ("report", "original_report"):
        if payload.get(key) is not None:
            payload[key] = load_persisted_report(payload[key]).model_dump(mode="json")
    payload["report_history"] = [
        load_persisted_report(item).model_dump(mode="json")
        for item in payload.get("report_history", ())
    ][:2]

    hypotheses: list[dict[str, Any]] = []
    for candidate in payload.get("hypotheses", ()):
        compact = _compact_hypothesis(candidate)
        if compact is None:
            continue
        try:
            hypotheses.append(
                Hypothesis.model_validate(compact).model_dump(mode="json")
            )
        except (TypeError, ValueError, ValidationError):
            continue
    payload["hypotheses"] = hypotheses

    evidence: list[dict[str, Any]] = []
    for candidate in payload.get("evidence", ()):
        try:
            evidence.append(
                EvidenceCitation.model_validate(candidate).model_dump(mode="json")
            )
        except (TypeError, ValueError, ValidationError):
            continue
    payload["evidence"] = evidence
    return AnalysisState.model_validate(payload)


def _compact_hypothesis(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    payload = _mapping(value)
    references: list[dict[str, Any]] = []
    for candidate in payload.get("evidence", ()):
        citation = _mapping(candidate)
        keys = ("commit_sha", "path", "start_line", "end_line")
        if all(key in citation for key in keys):
            references.append({key: citation[key] for key in keys})
    payload["evidence"] = references
    return payload


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        return {
            field: _plain_value(getattr(value, field))
            for field in value.__class__.model_fields
            if hasattr(value, field)
        }
    if isinstance(value, Mapping):
        return {str(key): _plain_value(item) for key, item in value.items()}
    raise TypeError("persisted agent data must be an object")


def _plain_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _mapping(value)
    if isinstance(value, Mapping):
        return {str(key): _plain_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain_value(item) for item in value]
    return value


def _legacy_insufficient_report() -> AnalysisReport:
    return AnalysisReport(
        outcome="insufficient_evidence",
        issue_summary="A legacy report cannot be represented by the current contract.",
        observed_behavior="The previously stored observation is unavailable safely.",
        expected_behavior="The previously stored expectation is unavailable safely.",
        uncertainties=(_LEGACY_REPORT_UNCERTAINTY,),
        confidence=0.0,
    )
