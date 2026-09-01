from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent import AnalysisReport
from app.agent.compatibility import load_persisted_report
from app.ingestion import RepositoryCoordinates, validate_issue_number
from app.report_limits import PREVIOUS_REPORT_HISTORY_MAX

from .domain import (
    AnalysisConflictError,
    AnalysisNotFoundError,
    CreateAnalysisResult,
    FeedbackResult,
    PersistentAnalysisStatus,
    StoredAnalysis,
    StoredEvent,
    StoredFeedback,
    StoredReport,
    TERMINAL_STATUSES,
    validate_event_type,
    validate_status_transition,
)
from .models import (
    AnalysisEventRow,
    AnalysisFeedbackCommandRow,
    AnalysisJobRow,
    AnalysisReportVersionRow,
    utc_now,
)
from .failures import (
    PublicFailure,
    canonical_public_failure,
    safe_public_error,
    safe_public_mapping,
    sanitize_public_mapping,
    sanitize_public_report,
)


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
                        .order_by(AnalysisReportVersionRow.version.desc())
                        .limit(PREVIOUS_REPORT_HISTORY_MAX + 1)
                    )
                ).all()
            )
            reports = tuple(
                StoredReport(
                    version=item.version,
                    report=sanitize_public_report(load_persisted_report(item.report)),
                    state=dict(item.state),
                    created_at=_aware(item.created_at),
                )
                for item in reversed(report_rows)
            )
            error_code, error_message = safe_public_error(
                row.error_code, row.error_message
            )
            return StoredAnalysis(
                analysis_id=row.id,
                repo_url=row.repo_url,
                issue_number=row.issue_number,
                status=row.status,
                progress=safe_public_mapping(row.progress),
                counters=safe_public_mapping(row.counters, counters=True),
                created_at=_aware(row.created_at),
                updated_at=_aware(row.updated_at),
                error_code=error_code,
                error_message=error_message,
                state=dict(row.state),
                pending_feedback_action=row.pending_feedback_action,
                report_history=reports,
            )

    async def append_event(
        self,
        analysis_id: UUID,
        event_type: str,
        data: dict[str, Any],
        *,
        lease: Any | None = None,
        dedupe_key: str | None = None,
    ) -> StoredEvent:
        event_type = validate_event_type(event_type)
        if dedupe_key is not None and (not dedupe_key or len(dedupe_key) > 200):
            raise ValueError("event dedupe key must contain 1 to 200 characters")
        safe_data = sanitize_public_mapping(data)
        async with self.sessions.begin() as session:
            job = await self._get_row(session, analysis_id, for_update=True)
            _assert_active_lease(job, lease)
            if dedupe_key is not None:
                existing = await session.scalar(
                    select(AnalysisEventRow).where(
                        AnalysisEventRow.analysis_id == analysis_id,
                        AnalysisEventRow.dedupe_key == dedupe_key,
                    )
                )
                if existing is not None:
                    return _deduped_event(existing, event_type, safe_data)
            row = AnalysisEventRow(
                analysis_id=analysis_id,
                sequence=job.next_event_sequence,
                event_type=event_type,
                data=safe_data,
                dedupe_key=dedupe_key,
            )
            job.next_event_sequence += 1
            job.updated_at = utc_now()
            session.add(row)
            await session.flush()
            return _stored_event(row)

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
            return tuple(_stored_event(row) for row in rows)

    async def event_stream_snapshot(
        self, analysis_id: UUID, *, after_sequence: int = 0
    ) -> tuple[tuple[StoredEvent, ...], PersistentAnalysisStatus]:
        """Read replay events and terminal status from one database snapshot."""
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    select(AnalysisJobRow.status, AnalysisEventRow)
                    .outerjoin(
                        AnalysisEventRow,
                        and_(
                            AnalysisEventRow.analysis_id == AnalysisJobRow.id,
                            AnalysisEventRow.sequence > after_sequence,
                        ),
                    )
                    .where(AnalysisJobRow.id == analysis_id)
                    .order_by(AnalysisEventRow.sequence)
                )
            ).all()
            if not rows:
                raise AnalysisNotFoundError()
            status = rows[0][0]
            events = tuple(
                _stored_event(event_row)
                for _, event_row in rows
                if event_row is not None
            )
            return events, status

    async def save_report(
        self,
        analysis_id: UUID,
        report: AnalysisReport,
        *,
        state: dict[str, Any],
        lease: Any | None = None,
        result_key: str | None = None,
    ) -> StoredReport:
        if result_key is not None and (not result_key or len(result_key) > 200):
            raise ValueError("report result key must contain 1 to 200 characters")
        report = sanitize_public_report(report)
        async with self.sessions.begin() as session:
            job = await self._get_row(session, analysis_id, for_update=True)
            _assert_active_lease(job, lease)
            if result_key is not None:
                existing = await session.scalar(
                    select(AnalysisReportVersionRow).where(
                        AnalysisReportVersionRow.analysis_id == analysis_id,
                        AnalysisReportVersionRow.result_key == result_key,
                    )
                )
                if existing is not None:
                    return _stored_report(existing)
            row = AnalysisReportVersionRow(
                analysis_id=analysis_id,
                version=job.next_report_version,
                report=report.model_dump(mode="json"),
                state=state,
                result_key=result_key,
            )
            job.next_report_version += 1
            job.state = state
            job.updated_at = utc_now()
            session.add(row)
            await session.flush()
            return _stored_report(row)

    async def publish_review_result(
        self,
        analysis_id: UUID,
        *,
        report: AnalysisReport,
        state: dict[str, Any],
        counters: dict[str, Any],
        events: tuple[tuple[int, str, dict[str, Any]], ...],
        result_key: str,
        lease: Any | None = None,
        consume_feedback: bool = False,
    ) -> StoredAnalysis:
        """Publish one review result as a single externally visible transaction."""
        if not result_key or len(result_key) > 200:
            raise ValueError("report result key must contain 1 to 200 characters")
        safe_counters = sanitize_public_mapping(counters, counters=True)
        report = sanitize_public_report(report)
        async with self.sessions.begin() as session:
            job = await self._get_row(session, analysis_id, for_update=True)
            _assert_active_lease(job, lease)
            existing_report = await session.scalar(
                select(AnalysisReportVersionRow).where(
                    AnalysisReportVersionRow.analysis_id == analysis_id,
                    AnalysisReportVersionRow.result_key == result_key,
                )
            )
            if existing_report is not None:
                if job.status is not PersistentAnalysisStatus.REVIEW_READY:
                    raise AnalysisConflictError("Published review state is inconsistent.")
            else:
                for source_sequence, event_type, data in events:
                    event_type = validate_event_type(event_type)
                    dedupe_key = f"graph:{source_sequence}"
                    duplicate = await session.scalar(
                        select(AnalysisEventRow).where(
                            AnalysisEventRow.analysis_id == analysis_id,
                            AnalysisEventRow.dedupe_key == dedupe_key,
                        )
                    )
                    if duplicate is not None:
                        _deduped_event(
                            duplicate, event_type, sanitize_public_mapping(data)
                        )
                        continue
                    session.add(
                        AnalysisEventRow(
                            analysis_id=analysis_id,
                            sequence=job.next_event_sequence,
                            event_type=event_type,
                            data=sanitize_public_mapping(data),
                            dedupe_key=dedupe_key,
                        )
                    )
                    job.next_event_sequence += 1
                session.add(
                    AnalysisReportVersionRow(
                        analysis_id=analysis_id,
                        version=job.next_report_version,
                        report=report.model_dump(mode="json"),
                        state=state,
                        result_key=result_key,
                    )
                )
                job.next_report_version += 1
                validate_status_transition(
                    job.status, PersistentAnalysisStatus.REVIEW_READY
                )
                job.status = PersistentAnalysisStatus.REVIEW_READY
                job.state = state
                job.counters = safe_counters
                job.updated_at = utc_now()
                if consume_feedback:
                    await _consume_pending_feedback(session, job)
        return await self.get_analysis(analysis_id)

    async def transition(
        self,
        analysis_id: UUID,
        status: PersistentAnalysisStatus,
        *,
        progress: dict[str, Any] | None = None,
        counters: dict[str, Any] | None = None,
        state: dict[str, Any] | None = None,
        lease: Any | None = None,
        consume_feedback: bool = False,
    ) -> PersistentAnalysisStatus:
        safe_progress = (
            sanitize_public_mapping(progress) if progress is not None else None
        )
        safe_counters = (
            sanitize_public_mapping(counters, counters=True)
            if counters is not None
            else None
        )
        async with self.sessions.begin() as session:
            row = await self._get_row(session, analysis_id, for_update=True)
            _assert_active_lease(row, lease)
            if status == row.status:
                if state is not None:
                    row.state = state
                if consume_feedback:
                    await _consume_pending_feedback(session, row)
                return row.status
            validate_status_transition(row.status, status)
            row.status = status
            row.updated_at = utc_now()
            if safe_progress is not None:
                row.progress = safe_progress
            if safe_counters is not None:
                row.counters = safe_counters
            if state is not None:
                row.state = state
            if consume_feedback:
                await _consume_pending_feedback(session, row)
            return row.status

    async def submit_feedback(
        self,
        analysis_id: UUID,
        *,
        action: Literal["accept", "revise"],
        comment: str | None = None,
    ) -> FeedbackResult:
        if action not in {"accept", "revise"}:
            raise ValueError("feedback action must be accept or revise")
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
            if row.pending_feedback_action is not None:
                raise AnalysisConflictError("Another feedback command is still pending.")
            if action == "revise":
                if row.revision_count >= 1:
                    raise AnalysisConflictError(
                        "The single allowed report revision has already been used."
                    )
                row.revision_count += 1
                row.status = PersistentAnalysisStatus.REVISING
            row.pending_feedback_action = action
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
                fingerprint=row.fingerprint,
                processed_at=(
                    _aware(row.processed_at) if row.processed_at is not None else None
                ),
                created_at=_aware(row.created_at),
            )

    async def complete_accept(
        self,
        analysis_id: UUID,
        *,
        state: dict[str, Any],
        counters: dict[str, Any],
        events: tuple[tuple[int, str, dict[str, Any]], ...],
        lease: Any,
    ) -> StoredAnalysis:
        """Atomically persist accepted graph state/events and the terminal status."""
        safe_counters = sanitize_public_mapping(counters, counters=True)
        async with self.sessions.begin() as session:
            row = await self._get_row(session, analysis_id, for_update=True)
            _assert_active_lease(row, lease)
            if (
                row.status is not PersistentAnalysisStatus.REVIEW_READY
                or row.pending_feedback_action != "accept"
            ):
                raise AnalysisConflictError("Analysis has no pending acceptance.")
            for source_sequence, event_type, data in events:
                event_type = validate_event_type(event_type)
                dedupe_key = f"graph:{source_sequence}"
                existing = await session.scalar(
                    select(AnalysisEventRow).where(
                        AnalysisEventRow.analysis_id == analysis_id,
                        AnalysisEventRow.dedupe_key == dedupe_key,
                    )
                )
                if existing is not None:
                    _deduped_event(existing, event_type, sanitize_public_mapping(data))
                    continue
                safe_data = sanitize_public_mapping(data)
                session.add(
                    AnalysisEventRow(
                        analysis_id=analysis_id,
                        sequence=row.next_event_sequence,
                        event_type=event_type,
                        data=safe_data,
                        dedupe_key=dedupe_key,
                    )
                )
                row.next_event_sequence += 1
            validate_status_transition(row.status, PersistentAnalysisStatus.COMPLETED)
            row.status = PersistentAnalysisStatus.COMPLETED
            row.state = state
            row.counters = safe_counters
            row.updated_at = utc_now()
            await _consume_pending_feedback(session, row)
        return await self.get_analysis(analysis_id)

    async def fail_safe(
        self,
        analysis_id: UUID,
        failure: PublicFailure,
        *,
        lease: Any | None = None,
    ) -> StoredAnalysis:
        failure = canonical_public_failure(failure)
        async with self.sessions.begin() as session:
            row = await self._get_row(session, analysis_id, for_update=True)
            _assert_active_lease(row, lease)
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

    async def active_snapshot_paths(self) -> set[Path]:
        """Return snapshot roots still referenced by nonterminal analyses."""
        async with self.sessions() as session:
            states = (
                await session.scalars(
                    select(AnalysisJobRow.state).where(
                        AnalysisJobRow.status.not_in(TERMINAL_STATUSES)
                    )
                )
            ).all()
        paths: set[Path] = set()
        for state in states:
            snapshot = state.get("snapshot") if isinstance(state, dict) else None
            root = snapshot.get("root_path") if isinstance(snapshot, dict) else None
            if isinstance(root, str) and root:
                paths.add(Path(root).resolve())
        return paths

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


