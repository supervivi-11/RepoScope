from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from app.agent import AnalysisReport


class PersistentAnalysisStatus(StrEnum):
    QUEUED = "QUEUED"
    INGESTING = "INGESTING"
    INDEXING = "INDEXING"
    INVESTIGATING = "INVESTIGATING"
    REVIEW_READY = "REVIEW_READY"
    REVISING = "REVISING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


TERMINAL_STATUSES = frozenset(
    {PersistentAnalysisStatus.COMPLETED, PersistentAnalysisStatus.FAILED}
)
_EVENT_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,99}$")

ALLOWED_STATUS_TRANSITIONS = {
    PersistentAnalysisStatus.QUEUED: frozenset(
        {PersistentAnalysisStatus.INGESTING, PersistentAnalysisStatus.FAILED}
    ),
    PersistentAnalysisStatus.INGESTING: frozenset(
        {PersistentAnalysisStatus.INDEXING, PersistentAnalysisStatus.FAILED}
    ),
    PersistentAnalysisStatus.INDEXING: frozenset(
        {PersistentAnalysisStatus.INVESTIGATING, PersistentAnalysisStatus.FAILED}
    ),
    PersistentAnalysisStatus.INVESTIGATING: frozenset(
        {PersistentAnalysisStatus.REVIEW_READY, PersistentAnalysisStatus.FAILED}
    ),
    PersistentAnalysisStatus.REVIEW_READY: frozenset(
        {
            PersistentAnalysisStatus.REVISING,
            PersistentAnalysisStatus.COMPLETED,
            PersistentAnalysisStatus.FAILED,
        }
    ),
    PersistentAnalysisStatus.REVISING: frozenset(
        {PersistentAnalysisStatus.REVIEW_READY, PersistentAnalysisStatus.FAILED}
    ),
    PersistentAnalysisStatus.COMPLETED: frozenset(),
    PersistentAnalysisStatus.FAILED: frozenset(),
}


class AnalysisRepositoryError(Exception):
    """Safe base error for durable analysis operations."""


class AnalysisNotFoundError(AnalysisRepositoryError):
    pass


class AnalysisConflictError(AnalysisRepositoryError):
    pass


@dataclass(frozen=True, slots=True)
class CreateAnalysisResult:
    analysis_id: UUID
    status: PersistentAnalysisStatus
    replayed: bool


@dataclass(frozen=True, slots=True)
class StoredEvent:
    sequence: int
    event_type: str
    data: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class StoredReport:
    version: int
    report: AnalysisReport
    state: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class StoredAnalysis:
    analysis_id: UUID
    repo_url: str
    issue_number: int
    status: PersistentAnalysisStatus
    progress: dict[str, Any]
    counters: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    error_code: str | None
    error_message: str | None
    state: dict[str, Any]
    pending_feedback_action: Literal["accept", "revise"] | None
    report_history: tuple[StoredReport, ...]

    @property
    def current_report(self) -> AnalysisReport | None:
        return self.report_history[-1].report if self.report_history else None


@dataclass(frozen=True, slots=True)
class FeedbackResult:
    status: PersistentAnalysisStatus
    replayed: bool


@dataclass(frozen=True, slots=True)
class StoredFeedback:
    action: Literal["accept", "revise"]
    comment: str | None
    fingerprint: str
    processed_at: datetime | None
    created_at: datetime


class InvalidStatusTransitionError(AnalysisConflictError):
    pass


def validate_status_transition(
    current: PersistentAnalysisStatus,
    target: PersistentAnalysisStatus,
) -> None:
    if target is current:
        return
    if target not in ALLOWED_STATUS_TRANSITIONS[current]:
        raise InvalidStatusTransitionError(
            f"Cannot transition {current.value} to {target.value}."
        )


def validate_event_type(event_type: str) -> str:
    if (
        not isinstance(event_type, str)
        or _EVENT_TYPE_PATTERN.fullmatch(event_type) is None
    ):
        raise ValueError("event type must match ^[a-z][a-z0-9_]{0,99}$")
    return event_type
