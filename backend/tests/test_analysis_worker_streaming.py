from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
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
from app.analysis import AnalysisRepository, AnalysisWorker, PersistentAnalysisStatus
from app.analysis.checkpoints import CheckpointContext
from app.analysis.models import AnalysisEventRow
from app.analysis.queue import PostgresJobQueue
from app.db import metadata
from app.ingestion import GithubIssue, RepositorySnapshot


@pytest.fixture
async def repository(tmp_path: Path) -> AsyncIterator[AnalysisRepository]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'streaming.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    yield AnalysisRepository(sessions)
    await engine.dispose()


def _report(summary: str = "Streamed report") -> AnalysisReport:
    return AnalysisReport(
        outcome="insufficient_evidence",
        issue_summary=summary,
        observed_behavior="Observed behavior",
        expected_behavior="Expected behavior",
        uncertainties=("The streamed evidence is intentionally minimal.",),
        confidence=0.2,
    )


def _snapshot(tmp_path: Path) -> RepositorySnapshot:
    root = tmp_path / "snapshot"
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


def _issue(issue_number: int = 31) -> IssueIdentity:
    return IssueIdentity(
        number=issue_number,
        title="Parser issue",
        body="Parsing fails",
        html_url=f"https://github.com/owner/repo/issues/{issue_number}",
    )


def _base_state(analysis_id: object, *, status: AnalysisStatus = AnalysisStatus.INVESTIGATING) -> AnalysisState:
    phase = AnalysisPhase.REVIEW if status is AnalysisStatus.REVIEW_READY else AnalysisPhase.INVESTIGATING
    return AnalysisState(
        analysis_id=str(analysis_id),
        repository=RepositoryIdentity(owner="owner", repository="repo", commit_sha="a" * 40),
        issue=_issue(),
        phase=phase,
        status=status,
    )


class EmptyCheckpointFactory:
    @asynccontextmanager
    async def open(self, analysis_id, *, attempt_count: int = 1):
        class NoStateCheckpointer:
            async def aget_tuple(self, config):
                return None

        yield CheckpointContext(
            checkpointer=NoStateCheckpointer(),
            config={
                "configurable": {
                    "thread_id": f"{analysis_id}:attempt:{attempt_count}",
                    "checkpoint_ns": "",
                }
            },
        )


class StaticCheckpointFactory:
    def __init__(self, state: AnalysisState) -> None:
        self.state = state

    @asynccontextmanager
    async def open(self, analysis_id, *, attempt_count: int = 1):
        state = self.state

        class StaticCheckpointer:
            async def aget_tuple(self, config):
                return SimpleNamespace(
                    config=config,
                    checkpoint={"id": "checkpoint-final", "channel_values": state.model_dump(mode="json")},
                    pending_writes=(),
                )

        yield CheckpointContext(
            checkpointer=StaticCheckpointer(),
            config={
                "configurable": {
                    "thread_id": f"{analysis_id}:attempt:{attempt_count}",
                    "checkpoint_ns": "",
                }
            },
        )


async def _claim_at_investigating(
    repository: AnalysisRepository,
    queue: PostgresJobQueue,
    tmp_path: Path,
    *,
    issue_number: int = 31,
):
    snapshot = _snapshot(tmp_path)
    analysis = await repository.create_analysis(
        repo_url="https://github.com/owner/repo",
        issue_number=issue_number,
    )
    claim = await queue.claim_next(worker_id="worker-streaming")
    assert claim is not None
    await repository.transition(
        analysis.analysis_id,
        PersistentAnalysisStatus.INGESTING,
        state={
            "snapshot": _snapshot_payload(snapshot),
            "issue": _issue(issue_number).model_dump(mode="json"),
        },
        lease=claim,
    )
    await repository.transition(
        analysis.analysis_id,
        PersistentAnalysisStatus.INDEXING,
        lease=claim,
    )
    await repository.transition(
        analysis.analysis_id,
        PersistentAnalysisStatus.INVESTIGATING,
        lease=claim,
    )
    return analysis.analysis_id, claim


