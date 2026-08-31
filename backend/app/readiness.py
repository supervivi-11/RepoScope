from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine


EXPECTED_ALEMBIC_REVISION = "20260830_0001"
EXPECTED_TABLES = frozenset(
    {
        "analysis_jobs",
        "analysis_events",
        "analysis_report_versions",
        "analysis_feedback_commands",
    }
)


async def database_schema_ready(engine: AsyncEngine) -> bool:
    """Return true only when the migrated schema expected by this image exists."""
    try:
        async with engine.connect() as connection:
            tables = await connection.run_sync(
                lambda sync_connection: set(inspect(sync_connection).get_table_names())
            )
            if not EXPECTED_TABLES.issubset(tables) or "alembic_version" not in tables:
                return False
            revision = await connection.scalar(
                text("SELECT version_num FROM alembic_version LIMIT 1")
            )
            return revision == EXPECTED_ALEMBIC_REVISION
    except Exception:
        return False
