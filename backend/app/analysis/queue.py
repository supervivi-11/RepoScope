from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .domain import (
    AnalysisConflictError,
    AnalysisNotFoundError,
    PersistentAnalysisStatus,
    TERMINAL_STATUSES,
    validate_status_transition,
)
from .models import AnalysisJobRow, utc_now


_RECOVERABLE_STATUSES = (
    PersistentAnalysisStatus.INGESTING,
    PersistentAnalysisStatus.INDEXING,
    PersistentAnalysisStatus.INVESTIGATING,
    PersistentAnalysisStatus.REVISING,
)


@dataclass(frozen=True, slots=True)
class ClaimedAnalysis:
    analysis_id: UUID
    worker_id: str
    lease_expires_at: datetime
    attempt_count: int
    status: PersistentAnalysisStatus


def build_claim_statement(now: datetime):
    """Build the PostgreSQL atomic claim selector used inside one transaction."""
    lease_available = or_(
        AnalysisJobRow.lease_expires_at.is_(None),
        AnalysisJobRow.lease_expires_at <= now,
    )
    eligible = or_(
        and_(
            AnalysisJobRow.status == PersistentAnalysisStatus.QUEUED,
            lease_available,
        ),
        and_(
            AnalysisJobRow.status.in_(_RECOVERABLE_STATUSES),
            lease_available,
        ),
        and_(
            AnalysisJobRow.status == PersistentAnalysisStatus.REVIEW_READY,
            AnalysisJobRow.pending_feedback_action == "accept",
            lease_available,
        ),
    )
    return (
        select(AnalysisJobRow)
        .where(eligible)
        .order_by(AnalysisJobRow.created_at, AnalysisJobRow.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )


class PostgresJobQueue:
    """Durable lease queue with PostgreSQL SKIP LOCKED claim semantics."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        lease_duration: timedelta = timedelta(minutes=2),
    ) -> None:
        if lease_duration <= timedelta(0):
            raise ValueError("lease duration must be positive")
        self._sessions = sessions
        self._lease_duration = lease_duration

    @property
    def heartbeat_interval(self) -> float:
        return max(0.1, self._lease_duration.total_seconds() / 3)

    async def claim_next(
        self, *, worker_id: str, now: datetime | None = None
    ) -> ClaimedAnalysis | None:
        _validate_worker_id(worker_id)
        timestamp = now or utc_now()
        async with self._sessions.begin() as session:
            row = await session.scalar(build_claim_statement(timestamp))
            if row is None:
                return None
            row.lease_worker_id = worker_id
            row.lease_expires_at = timestamp + self._lease_duration
            row.attempt_count += 1
            row.updated_at = timestamp
            await session.flush()
            return _claimed(row)

    async def heartbeat(
        self,
        claim: ClaimedAnalysis,
        *,
        now: datetime | None = None,
    ) -> ClaimedAnalysis:
        _validate_worker_id(claim.worker_id)
        timestamp = now or utc_now()
        async with self._sessions.begin() as session:
            row = await self._owned_row(session, claim, now=timestamp)
            if row.status in TERMINAL_STATUSES:
                raise AnalysisConflictError("Terminal analyses cannot be heartbeated.")
            row.lease_expires_at = timestamp + self._lease_duration
            row.updated_at = timestamp
            await session.flush()
            return _claimed(row)

    async def release(
        self, claim: ClaimedAnalysis, *, now: datetime | None = None
    ) -> None:
        _validate_worker_id(claim.worker_id)
        timestamp = now or utc_now()
        async with self._sessions.begin() as session:
            row = await self._owned_row(session, claim, now=timestamp)
            row.lease_worker_id = None
            row.lease_expires_at = None
            row.updated_at = utc_now()

    async def finish(
        self,
        claim: ClaimedAnalysis,
        *,
        status: PersistentAnalysisStatus,
        now: datetime | None = None,
    ) -> None:
        if status not in TERMINAL_STATUSES:
            raise ValueError("queue finish requires a terminal status")
        _validate_worker_id(claim.worker_id)
        timestamp = now or utc_now()
        async with self._sessions.begin() as session:
            row = await self._owned_row(session, claim, now=timestamp)
            validate_status_transition(row.status, status)
            row.status = status
            row.lease_worker_id = None
            row.lease_expires_at = None
            row.updated_at = utc_now()

    @staticmethod
    async def _owned_row(
        session: AsyncSession,
        claim: ClaimedAnalysis,
        *,
        now: datetime,
    ) -> AnalysisJobRow:
        row = await session.scalar(
            select(AnalysisJobRow)
            .where(AnalysisJobRow.id == claim.analysis_id)
            .with_for_update()
        )
        if row is None:
            raise AnalysisNotFoundError()
        expires_at = row.lease_expires_at
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=now.tzinfo)
        if (
            row.lease_worker_id != claim.worker_id
            or row.attempt_count != claim.attempt_count
            or expires_at is None
            or expires_at <= now
        ):
            raise AnalysisConflictError("Analysis lease is stale or owned by another worker.")
        return row


def _claimed(row: AnalysisJobRow) -> ClaimedAnalysis:
    assert row.lease_worker_id is not None
    assert row.lease_expires_at is not None
    return ClaimedAnalysis(
        analysis_id=row.id,
        worker_id=row.lease_worker_id,
        lease_expires_at=row.lease_expires_at,
        attempt_count=row.attempt_count,
        status=row.status,
    )


def _validate_worker_id(worker_id: str) -> None:
    if not isinstance(worker_id, str) or not worker_id.strip() or len(worker_id) > 200:
        raise ValueError("worker ID must contain 1 to 200 characters")