@pytest.mark.anyio
async def test_worker_streams_non_boundary_graph_events_before_final_publish(
    repository: AnalysisRepository,
    tmp_path: Path,
) -> None:
    queue = PostgresJobQueue(repository.sessions, lease_duration=timedelta(seconds=30))
    analysis_id, claim = await _claim_at_investigating(repository, queue, tmp_path)
    release_final = asyncio.Event()
    stream_called: list[dict[str, object]] = []

    class BlockingGraph:
        async def astream(self, state, config=None, **kwargs):
            stream_called.append({"state": state, "config": config, **kwargs})
            assert kwargs["stream_mode"] == "values"
            assert kwargs["durability"] == "sync"
            first_event = AnalysisEvent(
                sequence=1,
                phase=AnalysisPhase.UNDERSTANDING,
                status=AnalysisStatus.INVESTIGATING,
                kind="issue_understood",
            )
            first = state.model_copy(
                update={
                    "phase": AnalysisPhase.UNDERSTANDING,
                    "status": AnalysisStatus.INVESTIGATING,
                    "events": (first_event,),
                }
            )
            yield first
            await release_final.wait()
            ready_event = AnalysisEvent(
                sequence=2,
                phase=AnalysisPhase.REVIEW,
                status=AnalysisStatus.REVIEW_READY,
                kind="review_ready",
            )
            yield first.model_copy(
                update={
                    "phase": AnalysisPhase.REVIEW,
                    "status": AnalysisStatus.REVIEW_READY,
                    "report": _report(),
                    "events": (first_event, ready_event),
                }
            )

    worker = AnalysisWorker(
        repository=repository,
        queue=queue,
        ingestion=SimpleNamespace(),
        index_builder=lambda item: SimpleNamespace(snapshot=item),
        tools_builder=lambda index: SimpleNamespace(index=index),
        graph_builder=lambda tools, checkpointer: BlockingGraph(),
        checkpoint_factory=EmptyCheckpointFactory(),
        snapshot_cleaner=SimpleNamespace(cleanup_expired=lambda snapshots: ()),
    )

    running = asyncio.create_task(worker.run(claim))
    for _ in range(100):
        graph_events = [
            event
            for event in await repository.list_events(analysis_id)
            if event.event_type == "issue_understood"
        ]
        if graph_events:
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("streamed graph event was not published before final report")

    midflight = await repository.get_analysis(analysis_id)
    assert midflight.status is PersistentAnalysisStatus.INVESTIGATING
    assert midflight.current_report is None
    assert graph_events[0].data["status"] == "INVESTIGATING"

    release_final.set()
    result = await asyncio.wait_for(running, timeout=1)
    events = await repository.list_events(analysis_id)

    assert result.status is PersistentAnalysisStatus.REVIEW_READY
    assert result.current_report == _report()
    assert [event.event_type for event in events if event.event_type in {"issue_understood", "review_ready"}] == [
        "issue_understood",
        "review_ready",
    ]
    assert stream_called and stream_called[0]["state"] is not None
    async with repository.sessions() as session:
        dedupe_keys = (
            await session.scalars(
                select(AnalysisEventRow.dedupe_key)
                .where(AnalysisEventRow.analysis_id == analysis_id)
                .order_by(AnalysisEventRow.sequence)
            )
        ).all()
    assert "graph:1" in dedupe_keys
    assert "graph:2" in dedupe_keys


@pytest.mark.anyio
async def test_worker_publishes_final_checkpoint_without_rerunning_graph(
    repository: AnalysisRepository,
    tmp_path: Path,
) -> None:
    queue = PostgresJobQueue(repository.sessions, lease_duration=timedelta(seconds=30))
    analysis_id, claim = await _claim_at_investigating(repository, queue, tmp_path, issue_number=32)
    non_boundary = AnalysisEvent(
        sequence=1,
        phase=AnalysisPhase.UNDERSTANDING,
        status=AnalysisStatus.INVESTIGATING,
        kind="issue_understood",
    )
    boundary = AnalysisEvent(
        sequence=2,
        phase=AnalysisPhase.REVIEW,
        status=AnalysisStatus.REVIEW_READY,
        kind="review_ready",
    )
    checkpoint_state = _base_state(analysis_id, status=AnalysisStatus.REVIEW_READY).model_copy(
        update={"report": _report("Recovered from checkpoint"), "events": (non_boundary, boundary)}
    )

    class NoModelRerunGraph:
        async def astream(self, *args, **kwargs):
            raise AssertionError("final checkpoint should publish without running graph")

    worker = AnalysisWorker(
        repository=repository,
        queue=queue,
        ingestion=SimpleNamespace(),
        index_builder=lambda item: SimpleNamespace(snapshot=item),
        tools_builder=lambda index: SimpleNamespace(index=index),
        graph_builder=lambda tools, checkpointer: NoModelRerunGraph(),
        checkpoint_factory=StaticCheckpointFactory(checkpoint_state),
        snapshot_cleaner=SimpleNamespace(cleanup_expired=lambda snapshots: ()),
    )

    result = await worker.run(claim)
    events = await repository.list_events(analysis_id)

    assert result.status is PersistentAnalysisStatus.REVIEW_READY
    assert result.current_report == _report("Recovered from checkpoint")
    assert [event.event_type for event in events if event.event_type in {"issue_understood", "review_ready"}] == [
        "issue_understood",
        "review_ready",
    ]
