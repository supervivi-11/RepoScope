from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agent import AnalysisReport
from app.analysis import AnalysisRepository, PersistentAnalysisStatus, StoredEvent
from app.analysis.failures import PublicFailure
from app.api import SSESettings, StaticDemoStore, create_app, iter_sse_events
from app.db import metadata


@pytest.fixture
async def repository(tmp_path: Path) -> AsyncIterator[AnalysisRepository]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'api.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    yield AnalysisRepository(sessions)
    await engine.dispose()


@pytest.fixture
async def client(repository: AnalysisRepository) -> AsyncIterator[AsyncClient]:
    application = create_app(repository=repository)
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://testserver"
    ) as active:
        yield active


def _report(summary: str = "Static report") -> AnalysisReport:
    return AnalysisReport(
        outcome="insufficient_evidence",
        issue_summary=summary,
        observed_behavior="Observed",
        expected_behavior="Expected",
        uncertainties=("Unknown",),
        confidence=0.2,
    )


async def _make_review_ready(repository: AnalysisRepository, analysis_id) -> None:
    for status in (
        PersistentAnalysisStatus.INGESTING,
        PersistentAnalysisStatus.INDEXING,
        PersistentAnalysisStatus.INVESTIGATING,
        PersistentAnalysisStatus.REVIEW_READY,
    ):
        await repository.transition(analysis_id, status)


@pytest.mark.anyio
async def test_create_analysis_returns_202_then_200_idempotent_replay(
    client: AsyncClient,
) -> None:
    """Breaks if client retries create duplicate analysis identities."""
    payload = {"repo_url": "https://github.com/owner/repo", "issue_number": 9}

    created = await client.post(
        "/api/v1/analyses", json=payload, headers={"Idempotency-Key": "create-9"}
    )
    replayed = await client.post(
        "/api/v1/analyses", json=payload, headers={"Idempotency-Key": "create-9"}
    )

    assert created.status_code == 202
    assert replayed.status_code == 200
    assert created.json() == replayed.json()
    assert created.json()["status"] == "QUEUED"


@pytest.mark.anyio
async def test_create_validates_github_input_and_idempotency_header(
    client: AsyncClient,
) -> None:
    """Breaks if arbitrary URLs or unsafe idempotency keys reach storage."""
    invalid_url = await client.post(
        "/api/v1/analyses",
        json={"repo_url": "https://example.com/owner/repo", "issue_number": 1},
    )
    invalid_key = await client.post(
        "/api/v1/analyses",
        json={"repo_url": "https://github.com/owner/repo", "issue_number": 1},
        headers={"Idempotency-Key": "contains spaces"},
    )

    assert invalid_url.status_code == 422
    assert invalid_key.status_code == 422


@pytest.mark.anyio
async def test_request_validation_error_never_echoes_invalid_credentials(
    client: AsyncClient,
) -> None:
    """Breaks if FastAPI's validation detail reflects hostile request input."""
    secret = "sk-a1b2"
    response = await client.post(
        "/api/v1/analyses",
        json={
            "repo_url": f"https://user:{secret}@github.com/owner/repo",
            "issue_number": 1,
        },
    )

    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "validation_error",
            "message": "Request validation failed.",
        }
    }
    assert secret not in response.text
    assert "user:" not in response.text


@pytest.mark.anyio
async def test_request_limit_counts_actual_chunked_bytes_and_ignores_false_length(
    client: AsyncClient,
) -> None:
    """Breaks if body limits trust Content-Length instead of received bytes."""
    oversized = (
        b'{"repo_url":"https://github.com/owner/repo","issue_number":1,'
        b'"padding":"' + (b"x" * 20_000) + b'"}'
    )

    async def chunks():
        for offset in range(0, len(oversized), 1_000):
            yield oversized[offset : offset + 1_000]

    falsely_low = await client.post(
        "/api/v1/analyses",
        content=chunks(),
        headers={"Content-Type": "application/json", "Content-Length": "1"},
    )
    chunked = await client.post(
        "/api/v1/analyses",
        content=chunks(),
        headers={"Content-Type": "application/json"},
    )

    for response in (falsely_low, chunked):
        assert response.status_code == 413
        assert response.json() == {
            "error": {
                "code": "request_too_large",
                "message": "Request body is too large.",
            }
        }


