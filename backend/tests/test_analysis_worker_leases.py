from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import TypedDict

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agent import (
    AnalysisEvent,
    AnalysisPhase,
    AnalysisReport,
    AnalysisState,
    AnalysisStatus,
    IssueIdentity,
    RepositoryIdentity,
)
from app.analysis import (
    AnalysisConflictError,
    AnalysisRepository,
    AnalysisWorker,
    PersistentAnalysisStatus,
)
from app.analysis.checkpoints import CheckpointContext
from app.analysis.failures import PublicFailure
from app.analysis.queue import PostgresJobQueue
from app.analysis.models import utc_now
from app.db import metadata
from app.ingestion import GithubIssue, RepositorySnapshot


@pytest.fixture
async def repository(tmp_path: Path) -> AsyncIterator[AnalysisRepository]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'leases.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    yield AnalysisRepository(sessions)
    await engine.dispose()


def _report() -> AnalysisReport:
    return AnalysisReport(
        outcome="insufficient_evidence",
        issue_summary="Recovered report",
        observed_behavior="Observed",
        expected_behavior="Expected",
        uncertainties=("Unknown",),
        confidence=0.2,
    )


def _snapshot(tmp_path: Path) -> RepositorySnapshot:
    root = tmp_path / "recovered-snapshot"
    root.mkdir(exist_ok=True)
    (root / "module.py").write_text("value = 1\n", encoding="utf-8")
    return RepositorySnapshot.create(
        owner="owner",
        repository="repo",
        commit_sha="a" * 40,
        root_path=root,
        indexed_byte_count=10,
        created_at=datetime(2026, 8, 30, tzinfo=UTC),
        cleanup_after=timedelta(hours=24),
    )


def _snapshot_payload(snapshot: RepositorySnapshot) -> dict[str, object]:
    return {
        "owner": snapshot.owner,
        "repository": snapshot.repository,
        "commit_sha": snapshot.commit_sha,
        "root_path": str(snapshot.root_path),
        "indexed_byte_count": snapshot.indexed_byte_count,
        "created_at": snapshot.created_at.isoformat(),
        "cleanup_deadline": snapshot.cleanup_deadline.isoformat(),
    }


def _issue() -> IssueIdentity:
    return IssueIdentity(
        number=21,
        title="Parser issue",
        body="Parsing fails",
        html_url="https://github.com/owner/repo/issues/21",
    )


def _final_state(analysis_id: object) -> AnalysisState:
    return AnalysisState(
        analysis_id=str(analysis_id),
        repository=RepositoryIdentity(
            owner="owner", repository="repo", commit_sha="a" * 40
        ),
        issue=_issue(),
        phase=AnalysisPhase.REVIEW,
        status=AnalysisStatus.REVIEW_READY,
        report=_report(),
        events=(
            AnalysisEvent(
                sequence=1,
                phase=AnalysisPhase.REVIEW,
                status=AnalysisStatus.REVIEW_READY,
                kind="review_ready",
            ),
        ),
    )


class SeedState(TypedDict):
    seeded: bool


class MemoryCheckpointFactory:
    def __init__(self, saver: InMemorySaver) -> None:
        self.saver = saver

    @asynccontextmanager
    async def open(self, analysis_id):
        yield CheckpointContext(
            checkpointer=self.saver,
            config={"configurable": {"thread_id": str(analysis_id)}},
        )


async def _seed_checkpoint(saver: InMemorySaver, analysis_id: object) -> None:
    builder = StateGraph(SeedState)
    builder.add_node("seed", lambda state: {"seeded": True})
    builder.add_edge(START, "seed")
    builder.add_edge("seed", END)
    graph = builder.compile(checkpointer=saver)
    await graph.ainvoke(
        {"seeded": False},
        {"configurable": {"thread_id": str(analysis_id)}},
    )


