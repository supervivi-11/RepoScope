from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypedDict
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agent import AnalysisState, IssueIdentity, RepositoryIdentity
from app.analysis import (
    AnalysisConflictError,
    AnalysisRepository,
    PersistentAnalysisStatus,
)
from app.analysis import checkpoints as checkpoint_module
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
            replace(claimed, worker_id="worker-b"),
            now=NOW + timedelta(seconds=5),
        )

    heartbeat = await queue.heartbeat(
        claimed, now=NOW + timedelta(seconds=5)
    )
    assert heartbeat.lease_expires_at == NOW + timedelta(seconds=35)

    await queue.release(heartbeat, now=NOW + timedelta(seconds=6))
    reclaimed = await queue.claim_next(
        worker_id="worker-b", now=NOW + timedelta(seconds=6)
    )
    assert reclaimed is not None
    await queue.finish(
        reclaimed,
        status=PersistentAnalysisStatus.FAILED,
        now=NOW + timedelta(seconds=7),
    )
    assert (await repository.get_analysis(reclaimed.analysis_id)).status is (
        PersistentAnalysisStatus.FAILED
    )
    assert await queue.claim_next(
        worker_id="worker-c", now=NOW + timedelta(minutes=5)
    ) is None


@pytest.mark.anyio
async def test_queue_finish_rejects_illegal_domain_transition(
    repository: AnalysisRepository,
) -> None:
    """Breaks if the lease queue can jump a queued job directly to completed."""
    analysis = await repository.create_analysis(
        repo_url="https://github.com/owner/repo", issue_number=18
    )
    queue = PostgresJobQueue(repository.sessions)
    claim = await queue.claim_next(worker_id="worker-transition")
    assert claim is not None

    with pytest.raises(AnalysisConflictError):
        await queue.finish(
            claim,
            status=PersistentAnalysisStatus.COMPLETED,
        )

    assert (await repository.get_analysis(analysis.analysis_id)).status is (
        PersistentAnalysisStatus.QUEUED
    )


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
    """Breaks if cross-process bootstrap is unlocked or attempts share a thread."""
    calls: dict[str, object] = {}
    lifecycle: list[str] = []

    class FakeCursor:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def execute(self, sql, parameters):
            if "pg_advisory_lock" in sql:
                lifecycle.append("lock")
            elif "pg_advisory_unlock" in sql:
                lifecycle.append("unlock")

    class FakeConnection:
        def __init__(self) -> None:
            self.cursor_instance = FakeCursor()

        def cursor(self):
            return self.cursor_instance

    class FakeSaver:
        def __init__(self) -> None:
            self.conn = FakeConnection()

        async def setup(self) -> None:
            lifecycle.append("setup")

        async def aget_tuple(self, config):
            return None

    fake_saver = FakeSaver()

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

    async with factory.open(analysis_id, attempt_count=3) as checkpoint:
        lifecycle.append("use")
        assert checkpoint.checkpointer is fake_saver
        assert checkpoint.config == {
            "configurable": {
                "thread_id": f"{analysis_id}:attempt:3",
                "checkpoint_ns": "",
            }
        }

    async with factory.open(uuid4()):
        lifecycle.append("reuse")

    assert calls["conn_string"] == (
        "postgresql://reposcope:secret@postgres:5432/reposcope"
    )
    assert calls["pipeline"] is False
    assert calls["serde"].pickle_fallback is False
    assert lifecycle == ["lock", "setup", "unlock", "use", "reuse"]


@pytest.mark.anyio
async def test_postgres_checkpoint_setup_unlocks_same_session_when_setup_fails(
    monkeypatch,
) -> None:
    """Breaks if a failed library bootstrap strands the global advisory lock."""
    calls: list[tuple[str, int]] = []

    class SetupFailure(RuntimeError):
        pass

    class FakeCursor:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def execute(self, sql, parameters):
            operation = "unlock" if "unlock" in sql else "lock"
            calls.append((operation, id(self)))

    class FakeConnection:
        def __init__(self) -> None:
            self.cursor_instance = FakeCursor()

        def cursor(self):
            return self.cursor_instance

    class FakeSaver:
        def __init__(self) -> None:
            self.conn = FakeConnection()

        async def setup(self) -> None:
            calls.append(("setup", id(self.conn.cursor_instance)))
            raise SetupFailure()

    @asynccontextmanager
    async def fake_open(*args, **kwargs):
        yield FakeSaver()

    monkeypatch.setattr(
        "app.analysis.checkpoints.AsyncPostgresSaver.from_conn_string",
        fake_open,
    )
    factory = PostgresCheckpointFactory(
        "postgresql://reposcope:secret@postgres:5432/reposcope"
    )

    with pytest.raises(SetupFailure):
        async with factory.open(uuid4()):
            pass

    assert [operation for operation, _ in calls] == ["lock", "setup", "unlock"]
    assert len({session_id for _, session_id in calls}) == 1


class _HandoffState(TypedDict):
    value: int
    feedback: str