@pytest.mark.anyio
async def test_get_analysis_returns_safe_state_report_history_and_errors(
    repository: AnalysisRepository, client: AsyncClient
) -> None:
    """Breaks if Task 6 cannot reconstruct current and original report state."""
    analysis = await repository.create_analysis(
        repo_url="https://github.com/owner/repo", issue_number=10
    )
    await repository.save_report(
        analysis.analysis_id, _report("Original"), state={"phase": "review"}
    )
    await repository.save_report(
        analysis.analysis_id, _report("Revised"), state={"phase": "revision"}
    )

    response = await client.get(f"/api/v1/analyses/{analysis.analysis_id}")
    missing = await client.get("/api/v1/analyses/00000000-0000-0000-0000-000000000000")
    malformed = await client.get("/api/v1/analyses/not-a-uuid")

    assert response.status_code == 200
    body = response.json()
    assert body["analysis_id"] == str(analysis.analysis_id)
    assert body["current_report"]["issue_summary"] == "Revised"
    assert [item["report"]["issue_summary"] for item in body["report_history"]] == [
        "Original",
        "Revised",
    ]
    assert body["error"] is None
    assert missing.status_code == 404
    assert malformed.status_code == 422


@pytest.mark.anyio
async def test_feedback_endpoint_enforces_comment_state_conflict_and_idempotency(
    repository: AnalysisRepository, client: AsyncClient
) -> None:
    """Breaks if API feedback can bypass Task 4's single revision contract."""
    analysis = await repository.create_analysis(
        repo_url="https://github.com/owner/repo", issue_number=11
    )
    await _make_review_ready(repository, analysis.analysis_id)

    blank = await client.post(
        f"/api/v1/analyses/{analysis.analysis_id}/feedback",
        json={"action": "revise", "comment": "   "},
    )
    revised = await client.post(
        f"/api/v1/analyses/{analysis.analysis_id}/feedback",
        json={"action": "revise", "comment": "Inspect the parser."},
    )
    duplicate = await client.post(
        f"/api/v1/analyses/{analysis.analysis_id}/feedback",
        json={"action": "revise", "comment": "Inspect the parser."},
    )
    conflict = await client.post(
        f"/api/v1/analyses/{analysis.analysis_id}/feedback",
        json={"action": "accept"},
    )

    assert blank.status_code == 422
    assert revised.status_code == 202
    assert duplicate.status_code == 200
    assert revised.json()["status"] == "REVISING"
    assert conflict.status_code == 409


