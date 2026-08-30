from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent import AnalysisReport
from app.ingestion import RepositoryCoordinates, validate_issue_number

from .domain import (
    AnalysisConflictError,
    AnalysisNotFoundError,
    CreateAnalysisResult,
    FeedbackResult,
    InvalidStatusTransitionError,
    PersistentAnalysisStatus,
    StoredAnalysis,
    StoredEvent,
    StoredFeedback,
    StoredReport,
)
from .models import (
    AnalysisEventRow,
    AnalysisFeedbackCommandRow,
    AnalysisJobRow,
    AnalysisReportVersionRow,
    utc_now,
)
from .failures import PublicFailure, canonical_public_failure, redact_public_data


_ALLOWED_TRANSITIONS = {
    PersistentAnalysisStatus.QUEUED: {
        PersistentAnalysisStatus.INGESTING,
        PersistentAnalysisStatus.FAILED,
    },
    PersistentAnalysisStatus.INGESTING: {
        PersistentAnalysisStatus.INDEXING,
        PersistentAnalysisStatus.FAILED,
    },
    PersistentAnalysisStatus.INDEXING: {
        PersistentAnalysisStatus.INVESTIGATING,
        PersistentAnalysisStatus.FAILED,
    },
    PersistentAnalysisStatus.INVESTIGATING: {
        PersistentAnalysisStatus.REVIEW_READY,
        PersistentAnalysisStatus.FAILED,
    },
    PersistentAnalysisStatus.REVIEW_READY: {
        PersistentAnalysisStatus.REVISING,
        PersistentAnalysisStatus.COMPLETED,
        PersistentAnalysisStatus.FAILED,
    },
    PersistentAnalysisStatus.REVISING: {
        PersistentAnalysisStatus.REVIEW_READY,
        PersistentAnalysisStatus.FAILED,
    },
    PersistentAnalysisStatus.COMPLETED: set(),
    PersistentAnalysisStatus.FAILED: set(),
}