@pytest.mark.anyio
async def test_reclaimed_attempt_fences_every_stale_worker_write_even_with_same_id(
    repository: AnalysisRepository,
) -> None:
    """Breaks if an expired attempt can append or transition after being reclaimed."""
    analysis = await repository.create_analysis(
        repo_url="https://github.com/owner/repo", issue_number=20
    )
    queue = PostgresJobQueue(repository.sessions, lease_duration=timedelta(seconds=30))
    started = utc_now()
    stale = await queue.claim_next(worker_id="worker-reused", now=started)
    assert stale is not None
    active = await queue.claim_next(
        worker_id="worker-reused", now=started + timedelta(seconds=31)
    )
    assert active is not None
    assert active.attempt_count == stale.attempt_count + 1

    with pytest.raises(AnalysisConflictError):
        await queue.heartbeat(stale)
    with pytest.raises(AnalysisConflictError):
        await repository.append_event(
            analysis.analysis_id, "stale_event", {"status": "stale"}, lease=stale
        )
    with pytest.raises(AnalysisConflictError):
        await repository.save_report(
            analysis.analysis_id, _report(), state={}, lease=stale, result_key="initial"
        )
    with pytest.raises(AnalysisConflictError):
        await repository.transition(
            analysis.analysis_id,
            PersistentAnalysisStatus.INGESTING,
            lease=stale,
        )
    with pytest.raises(AnalysisConflictError):
        await repository.fail_safe(
            analysis.analysis_id,
            PublicFailure("analysis_failed", "Analysis failed safely.", 500),
            lease=stale,
        )

    stored = await repository.get_analysis(analysis.analysis_id)
    assert stored.status is PersistentAnalysisStatus.QUEUED
    assert stored.report_history == ()
    assert await repository.list_events(analysis.analysis_id) == ()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "recovery_status",
    [
        PersistentAnalysisStatus.INGESTING,
        PersistentAnalysisStatus.INDEXING,
        PersistentAnalysisStatus.INVESTIGATING,
    ],
)
async def test_worker_recovers_active_stage_without_reingestion_and_uses_checkpoint_state(
    repository: AnalysisRepository,
    tmp_path: Path,
    recovery_status: PersistentAnalysisStatus,
) -> None:
    """Breaks if restart resolves a new commit or overwrites a real checkpoint."""
    calls: list[object] = []
    snapshot = _snapshot(tmp_path)
    analysis = await repository.create_analysis(
        repo_url="https://github.com/owner/repo", issue_number=21
    )
    queue = PostgresJobQueue(repository.sessions, lease_duration=timedelta(seconds=30))
    original_claim = await queue.claim_next(worker_id="worker-old")
    assert original_claim is not None

    durable_state = {
        "snapshot": _snapshot_payload(snapshot),
        "issue": _issue().model_dump(mode="json"),
    }
    await repository.transition(
        analysis.analysis_id,
        PersistentAnalysisStatus.INGESTING,
        state=durable_state,
        lease=original_claim,
    )
    if recovery_status in {
        PersistentAnalysisStatus.INDEXING,
        PersistentAnalysisStatus.INVESTIGATING,
    }:
        await repository.transition(
            analysis.analysis_id,
            PersistentAnalysisStatus.INDEXING,
            lease=original_claim,
        )
    if recovery_status is PersistentAnalysisStatus.INVESTIGATING:
        await repository.transition(
            analysis.analysis_id,
            PersistentAnalysisStatus.INVESTIGATING,
            lease=original_claim,
        )
    await queue.release(original_claim)
    recovered_claim = await queue.claim_next(worker_id="worker-new")
    assert recovered_claim is not None

    saver = InMemorySaver()
    if recovery_status is PersistentAnalysisStatus.INVESTIGATING:
        await _seed_checkpoint(saver, analysis.analysis_id)

    class NoReingestion:
        async def ingest(self, repo_url, issue_number):
            raise AssertionError("active-stage recovery must not resolve a new commit")

    class RecoveringGraph:
        async def ainvoke(self, state, *, config):
            calls.append(state)
            return _final_state(analysis.analysis_id)

    worker = AnalysisWorker(
        repository=repository,
        queue=queue,
        ingestion=NoReingestion(),
        index_builder=lambda item: SimpleNamespace(snapshot=item),
        tools_builder=lambda index: SimpleNamespace(index=index),
        graph_builder=lambda tools, checkpointer: RecoveringGraph(),
        checkpoint_factory=MemoryCheckpointFactory(saver),
        snapshot_cleaner=SimpleNamespace(cleanup_expired=lambda snapshots: ()),
    )

    result = await worker.run(recovered_claim)

    assert result.status is PersistentAnalysisStatus.REVIEW_READY
    assert len(result.report_history) == 1
    if recovery_status is PersistentAnalysisStatus.INVESTIGATING:
        assert calls == [None]
    else:
        assert len(calls) == 1
        assert isinstance(calls[0], AnalysisState)