def _assert_active_lease(row: AnalysisJobRow, lease: Any | None) -> None:
    if lease is None:
        return
    if (
        getattr(lease, "analysis_id", None) != row.id
        or getattr(lease, "worker_id", None) != row.lease_worker_id
        or getattr(lease, "attempt_count", None) != row.attempt_count
        or row.lease_expires_at is None
        or _aware(row.lease_expires_at) <= utc_now()
    ):
        raise AnalysisConflictError("Analysis lease is stale or owned by another worker.")


def _deduped_event(
    row: AnalysisEventRow,
    event_type: str,
    safe_data: dict[str, Any],
) -> StoredEvent:
    stored = _stored_event(row)
    if stored.event_type != event_type or stored.data != safe_data:
        raise AnalysisConflictError("Event dedupe key conflicts with stored data.")
    return stored


def _stored_event(row: AnalysisEventRow) -> StoredEvent:
    return StoredEvent(
        sequence=row.sequence,
        event_type=row.event_type,
        data=safe_public_mapping(row.data),
        created_at=_aware(row.created_at),
    )


def _stored_report(row: AnalysisReportVersionRow) -> StoredReport:
    return StoredReport(
        version=row.version,
        report=AnalysisReport.model_validate(row.report),
        state=dict(row.state),
        created_at=_aware(row.created_at),
    )


async def _consume_pending_feedback(
    session: AsyncSession, row: AnalysisJobRow
) -> None:
    action = row.pending_feedback_action
    if action is None:
        raise AnalysisConflictError("Analysis has no pending feedback command.")
    feedback = await session.scalar(
        select(AnalysisFeedbackCommandRow)
        .where(
            AnalysisFeedbackCommandRow.analysis_id == row.id,
            AnalysisFeedbackCommandRow.action == action,
            AnalysisFeedbackCommandRow.processed_at.is_(None),
        )
        .order_by(AnalysisFeedbackCommandRow.id.desc())
        .limit(1)
    )
    if feedback is None:
        raise AnalysisConflictError("Pending feedback command is unavailable.")
    feedback.processed_at = utc_now()
    row.pending_feedback_action = None
