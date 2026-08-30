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
from langgraph.types import Command, interrupt
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agent import (
    AnalysisEvent,
    AnalysisPhase,
    AnalysisReport,
    AnalysisState,
    AnalysisStatus,
    FeedbackCommand,
    IssueIdentity,
    RepositoryIdentity,
)
from app.analysis import (
    AnalysisConflictError,
    AnalysisRepository,
    AnalysisWorker,
    PersistentAnalysisStatus,
)
from app.analysis.checkpoints import CheckpointContext, build_checkpoint_serializer
from app.analysis.checkpoints import prepare_attempt_checkpoint
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
        self.opened_attempts: list[int] = []

    @asynccontextmanager
    async def open(self, analysis_id, *, attempt_count: int = 1):
        self.opened_attempts.append(attempt_count)
        yield CheckpointContext(
            checkpointer=self.saver,
            config=await prepare_attempt_checkpoint(
                self.saver,
                analysis_id,
                attempt_count=attempt_count,
            ),
        )


async def _seed_checkpoint(saver: InMemorySaver, analysis_id: object) -> None:
    builder = StateGraph(SeedState)
    builder.add_node("seed", lambda state: {"seeded": True})
    builder.add_edge(START, "seed")
    builder.add_edge("seed", END)
    graph = builder.compile(checkpointer=saver)
    await graph.ainvoke(
        {"seeded": False},
        {
            "configurable": {
                "thread_id": f"{analysis_id}:attempt:1",
                "checkpoint_ns": "",
            }
        },
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

    saver = InMemorySaver(serde=build_checkpoint_serializer())
    if recovery_status is PersistentAnalysisStatus.INVESTIGATING:
        await _seed_checkpoint(saver, analysis.analysis_id)

    class NoReingestion:
        async def ingest(self, repo_url, issue_number):
            raise AssertionError("active-stage recovery must not resolve a new commit")

    class RecoveringGraph:
        async def ainvoke(self, state, *, config):
            calls.append(state)
            return _final_state(analysis.analysis_id)

    checkpoint_factory = MemoryCheckpointFactory(saver)
    worker = AnalysisWorker(
        repository=repository,
        queue=queue,
        ingestion=NoReingestion(),
        index_builder=lambda item: SimpleNamespace(snapshot=item),
        tools_builder=lambda index: SimpleNamespace(index=index),
        graph_builder=lambda tools, checkpointer: RecoveringGraph(),
        checkpoint_factory=checkpoint_factory,
        snapshot_cleaner=SimpleNamespace(cleanup_expired=lambda snapshots: ()),
    )

    result = await worker.run(recovered_claim)

    assert result.status is PersistentAnalysisStatus.REVIEW_READY
    assert len(result.report_history) == 1
    assert checkpoint_factory.opened_attempts == [recovered_claim.attempt_count]
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
async def test_revision_checkpoint_crash_recovery_does_not_resend_command(
    repository: AnalysisRepository,
    tmp_path: Path,
) -> None:
    """Breaks if a checkpointed revision is mistaken for a fresh feedback command."""
    snapshot = _snapshot(tmp_path)
    analysis = await repository.create_analysis(
        repo_url="https://github.com/owner/repo",
        issue_number=27,
    )
    queue = PostgresJobQueue(repository.sessions, lease_duration=timedelta(seconds=30))
    initial_claim = await queue.claim_next(worker_id="worker-initial")
    assert initial_claim is not None
    initial_state = _final_state(analysis.analysis_id)
    durable_state = {
        "graph": initial_state.model_dump(mode="json"),
        "snapshot": _snapshot_payload(snapshot),
        "issue": _issue().model_dump(mode="json"),
    }
    await repository.transition(
        analysis.analysis_id,
        PersistentAnalysisStatus.INGESTING,
        state={
            "snapshot": durable_state["snapshot"],
            "issue": durable_state["issue"],
        },
        lease=initial_claim,
    )
    await repository.transition(
        analysis.analysis_id,
        PersistentAnalysisStatus.INDEXING,
        lease=initial_claim,
    )
    await repository.transition(
        analysis.analysis_id,
        PersistentAnalysisStatus.INVESTIGATING,
        lease=initial_claim,
    )
    assert initial_state.report is not None
    await repository.save_report(
        analysis.analysis_id,
        initial_state.report,
        state=durable_state,
        lease=initial_claim,
        result_key="initial",
    )
    await repository.transition(
        analysis.analysis_id,
        PersistentAnalysisStatus.REVIEW_READY,
        counters=initial_state.counters.model_dump(mode="json"),
        lease=initial_claim,
    )

    revised_report = _report().model_copy(
        update={"issue_summary": "Recovered revised report"}
    )

    def build_revisable_graph(checkpointer):
        builder = StateGraph(AnalysisState)

        def review(state: AnalysisState):
            payload = interrupt({"status": "REVIEW_READY"})
            return {"pending_feedback": FeedbackCommand.model_validate(payload)}

        def revise(state: AnalysisState):
            feedback = state.pending_feedback
            assert feedback is not None
            requested = AnalysisEvent(
                sequence=len(state.events) + 1,
                phase=AnalysisPhase.REVISING,
                status=AnalysisStatus.REVISING,
                kind="revision_requested",
            )
            revised = AnalysisEvent(
                sequence=len(state.events) + 2,
                phase=AnalysisPhase.REVISING,
                status=AnalysisStatus.REVISING,
                kind="report_revised",
            )
            return {
                "phase": AnalysisPhase.REVISING,
                "status": AnalysisStatus.REVISING,
                "report": revised_report,
                "original_report": state.report,
                "report_history": (state.report, revised_report),
                "revision_feedback": (feedback.text,),
                "applied_feedback_id": feedback.command_id,
                "pending_feedback": None,
                "counters": state.counters.model_copy(
                    update={"user_revisions": 1}
                ),
                "events": state.events + (requested, revised),
            }

        def reject(state: AnalysisState):
            rejected = AnalysisEvent(
                sequence=len(state.events) + 1,
                phase=AnalysisPhase.REVIEW,
                status=AnalysisStatus.REVIEW_READY,
                kind="revision_rejected",
            )
            return {
                "pending_feedback": None,
                "events": state.events + (rejected,),
            }

        def prepare_review(state: AnalysisState):
            ready = AnalysisEvent(
                sequence=len(state.events) + 1,
                phase=AnalysisPhase.REVIEW,
                status=AnalysisStatus.REVIEW_READY,
                kind="review_ready",
            )
            return {
                "phase": AnalysisPhase.REVIEW,
                "status": AnalysisStatus.REVIEW_READY,
                "events": state.events + (ready,),
            }

        builder.add_node("review", review)
        builder.add_node("revise", revise)
        builder.add_node("reject", reject)
        builder.add_node("prepare_review", prepare_review)
        builder.add_edge(START, "review")
        builder.add_conditional_edges(
            "review",
            lambda state: (
                "reject" if state.counters.user_revisions >= 1 else "revise"
            ),
            {"revise": "revise", "reject": "reject"},
        )
        builder.add_edge("revise", "prepare_review")
        builder.add_edge("reject", "prepare_review")
        builder.add_edge("prepare_review", "review")
        return builder.compile(checkpointer=checkpointer)

    saver = InMemorySaver()
    initial_config = await prepare_attempt_checkpoint(
        saver,
        analysis.analysis_id,
        attempt_count=initial_claim.attempt_count,
    )
    await build_revisable_graph(saver).ainvoke(initial_state, config=initial_config)
    await queue.release(initial_claim)
    await repository.submit_feedback(
        analysis.analysis_id,
        action="revise",
        comment="Inspect the parser branch once.",
    )
    feedback = await repository.latest_feedback(analysis.analysis_id)
    revision_claim = await queue.claim_next(worker_id="worker-revision")
    assert revision_claim is not None

    class SimulatedProcessCrash(BaseException):
        pass

    commands: list[Command] = []
    crash_once = True

    class CrashAfterCheckpoint:
        def __init__(self, graph) -> None:
            self.graph = graph

        async def ainvoke(self, value, *, config):
            nonlocal crash_once
            if isinstance(value, Command):
                commands.append(value)
            result = await self.graph.ainvoke(value, config=config)
            if isinstance(value, Command) and crash_once:
                crash_once = False
                raise SimulatedProcessCrash()
            return result

    worker = AnalysisWorker(
        repository=repository,
        queue=queue,
        ingestion=SimpleNamespace(),
        index_builder=lambda item: SimpleNamespace(snapshot=item),
        tools_builder=lambda index: SimpleNamespace(index=index),
        graph_builder=lambda tools, checkpointer: CrashAfterCheckpoint(
            build_revisable_graph(checkpointer)
        ),
        checkpoint_factory=MemoryCheckpointFactory(saver),
        snapshot_cleaner=SimpleNamespace(cleanup_expired=lambda snapshots: ()),
    )

    with pytest.raises(SimulatedProcessCrash):
        await worker.run(revision_claim)

    recovery_claim = await queue.claim_next(worker_id="worker-recovery")
    assert recovery_claim is not None
    recovered = await worker.run(recovery_claim)
    events = await repository.list_events(analysis.analysis_id)

    assert len(commands) == 1
    assert commands[0].resume["command_id"] == feedback.fingerprint
    assert recovered.status is PersistentAnalysisStatus.REVIEW_READY
    assert [item.report.issue_summary for item in recovered.report_history] == [
        "Recovered report",
        "Recovered revised report",
    ]
    event_types = [event.event_type for event in events]
    assert event_types.count("revision_requested") == 1
    assert event_types.count("report_revised") == 1
    assert "revision_rejected" not in event_types


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


@pytest.mark.anyio
async def test_heartbeat_loss_cancels_main_work_before_later_mutations(
    repository: AnalysisRepository,
    tmp_path: Path,
) -> None:
    """Breaks if lease loss is observed only after graph work mutates durable state."""
    snapshot = _snapshot(tmp_path)
    analysis = await repository.create_analysis(
        repo_url="https://github.com/owner/repo",
        issue_number=28,
    )
    queue = PostgresJobQueue(repository.sessions, lease_duration=timedelta(seconds=30))
    claim = await queue.claim_next(worker_id="worker-loses-heartbeat")
    assert claim is not None
    graph_started = asyncio.Event()
    graph_cancelled = asyncio.Event()
    graph_completed = asyncio.Event()

    class FakeIngestion:
        async def ingest(self, repo_url, issue_number):
            return SimpleNamespace(
                issue=GithubIssue(
                    number=issue_number,
                    title="Parser issue",
                    body="Parsing fails",
                    state="open",
                    html_url="https://github.com/owner/repo/issues/28",
                ),
                snapshot=snapshot,
            )

    class SlowGraph:
        async def ainvoke(self, state, *, config):
            graph_started.set()
            try:
                await asyncio.sleep(0.08)
            except asyncio.CancelledError:
                graph_cancelled.set()
                raise
            graph_completed.set()
            return _final_state(analysis.analysis_id)

    class LosingQueue:
        heartbeat_interval = 0.001

        def __init__(self) -> None:
            self.calls = 0

        async def heartbeat(self, active_claim):
            self.calls += 1
            if self.calls == 1:
                return await queue.heartbeat(active_claim)
            await graph_started.wait()
            raise AnalysisConflictError("Analysis lease was reclaimed.")

        async def release(self, active_claim):
            return await queue.release(active_claim)

    worker = AnalysisWorker(
        repository=repository,
        queue=LosingQueue(),  # type: ignore[arg-type]
        ingestion=FakeIngestion(),
        index_builder=lambda item: SimpleNamespace(snapshot=item),
        tools_builder=lambda index: SimpleNamespace(index=index),
        graph_builder=lambda tools, checkpointer: SlowGraph(),
        checkpoint_factory=MemoryCheckpointFactory(InMemorySaver()),
        snapshot_cleaner=SimpleNamespace(cleanup_expired=lambda snapshots: ()),
    )

    with pytest.raises(AnalysisConflictError, match="reclaimed"):
        await worker.run(claim)

    stored = await repository.get_analysis(analysis.analysis_id)
    assert graph_cancelled.is_set()
    assert not graph_completed.is_set()
    assert stored.status is PersistentAnalysisStatus.INVESTIGATING
    assert stored.report_history == ()
