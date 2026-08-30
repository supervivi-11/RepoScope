from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from app.agent import (
    AnalysisEvent,
    AnalysisState,
    AnalysisStatus,
    IssueIdentity,
    build_analysis_state,
)
from langgraph.types import Command
from app.ingestion import IngestionResult, IngestionService, RepositorySnapshot, SnapshotCleaner

from .checkpoints import CheckpointFactory
from .domain import PersistentAnalysisStatus, StoredAnalysis, TERMINAL_STATUSES
from .failures import map_public_failure
from .repository import AnalysisRepository


class GraphRunner(Protocol):
    async def ainvoke(self, state: Any, *, config: dict[str, Any]) -> Any: ...


class AnalysisWorker:
    """Orchestrate Tasks 2–4 behind durable, injectable application boundaries."""

    def __init__(
        self,
        *,
        repository: AnalysisRepository,
        ingestion: IngestionService,
        index_builder: Callable[[RepositorySnapshot], Any],
        tools_builder: Callable[[Any], Any],
        graph_builder: Callable[[Any, Any], GraphRunner],
        checkpoint_factory: CheckpointFactory,
        snapshot_cleaner: SnapshotCleaner,
    ) -> None:
        self._repository = repository
        self._ingestion = ingestion
        self._index_builder = index_builder
        self._tools_builder = tools_builder
        self._graph_builder = graph_builder
        self._checkpoint_factory = checkpoint_factory
        self._snapshot_cleaner = snapshot_cleaner

    async def run(self, analysis_id: UUID) -> StoredAnalysis:
        stored = await self._repository.get_analysis(analysis_id)
        if stored.status in TERMINAL_STATUSES or stored.status is (
            PersistentAnalysisStatus.REVIEW_READY
        ):
            return stored
        if stored.status is PersistentAnalysisStatus.REVISING:
            return await self._resume_revision(stored)

        snapshot: RepositorySnapshot | None = None
        try:
            await self._advance(stored.status, analysis_id, PersistentAnalysisStatus.INGESTING)
            await self._repository.append_event(
                analysis_id, "ingestion_started", {"status": "INGESTING"}
            )
            ingestion = await self._ingestion.ingest(
                stored.repo_url, stored.issue_number
            )
            snapshot = ingestion.snapshot

            current = (await self._repository.get_analysis(analysis_id)).status
            await self._advance(current, analysis_id, PersistentAnalysisStatus.INDEXING)
            await self._repository.append_event(
                analysis_id, "indexing_started", {"status": "INDEXING"}
            )
            index = self._index_builder(snapshot)
            tools = self._tools_builder(index)

            current = (await self._repository.get_analysis(analysis_id)).status
            await self._advance(current, analysis_id, PersistentAnalysisStatus.INVESTIGATING)
            await self._repository.append_event(
                analysis_id,
                "investigation_started",
                {"status": "INVESTIGATING"},
            )
            initial_state = build_analysis_state(
                analysis_id=str(analysis_id),
                tools=tools,
                issue=_issue_identity(ingestion),
            )
            async with self._checkpoint_factory.open(analysis_id) as checkpoint:
                graph = self._graph_builder(tools, checkpoint.checkpointer)
                output = await graph.ainvoke(initial_state, config=checkpoint.config)
            final_state = AnalysisState.model_validate(output)

            for event in final_state.events:
                await self._persist_graph_event(analysis_id, event)
            if final_state.report is None:
                raise RuntimeError("Investigation graph returned no report.")
            await self._repository.save_report(
                analysis_id,
                final_state.report,
                state={
                    "graph": final_state.model_dump(mode="json"),
                    "snapshot": _snapshot_state(snapshot),
                },
            )
            target = _persistent_graph_status(final_state.status)
            await self._repository.transition(
                analysis_id,
                target,
                counters=final_state.counters.model_dump(mode="json"),
            )
            self._snapshot_cleaner.cleanup_expired((snapshot,))
            return await self._repository.get_analysis(analysis_id)
        except Exception as error:
            failure = map_public_failure(error)
            if snapshot is not None:
                try:
                    self._snapshot_cleaner.cleanup_expired((snapshot,))
                except Exception:
                    pass
            return await self._repository.fail_safe(analysis_id, failure)

    async def _resume_revision(self, stored: StoredAnalysis) -> StoredAnalysis:
        analysis_id = stored.analysis_id
        snapshot: RepositorySnapshot | None = None
        try:
            snapshot = _restore_snapshot(stored.state)
            previous_state = AnalysisState.model_validate(stored.state["graph"])
            feedback = await self._repository.latest_feedback(analysis_id)
            if feedback.action != "revise" or feedback.comment is None:
                raise RuntimeError("Revision job has no durable revision command.")
            index = self._index_builder(snapshot)
            tools = self._tools_builder(index)
            async with self._checkpoint_factory.open(analysis_id) as checkpoint:
                graph = self._graph_builder(tools, checkpoint.checkpointer)
                output = await graph.ainvoke(
                    Command(
                        resume={"action": "revise", "text": feedback.comment}
                    ),
                    config=checkpoint.config,
                )
            final_state = AnalysisState.model_validate(output)
            for event in final_state.events[len(previous_state.events) :]:
                await self._persist_graph_event(analysis_id, event)
            if final_state.report is None:
                raise RuntimeError("Revised graph returned no report.")
            await self._repository.save_report(
                analysis_id,
                final_state.report,
                state={
                    "graph": final_state.model_dump(mode="json"),
                    "snapshot": _snapshot_state(snapshot),
                },
            )
            await self._repository.transition(
                analysis_id,
                _persistent_graph_status(final_state.status),
                counters=final_state.counters.model_dump(mode="json"),
            )
            self._snapshot_cleaner.cleanup_expired((snapshot,))
            return await self._repository.get_analysis(analysis_id)
        except Exception as error:
            failure = map_public_failure(error)
            if snapshot is not None:
                try:
                    self._snapshot_cleaner.cleanup_expired((snapshot,))
                except Exception:
                    pass
            return await self._repository.fail_safe(analysis_id, failure)

    async def _persist_graph_event(
        self, analysis_id: UUID, event: AnalysisEvent
    ) -> None:
        payload = event.model_dump(mode="json", exclude={"sequence", "kind"})
        await self._repository.append_event(analysis_id, event.kind, payload)

    async def _advance(
        self,
        current: PersistentAnalysisStatus,
        analysis_id: UUID,
        target: PersistentAnalysisStatus,
    ) -> None:
        order = {
            PersistentAnalysisStatus.QUEUED: 0,
            PersistentAnalysisStatus.INGESTING: 1,
            PersistentAnalysisStatus.INDEXING: 2,
            PersistentAnalysisStatus.INVESTIGATING: 3,
        }
        if current is target or order.get(current, 99) > order[target]:
            return
        await self._repository.transition(analysis_id, target)


