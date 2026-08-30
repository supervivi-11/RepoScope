"""Add durable analysis jobs, events, reports, and feedback.

Revision ID: 20260830_0001
Revises:
Create Date: 2026-08-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260830_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


STATUS_VALUES = (
    "QUEUED",
    "INGESTING",
    "INDEXING",
    "INVESTIGATING",
    "REVIEW_READY",
    "REVISING",
    "COMPLETED",
    "FAILED",
)


def upgrade() -> None:
    status_type = sa.Enum(
        *STATUS_VALUES,
        name="persistent_analysis_status",
        native_enum=False,
        create_constraint=True,
        length=32,
    )
    op.create_table(
        "analysis_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("repo_url", sa.String(length=300), nullable=False),
        sa.Column("issue_number", sa.Integer(), nullable=False),
        sa.Column("status", status_type, nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=True),
        sa.Column("progress", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("counters", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("state", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.String(length=500), nullable=True),
        sa.Column("lease_worker_id", sa.String(length=200), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("revision_count", sa.Integer(), nullable=False),
        sa.Column("next_event_sequence", sa.Integer(), nullable=False),
        sa.Column("next_report_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index("ix_analysis_jobs_status", "analysis_jobs", ["status"])
    op.create_index(
        "ix_analysis_jobs_lease_expires_at", "analysis_jobs", ["lease_expires_at"]
    )
    op.create_table(
        "analysis_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("analysis_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["analysis_id"], ["analysis_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "analysis_id", "sequence", name="uq_analysis_events_analysis_sequence"
        ),
    )
    op.create_index(
        "ix_analysis_events_analysis_id", "analysis_events", ["analysis_id"]
    )
    op.create_table(
        "analysis_report_versions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("analysis_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("report", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("state", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["analysis_id"], ["analysis_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "analysis_id", "version", name="uq_analysis_report_versions_version"
        ),
    )
    op.create_index(
        "ix_analysis_report_versions_analysis_id",
        "analysis_report_versions",
        ["analysis_id"],
    )
    op.create_table(
        "analysis_feedback_commands",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("analysis_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(length=20), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["analysis_id"], ["analysis_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "analysis_id", "fingerprint", name="uq_analysis_feedback_fingerprint"
        ),
    )
    op.create_index(
        "ix_analysis_feedback_commands_analysis_id",
        "analysis_feedback_commands",
        ["analysis_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_analysis_feedback_commands_analysis_id",
        table_name="analysis_feedback_commands",
    )
    op.drop_table("analysis_feedback_commands")
    op.drop_index(
        "ix_analysis_report_versions_analysis_id",
        table_name="analysis_report_versions",
    )
    op.drop_table("analysis_report_versions")
    op.drop_index("ix_analysis_events_analysis_id", table_name="analysis_events")
    op.drop_table("analysis_events")
    op.drop_index("ix_analysis_jobs_lease_expires_at", table_name="analysis_jobs")
    op.drop_index("ix_analysis_jobs_status", table_name="analysis_jobs")
    op.drop_table("analysis_jobs")