class AnalysisRepository:
    """Async durable service over analysis jobs and append-only artifacts."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def create_analysis(
        self,
        *,
        repo_url: str,
        issue_number: object,
        idempotency_key: str | None = None,
    ) -> CreateAnalysisResult:
        coordinates = RepositoryCoordinates.parse(repo_url)
        number = validate_issue_number(issue_number)
        canonical_url = coordinates.canonical_url
        async with self.sessions() as session:
            if idempotency_key is not None:
                existing = await session.scalar(
                    select(AnalysisJobRow).where(
                        AnalysisJobRow.idempotency_key == idempotency_key
                    )
                )
                if existing is not None:
                    if (
                        existing.repo_url != canonical_url
                        or existing.issue_number != number
                    ):
                        raise AnalysisConflictError(
                            "Idempotency key was already used for another request."
                        )
                    return CreateAnalysisResult(
                        analysis_id=existing.id,
                        status=existing.status,
                        replayed=True,
                    )
            row = AnalysisJobRow(
                repo_url=canonical_url,
                issue_number=number,
                idempotency_key=idempotency_key,
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                if idempotency_key is None:
                    raise
                existing = await session.scalar(
                    select(AnalysisJobRow).where(
                        AnalysisJobRow.idempotency_key == idempotency_key
                    )
                )
                if existing is None:
                    raise
                if (
                    existing.repo_url != canonical_url
                    or existing.issue_number != number
                ):
                    raise AnalysisConflictError(
                        "Idempotency key was already used for another request."
                    )
                return CreateAnalysisResult(existing.id, existing.status, True)
            return CreateAnalysisResult(row.id, row.status, False)

    async def get_analysis(self, analysis_id: UUID) -> StoredAnalysis:
        async with self.sessions() as session:
            row = await self._get_row(session, analysis_id)
            report_rows = tuple(
                (
                    await session.scalars(
                        select(AnalysisReportVersionRow)
                        .where(AnalysisReportVersionRow.analysis_id == analysis_id)
                        .order_by(AnalysisReportVersionRow.version)
                    )
                ).all()
            )
            reports = tuple(
                StoredReport(
                    version=item.version,
                    report=AnalysisReport.model_validate(item.report),
                    state=dict(item.state),
                    created_at=_aware(item.created_at),
                )
                for item in report_rows
            )
            return StoredAnalysis(
                analysis_id=row.id,
                repo_url=row.repo_url,
                issue_number=row.issue_number,
                status=row.status,
                progress=dict(row.progress),
                counters=dict(row.counters),
                created_at=_aware(row.created_at),
                updated_at=_aware(row.updated_at),
                error_code=row.error_code,
                error_message=row.error_message,
                state=dict(row.state),
                report_history=reports,
            )

    async def append_event(
        self, analysis_id: UUID, event_type: str, data: dict[str, Any]
    ) -> StoredEvent:
        if not event_type or len(event_type) > 100:
            raise ValueError("event type must contain 1 to 100 characters")
        safe_data = redact_public_data(data)
        if not isinstance(safe_data, dict):
            raise ValueError("event data must be a JSON object")
        async with self.sessions.begin() as session:
            next_value = await session.scalar(
                update(AnalysisJobRow)
                .where(AnalysisJobRow.id == analysis_id)
                .values(
                    next_event_sequence=AnalysisJobRow.next_event_sequence + 1,
                    updated_at=utc_now(),
                )
                .returning(AnalysisJobRow.next_event_sequence)
            )
            if next_value is None:
                raise AnalysisNotFoundError()
            row = AnalysisEventRow(
                analysis_id=analysis_id,
                sequence=next_value - 1,
                event_type=event_type,
                data=safe_data,
            )
            session.add(row)
            await session.flush()
            return StoredEvent(
                sequence=row.sequence,
                event_type=row.event_type,
                data=dict(row.data),
                created_at=_aware(row.created_at),
            )

    async def list_events(
        self, analysis_id: UUID, *, after_sequence: int = 0
    ) -> tuple[StoredEvent, ...]:
        async with self.sessions() as session:
            exists = await session.scalar(
                select(AnalysisJobRow.id).where(AnalysisJobRow.id == analysis_id)
            )
            if exists is None:
                raise AnalysisNotFoundError()
            rows = tuple(
                (
                    await session.scalars(
                        select(AnalysisEventRow)
                        .where(
                            AnalysisEventRow.analysis_id == analysis_id,
                            AnalysisEventRow.sequence > after_sequence,
                        )
                        .order_by(AnalysisEventRow.sequence)
                    )
                ).all()
            )
            return tuple(
                StoredEvent(
                    sequence=row.sequence,
                    event_type=row.event_type,
                    data=dict(row.data),
                    created_at=_aware(row.created_at),
                )
                for row in rows
            )

    async def save_report(
        self,
        analysis_id: UUID,
        report: AnalysisReport,
        *,
        state: dict[str, Any],
    ) -> StoredReport:
        async with self.sessions.begin() as session:
            version = await session.scalar(
                update(AnalysisJobRow)
                .where(AnalysisJobRow.id == analysis_id)
                .values(
                    next_report_version=AnalysisJobRow.next_report_version + 1,
                    state=state,
                    updated_at=utc_now(),
                )
                .returning(AnalysisJobRow.next_report_version)
            )
            if version is None:
                raise AnalysisNotFoundError()
            row = AnalysisReportVersionRow(
                analysis_id=analysis_id,
                version=version - 1,
                report=report.model_dump(mode="json"),
                state=state,
            )
            session.add(row)
            await session.flush()
            return StoredReport(
                version=row.version,
                report=report,
                state=dict(state),
                created_at=_aware(row.created_at),
            )

    async def transition(
        self,
        analysis_id: UUID,
        status: PersistentAnalysisStatus,
        *,
        progress: dict[str, Any] | None = None,
        counters: dict[str, Any] | None = None,
    ) -> PersistentAnalysisStatus:
        async with self.sessions.begin() as session:
            row = await self._get_row(session, analysis_id, for_update=True)
            if status == row.status:
                return row.status
            if status not in _ALLOWED_TRANSITIONS[row.status]:
                raise InvalidStatusTransitionError(
                    f"Cannot transition {row.status.value} to {status.value}."
                )
            row.status = status
            row.updated_at = utc_now()
            if progress is not None:
                row.progress = progress
            if counters is not None:
                row.counters = counters
            return row.status

    async def submit_feedback(
        self,
        analysis_id: UUID,
        *,
        action: Literal["accept", "revise"],
        comment: str | None = None,
    ) -> FeedbackResult:
        if action == "revise":
            if comment is None or not comment.strip():
                raise ValueError("revision comment must be nonblank")
        elif comment is not None:
            raise ValueError("accept feedback cannot include a comment")
        fingerprint = _feedback_fingerprint(action, comment)
        async with self.sessions.begin() as session:
            row = await self._get_row(session, analysis_id, for_update=True)
            duplicate = await session.scalar(
                select(AnalysisFeedbackCommandRow).where(
                    AnalysisFeedbackCommandRow.analysis_id == analysis_id,
                    AnalysisFeedbackCommandRow.fingerprint == fingerprint,
                )
            )
            if duplicate is not None:
                return FeedbackResult(status=row.status, replayed=True)
            if row.status is not PersistentAnalysisStatus.REVIEW_READY:
                raise AnalysisConflictError("Analysis is not ready for feedback.")
            if action == "revise":
                if row.revision_count >= 1:
                    raise AnalysisConflictError(
                        "The single allowed report revision has already been used."
                    )
                row.revision_count += 1
                row.status = PersistentAnalysisStatus.REVISING
            else:
                row.status = PersistentAnalysisStatus.COMPLETED
            row.updated_at = utc_now()
            session.add(
                AnalysisFeedbackCommandRow(
                    analysis_id=analysis_id,
                    action=action,
                    comment=comment,
                    fingerprint=fingerprint,
                )
            )
            return FeedbackResult(status=row.status, replayed=False)

    async def latest_feedback(self, analysis_id: UUID) -> StoredFeedback:
        async with self.sessions() as session:
            exists = await session.scalar(
                select(AnalysisJobRow.id).where(AnalysisJobRow.id == analysis_id)
            )
            if exists is None:
                raise AnalysisNotFoundError()
            row = await session.scalar(
                select(AnalysisFeedbackCommandRow)
                .where(AnalysisFeedbackCommandRow.analysis_id == analysis_id)
                .order_by(AnalysisFeedbackCommandRow.id.desc())
                .limit(1)
            )
            if row is None:
                raise AnalysisConflictError("Analysis has no feedback command.")
            return StoredFeedback(
                action=row.action,
                comment=row.comment,
                created_at=_aware(row.created_at),
            )

    async def fail_safe(
        self, analysis_id: UUID, failure: PublicFailure
    ) -> StoredAnalysis:
        failure = canonical_public_failure(failure)
        async with self.sessions.begin() as session:
            row = await self._get_row(session, analysis_id, for_update=True)
            if row.status is PersistentAnalysisStatus.COMPLETED:
                raise AnalysisConflictError("Completed analyses cannot fail.")
            if row.status is not PersistentAnalysisStatus.FAILED:
                sequence = row.next_event_sequence
                row.next_event_sequence += 1
                row.status = PersistentAnalysisStatus.FAILED
                row.error_code = failure.code
                row.error_message = failure.message
                row.lease_worker_id = None
                row.lease_expires_at = None
                row.updated_at = utc_now()
                session.add(
                    AnalysisEventRow(
                        analysis_id=analysis_id,
                        sequence=sequence,
                        event_type="analysis_failed",
                        data={"code": failure.code, "error": failure.message},
                    )
                )
        return await self.get_analysis(analysis_id)

    @staticmethod
    async def _get_row(
        session: AsyncSession, analysis_id: UUID, *, for_update: bool = False
    ) -> AnalysisJobRow:
        statement = select(AnalysisJobRow).where(AnalysisJobRow.id == analysis_id)
        if for_update:
            statement = statement.with_for_update()
        row = await session.scalar(statement)
        if row is None:
            raise AnalysisNotFoundError()
        return row


def _feedback_fingerprint(action: str, comment: str | None) -> str:
    encoded = json.dumps(
        {"action": action, "comment": comment}, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