@pytest.mark.anyio
async def test_worker_persists_snapshot_before_index_and_recovery_is_idempotent(
    repository: AnalysisRepository, tmp_path: Path
) -> None:
    """Breaks if an index crash loses immutable snapshot metadata or duplicates work."""
    calls: list[str] = []
    snapshot = _snapshot(tmp_path)
    analysis = await repository.create_analysis(
        repo_url="https://github.com/owner/repo", issue_number=22
    )
    queue = PostgresJobQueue(repository.sessions, lease_duration=timedelta(seconds=30))
    first_claim = await queue.claim_next(worker_id="worker-first")
    assert first_claim is not None

    class FakeIngestion:
        async def ingest(self, repo_url, issue_number):
            calls.append("ingest")
            return SimpleNamespace(
                issue=GithubIssue(
                    number=issue_number,
                    title="Parser issue",
                    body="Parsing fails",
                    state="open",
                    html_url="https://github.com/owner/repo/issues/22",
                ),
                snapshot=snapshot,
            )

    class SimulatedProcessCrash(BaseException):
        pass

    first_worker = AnalysisWorker(
        repository=repository,
        queue=queue,
        ingestion=FakeIngestion(),
        index_builder=lambda item: (_ for _ in ()).throw(SimulatedProcessCrash()),
        tools_builder=lambda index: None,
        graph_builder=lambda tools, checkpointer: None,
        checkpoint_factory=MemoryCheckpointFactory(InMemorySaver()),
        snapshot_cleaner=SimpleNamespace(cleanup_expired=lambda snapshots: ()),
    )

    with pytest.raises(SimulatedProcessCrash):
        await first_worker.run(first_claim)

    crashed = await repository.get_analysis(analysis.analysis_id)
    assert crashed.status is PersistentAnalysisStatus.INDEXING
    assert crashed.state["snapshot"]["commit_sha"] == "a" * 40
    assert crashed.state["issue"]["number"] == 22

    recovered_claim = await queue.claim_next(
        worker_id="worker-recovery",
        now=first_claim.lease_expires_at + timedelta(seconds=1),
    )
    assert recovered_claim is not None

    class FinalGraph:
        async def ainvoke(self, state, *, config):
            return _final_state(analysis.analysis_id)

    recovery_worker = AnalysisWorker(
        repository=repository,
        queue=queue,
        ingestion=FakeIngestion(),
        index_builder=lambda item: calls.append("index") or SimpleNamespace(snapshot=item),
        tools_builder=lambda index: SimpleNamespace(index=index),
        graph_builder=lambda tools, checkpointer: FinalGraph(),
        checkpoint_factory=MemoryCheckpointFactory(InMemorySaver()),
        snapshot_cleaner=SimpleNamespace(cleanup_expired=lambda snapshots: ()),
    )

    result = await recovery_worker.run(recovered_claim)
    duplicate = await recovery_worker.run(recovered_claim)

    assert result.status is PersistentAnalysisStatus.REVIEW_READY
    assert duplicate.status is PersistentAnalysisStatus.REVIEW_READY
    assert calls == ["ingest", "index"]
    assert len(result.report_history) == 1
    assert [event.event_type for event in await repository.list_events(analysis.analysis_id)].count(
        "review_ready"
    ) == 1


@pytest.mark.anyio
async def test_worker_heartbeats_during_long_graph_execution(
    repository: AnalysisRepository, tmp_path: Path
) -> None:
    """Breaks if a healthy long-running worker lets another worker reclaim its lease."""
    snapshot = _snapshot(tmp_path)
    analysis = await repository.create_analysis(
        repo_url="https://github.com/owner/repo", issue_number=26
    )
    queue = PostgresJobQueue(
        repository.sessions, lease_duration=timedelta(seconds=0.12)
    )
    claim = await queue.claim_next(worker_id="worker-heartbeat")
    assert claim is not None
    graph_started = asyncio.Event()
    finish_graph = asyncio.Event()

    class FakeIngestion:
        async def ingest(self, repo_url, issue_number):
            return SimpleNamespace(
                issue=GithubIssue(
                    number=issue_number,
                    title="Parser issue",
                    body="Parsing fails",
                    state="open",
                    html_url="https://github.com/owner/repo/issues/26",
                ),
                snapshot=snapshot,
            )

    class SlowGraph:
        async def ainvoke(self, state, *, config):
            graph_started.set()
            await finish_graph.wait()
            return _final_state(analysis.analysis_id)

    worker = AnalysisWorker(
        repository=repository,
        queue=queue,
        ingestion=FakeIngestion(),
        index_builder=lambda item: SimpleNamespace(snapshot=item),
        tools_builder=lambda index: SimpleNamespace(index=index),
        graph_builder=lambda tools, checkpointer: SlowGraph(),
        checkpoint_factory=MemoryCheckpointFactory(InMemorySaver()),
        snapshot_cleaner=SimpleNamespace(cleanup_expired=lambda snapshots: ()),
    )

    running = asyncio.create_task(worker.run(claim))
    await graph_started.wait()
    await asyncio.sleep(0.18)
    duplicate = await queue.claim_next(worker_id="worker-duplicate")
    finish_graph.set()
    result = await running

    assert duplicate is None
    assert result.status is PersistentAnalysisStatus.REVIEW_READY