@pytest.mark.anyio
async def test_sse_replays_after_last_event_id_and_closes_on_terminal(
    repository: AnalysisRepository, client: AsyncClient
) -> None:
    """Breaks if SSE reconnect duplicates acknowledged events or never closes."""
    analysis = await repository.create_analysis(
        repo_url="https://github.com/owner/repo", issue_number=12
    )
    await repository.append_event(
        analysis.analysis_id, "analysis_queued", {"status": "QUEUED"}
    )
    await repository.append_event(
        analysis.analysis_id, "ingestion_started", {"status": "INGESTING"}
    )
    await repository.fail_safe(
        analysis.analysis_id,
        PublicFailure("analysis_failed", "Analysis failed safely.", 500),
    )

    response = await client.get(
        f"/api/v1/analyses/{analysis.analysis_id}/events",
        headers={"Last-Event-ID": "1"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "id: 1\n" not in response.text
    assert "id: 2\nevent: ingestion_started\n" in response.text
    assert "id: 3\nevent: analysis_failed\n" in response.text
    assert "data: {" in response.text


@pytest.mark.anyio
async def test_sse_heartbeat_and_disconnect_stop_without_holding_a_session(
    repository: AnalysisRepository,
) -> None:
    """Breaks if idle clients get no keepalive or cancellation leaks DB sessions."""
    analysis = await repository.create_analysis(
        repo_url="https://github.com/owner/repo", issue_number=13
    )

    class ConnectedRequest:
        async def is_disconnected(self):
            return False

    stream = iter_sse_events(
        repository,
        analysis.analysis_id,
        request=ConnectedRequest(),
        after_sequence=0,
        settings=SSESettings(poll_interval=0.001, heartbeat_interval=0),
    )
    assert await anext(stream) == ": heartbeat\n\n"
    await stream.aclose()

    class DisconnectedRequest:
        async def is_disconnected(self):
            return True

    disconnected = iter_sse_events(
        repository,
        analysis.analysis_id,
        request=DisconnectedRequest(),
        after_sequence=0,
        settings=SSESettings(poll_interval=0, heartbeat_interval=0),
    )
    with pytest.raises(StopAsyncIteration):
        await anext(disconnected)


@pytest.mark.anyio
async def test_sse_terminal_status_and_events_share_one_replay_snapshot() -> None:
    """Breaks if a terminal commit can land between replay and status reads."""
    terminal_event = StoredEvent(
        sequence=1,
        event_type="report_accepted",
        data={"status": "COMPLETED"},
        created_at=datetime(2026, 8, 30, tzinfo=UTC),
    )

    class RacingRepository:
        async def list_events(self, analysis_id, *, after_sequence):
            return ()

        async def get_analysis(self, analysis_id):
            return SimpleNamespace(status=PersistentAnalysisStatus.COMPLETED)

        async def event_stream_snapshot(self, analysis_id, *, after_sequence):
            return (terminal_event,), PersistentAnalysisStatus.COMPLETED

    class ConnectedRequest:
        async def is_disconnected(self):
            return False

    stream = iter_sse_events(
        RacingRepository(),  # type: ignore[arg-type]
        analysis_id="race",  # type: ignore[arg-type]
        request=ConnectedRequest(),
        after_sequence=0,
        settings=SSESettings(poll_interval=0, heartbeat_interval=60),
    )

    assert await anext(stream) == (
        "id: 1\n"
        "event: report_accepted\n"
        'data: {"status":"COMPLETED"}\n\n'
    )
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


@pytest.mark.anyio
async def test_demo_cases_are_versioned_static_artifacts_and_health_stays_exact(
    client: AsyncClient,
) -> None:
    """Breaks if public demos call a model or health is coupled to database readiness."""
    demos = await client.get("/api/v1/demo-cases")
    health = await client.get("/health")

    assert demos.status_code == 200
    assert demos.json()["version"] == "1"
    assert len(demos.json()["cases"]) == 3
    assert all(case["report"] and case["events"] for case in demos.json()["cases"])
    assert health.json() == {"status": "ok", "service": "reposcope-api"}


@pytest.mark.anyio
async def test_database_readiness_is_separate_and_internal_errors_are_stable() -> None:
    """Breaks if readiness weakens health or API exceptions expose stack/secret text."""
    class ExplodingRepository:
        async def create_analysis(self, **kwargs):
            raise RuntimeError("sk-do-not-leak")

    async def unavailable() -> bool:
        return False

    application = create_app(
        repository=ExplodingRepository(), database_probe=unavailable
    )
    async with AsyncClient(
        transport=ASGITransport(app=application, raise_app_exceptions=False),
        base_url="http://testserver",
    ) as local:
        health = await local.get("/health")
        ready = await local.get("/ready")
        failed = await local.post(
            "/api/v1/analyses",
            json={"repo_url": "https://github.com/owner/repo", "issue_number": 1},
        )

    assert health.status_code == 200
    assert ready.status_code == 503
    assert failed.status_code == 500
    assert failed.json() == {
        "error": {"code": "internal_error", "message": "Analysis failed safely."}
    }
    assert "sk-do-not-leak" not in failed.text