@pytest.mark.anyio
async def test_attempt_handoff_clones_checkpoint_and_pending_interrupt_once() -> None:
    """Breaks if reclaim loses interrupt state or follows later stale writes."""
    analysis_id = uuid4()
    saver = InMemorySaver()
    builder = StateGraph(_HandoffState)
    builder.add_node("increment", lambda state: {"value": state["value"] + 1})

    def pause(state):
        return {"feedback": interrupt({"value": state["value"]})}

    builder.add_node("pause", pause)
    builder.add_node("finish", lambda state: {"value": state["value"] + 10})
    builder.add_edge(START, "increment")
    builder.add_edge("increment", "pause")
    builder.add_edge("pause", "finish")
    builder.add_edge("finish", END)
    graph = builder.compile(checkpointer=saver)
    source_config = {
        "configurable": {
            "thread_id": f"{analysis_id}:attempt:1",
            "checkpoint_ns": "",
        }
    }
    await graph.ainvoke({"value": 0, "feedback": ""}, config=source_config)
    source = await saver.aget_tuple(source_config)
    assert source is not None

    target_config = await checkpoint_module.prepare_attempt_checkpoint(
        saver,
        analysis_id,
        attempt_count=2,
    )
    target = await saver.aget_tuple(target_config)
    assert target is not None
    assert target.checkpoint == source.checkpoint
    assert target.metadata == source.metadata
    assert target.pending_writes == source.pending_writes

    resumed = await graph.ainvoke(Command(resume="active"), config=target_config)
    target_before_stale = await saver.aget_tuple(target_config)
    stale = await graph.ainvoke(Command(resume="stale"), config=source_config)
    target_after_stale = await saver.aget_tuple(target_config)

    assert resumed == {"value": 11, "feedback": "active"}
    assert stale == {"value": 11, "feedback": "stale"}
    assert target_after_stale == target_before_stale


@pytest.mark.anyio
async def test_attempt_handoff_does_not_publish_checkpoint_before_pending_writes() -> None:
    """Breaks if a write failure leaves a target that retries mistake for complete."""
    analysis_id = uuid4()
    saver = InMemorySaver()
    builder = StateGraph(_HandoffState)

    def pause(state):
        return {"feedback": interrupt({"value": state["value"]})}

    builder.add_node("pause", pause)
    builder.add_edge(START, "pause")
    builder.add_edge("pause", END)
    graph = builder.compile(checkpointer=saver)
    source_config = checkpoint_module.attempt_checkpoint_config(
        analysis_id,
        attempt_count=1,
    )
    target_config = checkpoint_module.attempt_checkpoint_config(
        analysis_id,
        attempt_count=2,
    )
    await graph.ainvoke({"value": 0, "feedback": ""}, config=source_config)
    source = await saver.aget_tuple(source_config)
    assert source is not None and source.pending_writes

    class FailFirstWriteSaver:
        failed = False

        async def aget_tuple(self, config):
            return await saver.aget_tuple(config)

        async def aput(self, *args, **kwargs):
            return await saver.aput(*args, **kwargs)

        async def aput_writes(self, *args, **kwargs):
            if not self.failed:
                self.failed = True
                raise RuntimeError("simulated pending-write failure")
            return await saver.aput_writes(*args, **kwargs)

    flaky = FailFirstWriteSaver()
    with pytest.raises(RuntimeError, match="pending-write failure"):
        await checkpoint_module.prepare_attempt_checkpoint(
            flaky,
            analysis_id,
            attempt_count=2,
        )

    assert await saver.aget_tuple(target_config) is None
    await checkpoint_module.prepare_attempt_checkpoint(
        flaky,
        analysis_id,
        attempt_count=2,
    )
    target = await saver.aget_tuple(target_config)
    assert target is not None
    assert target.pending_writes == source.pending_writes


@pytest.mark.anyio
@pytest.mark.parametrize("spoof_target", [True, False])
async def test_attempt_handoff_rejects_foreign_source_or_destination_ownership(
    spoof_target: bool,
) -> None:
    """Breaks if a misrouted saver can clone or select another analysis thread."""
    requested_id = uuid4()
    foreign_id = uuid4()
    saver = InMemorySaver()
    builder = StateGraph(_HandoffState)
    builder.add_node("finish", lambda state: {"value": state["value"] + 1})
    builder.add_edge(START, "finish")
    builder.add_edge("finish", END)
    graph = builder.compile(checkpointer=saver)
    foreign_config = {
        "configurable": {
            "thread_id": f"{foreign_id}:attempt:1",
            "checkpoint_ns": "",
        }
    }
    await graph.ainvoke({"value": 0, "feedback": ""}, config=foreign_config)
    foreign = await saver.aget_tuple(foreign_config)
    assert foreign is not None

    class MisroutingSaver:
        async def aget_tuple(self, config):
            is_target = config["configurable"]["thread_id"].endswith(":attempt:2")
            if is_target:
                return foreign if spoof_target else None
            return foreign

        async def aput(self, *args, **kwargs):
            raise AssertionError("foreign checkpoint must not be cloned")

        async def aput_writes(self, *args, **kwargs):
            raise AssertionError("foreign writes must not be cloned")

    with pytest.raises(RuntimeError, match="ownership"):
        await checkpoint_module.prepare_attempt_checkpoint(
            MisroutingSaver(),
            requested_id,
            attempt_count=2,
        )
