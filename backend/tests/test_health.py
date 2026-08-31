import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.main import app
from app.readiness import database_schema_ready


@pytest.mark.anyio
async def test_health_returns_validated_service_status() -> None:
    """Breaks if the health contract loses its validated response schema."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "reposcope-api"}
    health_schema = app.openapi()["paths"]["/health"]["get"]["responses"]["200"]
    assert health_schema["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/HealthResponse"
    }


@pytest.mark.anyio
async def test_readiness_requires_expected_alembic_revision(tmp_path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'ready.db'}")
    async with engine.begin() as connection:
        await connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
        await connection.execute(
            text("INSERT INTO alembic_version VALUES ('unexpected')")
        )
    assert await database_schema_ready(engine) is False

    async with engine.begin() as connection:
        await connection.execute(text("UPDATE alembic_version SET version_num='20260830_0001'"))
        await connection.execute(text("CREATE TABLE analysis_jobs (id VARCHAR(36))"))
        await connection.execute(text("CREATE TABLE analysis_events (id INTEGER)"))
        await connection.execute(text("CREATE TABLE analysis_report_versions (id INTEGER)"))
        await connection.execute(text("CREATE TABLE analysis_feedback_commands (id INTEGER)"))
    assert await database_schema_ready(engine) is True
    await engine.dispose()
