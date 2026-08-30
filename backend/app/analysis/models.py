from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
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
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    analysis_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("analysis_jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
