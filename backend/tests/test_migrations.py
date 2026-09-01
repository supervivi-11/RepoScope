from alembic import command
from alembic.config import Config
from sqlalchemy.dialects import postgresql

from app.db import metadata
from app.analysis.models import AnalysisEventRow, AnalysisJobRow


def test_alembic_environment_uses_application_metadata(tmp_path, monkeypatch) -> None:
    """Breaks if Alembic cannot load application metadata from DATABASE_URL."""
    database_url = f"sqlite:///{tmp_path / 'reposcope.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    config = Config("backend/alembic.ini")

    command.current(config)

    assert set(metadata.tables) == {
        "analysis_jobs",
        "analysis_events",
        "analysis_report_versions",
        "analysis_feedback_commands",
    }


def test_persistence_schema_uses_uuid_timestamps_jsonb_and_unique_sequences() -> None:
    """Breaks if durable analysis records lose their PostgreSQL safety constraints."""
    jobs = AnalysisJobRow.__table__
    events = AnalysisEventRow.__table__

    assert jobs.c.id.type.as_uuid is True
    assert jobs.c.created_at.type.timezone is True
    assert jobs.c.updated_at.type.timezone is True
    assert jobs.c.idempotency_key.unique is True
    assert events.c.created_at.type.timezone is True
    assert "JSONB" in str(jobs.c.progress.type.compile(dialect=postgresql.dialect()))
    assert any(
        constraint.name == "uq_analysis_events_analysis_sequence"
        for constraint in events.constraints
    )


def test_offline_postgresql_migration_creates_all_analysis_tables(
    monkeypatch, capsys
) -> None:
    """Breaks if a fresh PostgreSQL deployment cannot create Task 5 storage."""
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+psycopg://reposcope:reposcope@localhost:5432/reposcope",
    )
    config = Config("backend/alembic.ini")

    command.upgrade(config, "head", sql=True)

    sql = capsys.readouterr().out
    assert "CREATE TABLE analysis_jobs" in sql
    assert "CREATE TABLE analysis_events" in sql
    assert "CREATE TABLE analysis_report_versions" in sql
    assert "CREATE TABLE analysis_feedback_commands" in sql
