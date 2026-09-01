from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

from .domain import PersistentAnalysisStatus


def utc_now() -> datetime:
    return datetime.now(UTC)


JSON_VALUE = JSON().with_variant(JSONB(), "postgresql")
STATUS_TYPE = Enum(
    PersistentAnalysisStatus,
    name="persistent_analysis_status",
    native_enum=False,
    create_constraint=True,
    validate_strings=True,
    length=32,
)


class AnalysisJobRow(Base):
    __tablename__ = "analysis_jobs"
    __table_args__ = (
        CheckConstraint("issue_number > 0", name="ck_analysis_jobs_issue_positive"),
        CheckConstraint("attempt_count >= 0", name="ck_analysis_jobs_attempt_nonnegative"),
        CheckConstraint("revision_count >= 0", name="ck_analysis_jobs_revision_nonnegative"),
        CheckConstraint("next_event_sequence >= 1", name="ck_analysis_jobs_event_sequence_positive"),
        CheckConstraint("next_report_version >= 1", name="ck_analysis_jobs_report_version_positive"),
        CheckConstraint(
            "pending_feedback_action IS NULL OR pending_feedback_action IN ('accept', 'revise')",
            name="ck_analysis_jobs_pending_feedback_action",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    repo_url: Mapped[str] = mapped_column(String(300), nullable=False)
    issue_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[PersistentAnalysisStatus] = mapped_column(
        STATUS_TYPE, nullable=False, default=PersistentAnalysisStatus.QUEUED, index=True
    )
    idempotency_key: Mapped[str | None] = mapped_column(
        String(200), nullable=True, unique=True
    )
    progress: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, default=dict, nullable=False)
    counters: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, default=dict, nullable=False)
    state: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, default=dict, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    lease_worker_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    revision_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pending_feedback_action: Mapped[str | None] = mapped_column(
        String(20), nullable=True
    )
    next_event_sequence: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    next_report_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class AnalysisEventRow(Base):
    __tablename__ = "analysis_events"
    __table_args__ = (
        UniqueConstraint(
            "analysis_id",
            "sequence",
            name="uq_analysis_events_analysis_sequence",
        ),
        UniqueConstraint(
            "analysis_id",
            "dedupe_key",
            name="uq_analysis_events_dedupe_key",
        ),
        CheckConstraint("sequence >= 1", name="ck_analysis_events_sequence_positive"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    analysis_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("analysis_jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    dedupe_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class AnalysisReportVersionRow(Base):
    __tablename__ = "analysis_report_versions"
    __table_args__ = (
        UniqueConstraint(
            "analysis_id", "version", name="uq_analysis_report_versions_version"
        ),
        UniqueConstraint(
            "analysis_id", "result_key", name="uq_analysis_report_versions_result_key"
        ),
        CheckConstraint("version >= 1", name="ck_analysis_reports_version_positive"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    analysis_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("analysis_jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    result_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    report: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    state: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class AnalysisFeedbackCommandRow(Base):
    __tablename__ = "analysis_feedback_commands"
    __table_args__ = (
        UniqueConstraint(
            "analysis_id", "fingerprint", name="uq_analysis_feedback_fingerprint"
        ),
        CheckConstraint(
            "action IN ('accept', 'revise')",
            name="ck_analysis_feedback_action",
        ),
        CheckConstraint(
            "(action = 'accept' AND comment IS NULL) OR "
            "(action = 'revise' AND comment IS NOT NULL AND length(trim(comment)) > 0)",
            name="ck_analysis_feedback_comment",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    analysis_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("analysis_jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
