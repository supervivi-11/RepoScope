from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agent import AnalysisState, IssueIdentity, RepositoryIdentity
from app.analysis import (
    AnalysisConflictError,
    AnalysisRepository,
    PersistentAnalysisStatus,
)
from app.analysis.checkpoints import (
    PostgresCheckpointFactory,
    build_checkpoint_serializer,
)
from app.analysis.queue import PostgresJobQueue, build_claim_statement
from app.db import metadata


NOW = datetime(2026, 8, 30, 8, 0, tzinfo=UTC)


@pytest.fixture
async def repository(tmp_path: Path) -> AsyncIterator[AnalysisRepository]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'queue.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    yield AnalysisRepository(sessions)
    await engine.dispose()


def test_claim_statement_uses_postgresql_skip_locked_and_expired_recovery() -> None:
    """Breaks if multiple workers can block each other or active leases are reclaimed."""
    sql = str(
        build_claim_statement(NOW)
        .compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    ).upper()

    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "LEASE_EXPIRES_AT" in sql
    assert "QUEUED" in sql
    assert "REVISING" in sql
    assert "ORDER BY" in sql


@pytest.mark.anyio
async def test_claim_is_atomic_excludes_active_lease_and_recovers_expired_job(
    repository: AnalysisRepository,
) -> None:
    """Breaks if duplicate delivery acquires a live lease or restart recovery loses work."""
    analysis = await repository.create_analysis(
        repo_url="https://github.com/owner/repo", issue_number=7
    )
    queue = PostgresJobQueue(repository.sessions, lease_duration=timedelta(seconds=30))

    first = await queue.claim_next(worker_id="worker-a", now=NOW)
    while_active = await queue.claim_next(
        worker_id="worker-b", now=NOW + timedelta(seconds=20)
    )
    recovered = await queue.claim_next(
        worker_id="worker-b", now=NOW + timedelta(seconds=31)
    )

    assert first is not None
    assert first.analysis_id == analysis.analysis_id
    assert first.attempt_count == 1
    assert while_active is None
    assert recovered is not None
    assert recovered.analysis_id == analysis.analysis_id
    assert recovered.worker_id == "worker-b"
    assert recovered.attempt_count == 2


@pytest.mark.anyio
async def test_heartbeat_release_and_terminal_transition_require_lease_owner(
    repository: AnalysisRepository,
) -> None:
    """Breaks if one worker can mutate another worker's durable lease."""
    await repository.create_analysis(
        repo_url="https://github.com/owner/repo", issue_number=8
    )
    queue = PostgresJobQueue(repository.sessions, lease_duration=timedelta(seconds=30))
    claimed = await queue.claim_next(worker_id="worker-a", now=NOW)
    assert claimed is not None

    with pytest.raises(AnalysisConflictError):
        await queue.heartbeat(
            claimed.analysis_id, worker_id="worker-b", now=NOW + timedelta(seconds=5)
        )

    heartbeat = await queue.heartbeat(
        claimed.analysis_id, worker_id="worker-a", now=NOW + timedelta(seconds=5)
    )
    assert heartbeat.lease_expires_at == NOW + timedelta(seconds=35)

    await queue.release(claimed.analysis_id, worker_id="worker-a")
    reclaimed = await queue.claim_next(
        worker_id="worker-b", now=NOW + timedelta(seconds=6)
    )
    assert reclaimed is not None
    await queue.finish(
        reclaimed.analysis_id,
        worker_id="worker-b",
        status=PersistentAnalysisStatus.FAILED,
    )
    assert (await repository.get_analysis(reclaimed.analysis_id)).status is (
        PersistentAnalysisStatus.FAILED
    )
    assert await queue.claim_next(
        worker_id="worker-c", now=NOW + timedelta(minutes=5)
    ) is None


def test_checkpoint_serializer_round_trips_allowlisted_task4_state_without_pickle() -> None:
    """Breaks if graph state requires unsafe arbitrary-object deserialization."""
    state = AnalysisState(
        analysis_id=str(uuid4()),
        repository=RepositoryIdentity(
            owner="owner", repository="repo", commit_sha="a" * 40
        ),
        issue=IssueIdentity(
            number=1,
            title="Parser fails",
            body="Observed failure",
            html_url="https://github.com/owner/repo/issues/1",
        ),
    )
    serializer = build_checkpoint_serializer()

    payload = serializer.dumps_typed(state)
    restored = serializer.loads_typed(payload)

    assert payload[0] == "msgpack"
    assert serializer.pickle_fallback is False
    assert restored == state


@pytest.mark.anyio
async def test_postgres_checkpoint_factory_keys_thread_and_uses_safe_serializer(
    monkeypatch,
) -> None:
    """Breaks if analyses share a checkpoint thread or production enables pickle."""
    calls: dict[str, object] = {}
    fake_saver = object()

    class FakeAsyncPostgresSaver:
        @classmethod
        @asynccontextmanager
        async def from_conn_string(cls, conn_string: str, **kwargs):
            calls["conn_string"] = conn_string
            calls.update(kwargs)
            yield fake_saver

    monkeypatch.setattr(
        "app.analysis.checkpoints.AsyncPostgresSaver", FakeAsyncPostgresSaver
    )
    analysis_id = uuid4()
    factory = PostgresCheckpointFactory(
        "postgresql+psycopg://reposcope:secret@postgres:5432/reposcope"
    )

    async with factory.open(analysis_id) as checkpoint:
        assert checkpoint.checkpointer is fake_saver
        assert checkpoint.config == {
            "configurable": {"thread_id": str(analysis_id)}
        }

    assert calls["conn_string"] == (
        "postgresql://reposcope:secret@postgres:5432/reposcope"
    )
    assert calls["pipeline"] is False
    assert calls["serde"].pickle_fallback is False
