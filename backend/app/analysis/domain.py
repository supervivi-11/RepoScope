from __future__ import annotations

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
    created_at: datetime


class InvalidStatusTransitionError(AnalysisConflictError):
    pass