def _issue_identity(ingestion: IngestionResult) -> IssueIdentity:
    issue = ingestion.issue
    return IssueIdentity(
        number=issue.number,
        title=issue.title,
        body=issue.body,
        html_url=issue.html_url,
    )


def _persistent_graph_status(status: AnalysisStatus) -> PersistentAnalysisStatus:
    mapping = {
        AnalysisStatus.REVIEW_READY: PersistentAnalysisStatus.REVIEW_READY,
        AnalysisStatus.COMPLETED: PersistentAnalysisStatus.COMPLETED,
        AnalysisStatus.FAILED: PersistentAnalysisStatus.FAILED,
    }
    try:
        return mapping[status]
    except KeyError as exc:
        raise RuntimeError("Investigation graph stopped in a non-persistable state.") from exc


def _snapshot_state(snapshot: RepositorySnapshot) -> dict[str, Any]:
    return {
        "owner": snapshot.owner,
        "repository": snapshot.repository,
        "commit_sha": snapshot.commit_sha,
        "root_path": str(snapshot.root_path),
        "indexed_byte_count": snapshot.indexed_byte_count,
        "created_at": snapshot.created_at.isoformat(),
        "cleanup_deadline": snapshot.cleanup_deadline.isoformat(),
    }


def _restore_snapshot(state: dict[str, Any]) -> RepositorySnapshot:
    payload = state.get("snapshot")
    if not isinstance(payload, dict):
        raise RuntimeError("Persisted snapshot metadata is unavailable.")
    try:
        return RepositorySnapshot(
            owner=str(payload["owner"]),
            repository=str(payload["repository"]),
            commit_sha=str(payload["commit_sha"]),
            root_path=Path(str(payload["root_path"])),
            indexed_byte_count=int(payload["indexed_byte_count"]),
            created_at=datetime.fromisoformat(str(payload["created_at"])),
            cleanup_deadline=datetime.fromisoformat(
                str(payload["cleanup_deadline"])
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("Persisted snapshot metadata is invalid.") from exc
