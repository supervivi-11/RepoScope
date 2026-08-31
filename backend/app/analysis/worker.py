from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
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
from .domain import (
    AnalysisConflictError,
    PersistentAnalysisStatus,
    StoredAnalysis,
    TERMINAL_STATUSES,
)
from .failures import map_public_failure
from .queue import ClaimedAnalysis, PostgresJobQueue
from .repository import AnalysisRepository


class GraphRunner(Protocol):
    async def ainvoke(self, state: Any, *, config: dict[str, Any]) -> Any: ...


class AnalysisWorker:
    """Orchestrate Tasks 2–4 behind durable, injectable application boundaries."""

    def __init__(
        self,
        *,
        repository: AnalysisRepository,
        queue: PostgresJobQueue,
        ingestion: IngestionService,
        index_builder: Callable[[RepositorySnapshot], Any],
        tools_builder: Callable[[Any], Any],
        graph_builder: Callable[[Any, Any], GraphRunner],
        checkpoint_factory: CheckpointFactory,
        snapshot_cleaner: SnapshotCleaner,
    ) -> None:
        self._repository = repository
        self._queue = queue
        self._ingestion = ingestion
        self._index_builder = index_builder
        self._tools_builder = tools_builder
        self._graph_builder = graph_builder
        self._checkpoint_factory = checkpoint_factory
        self._snapshot_cleaner = snapshot_cleaner

    async def run(self, claim: ClaimedAnalysis) -> StoredAnalysis:
        stored = await self._repository.get_analysis(claim.analysis_id)
        if stored.status in TERMINAL_STATUSES or (
            stored.status is PersistentAnalysisStatus.REVIEW_READY
            and stored.pending_feedback_action is None
        ):
            return stored

        active_claim = await self._queue.heartbeat(claim)
        stop_heartbeat = asyncio.Event()
        main_work = asyncio.create_task(self._run_claimed(stored, active_claim))
        heartbeat = asyncio.create_task(
            self._heartbeat_loop(active_claim, stop_heartbeat)
        )
        try:
            done, _ = await asyncio.wait(
                {main_work, heartbeat},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat in done:
                try:
                    await heartbeat
                except BaseException as heartbeat_error:
                    await _cancel_and_await(main_work)
                    if isinstance(heartbeat_error, AnalysisConflictError):
                        latest = await self._repository.get_analysis(claim.analysis_id)
                        if latest.status in TERMINAL_STATUSES:
                            return latest
                    raise heartbeat_error
                await _cancel_and_await(main_work)
                raise RuntimeError("Analysis heartbeat stopped unexpectedly.")

            result = await main_work
            stop_heartbeat.set()
            try:
                await heartbeat
            except AnalysisConflictError:
                if result.status not in TERMINAL_STATUSES:
                    raise
            return result
        finally:
            stop_heartbeat.set()
            await _cancel_and_await(main_work)
            await _cancel_and_await(heartbeat)
            try:
                await self._queue.release(active_claim)
            except AnalysisConflictError:
                pass

    async def _heartbeat_loop(
        self, claim: ClaimedAnalysis, stop: asyncio.Event
    ) -> None:
        while True:
            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=self._queue.heartbeat_interval
                )
            except TimeoutError:
                await self._queue.heartbeat(claim)
                continue
            return

    async def _run_claimed(
        self, stored: StoredAnalysis, claim: ClaimedAnalysis
    ) -> StoredAnalysis:
        analysis_id = stored.analysis_id
        if (
            stored.status is PersistentAnalysisStatus.REVISING
            or stored.pending_feedback_action is not None
        ):
            return await self._resume_feedback(stored, claim)

        snapshot: RepositorySnapshot | None = None
        try:
            issue: IssueIdentity
            if stored.status is PersistentAnalysisStatus.QUEUED:
                await self._repository.transition(
                    analysis_id,
                    PersistentAnalysisStatus.INGESTING,
                    lease=claim,
                )
                stored = await self._repository.get_analysis(analysis_id)

            if stored.status is PersistentAnalysisStatus.INGESTING and not isinstance(
                stored.state.get("snapshot"), dict
            ):
                await self._repository.append_event(
                    analysis_id,
                    "ingestion_started",
                    {"status": "INGESTING"},
                    lease=claim,
                    dedupe_key="stage:ingestion",
                )
                ingestion = await self._ingestion.ingest(
                    stored.repo_url, stored.issue_number
                )
                snapshot = ingestion.snapshot
                issue = _issue_identity(ingestion)
                await self._repository.transition(
                    analysis_id,
                    PersistentAnalysisStatus.INGESTING,
                    state={
                        "snapshot": _snapshot_state(snapshot),
                        "issue": issue.model_dump(mode="json"),
                    },
                    lease=claim,
                )
                stored = await self._repository.get_analysis(analysis_id)
            else:
                snapshot = _restore_snapshot(stored.state)
                issue = _restore_issue(stored.state)

            if stored.status is PersistentAnalysisStatus.INGESTING:
                await self._repository.transition(
                    analysis_id,
                    PersistentAnalysisStatus.INDEXING,
                    lease=claim,
                )
                stored = await self._repository.get_analysis(analysis_id)

            if stored.status is PersistentAnalysisStatus.INDEXING:
                await self._repository.append_event(
                    analysis_id,
                    "indexing_started",
                    {"status": "INDEXING"},
                    lease=claim,
                    dedupe_key="stage:indexing",
                )
                index = await asyncio.to_thread(self._index_builder, snapshot)
                tools = await asyncio.to_thread(self._tools_builder, index)
                await self._repository.transition(
                    analysis_id,
                    PersistentAnalysisStatus.INVESTIGATING,
                    lease=claim,
                )
                stored = await self._repository.get_analysis(analysis_id)
            elif stored.status is PersistentAnalysisStatus.INVESTIGATING:
                index = await asyncio.to_thread(self._index_builder, snapshot)
                tools = await asyncio.to_thread(self._tools_builder, index)
            else:
                raise RuntimeError("Analysis cannot be resumed from its durable status.")

            await self._repository.append_event(
                analysis_id,
                "investigation_started",
                {"status": "INVESTIGATING"},
                lease=claim,
                dedupe_key="stage:investigation",
            )
            initial_state = build_analysis_state(
                analysis_id=str(analysis_id),
                tools=tools,
                issue=issue,
            )
            async with self._checkpoint_factory.open(
                analysis_id,
                attempt_count=claim.attempt_count,
            ) as checkpoint:
                graph = self._graph_builder(tools, checkpoint.checkpointer)
                graph_input = (
                    None
                    if await _checkpoint_has_state(
                        checkpoint.checkpointer, checkpoint.config
                    )
                    else initial_state
                )
                output = await graph.ainvoke(graph_input, config=checkpoint.config)
            final_state = _analysis_state(output)

            if final_state.report is None:
                raise RuntimeError("Investigation graph returned no report.")
            target = _persistent_graph_status(final_state.status)
            if target is not PersistentAnalysisStatus.REVIEW_READY:
                raise RuntimeError("Initial investigation did not produce a review result.")
            result = await self._repository.publish_review_result(
                analysis_id,
                report=final_state.report,
                state={
                    "graph": final_state.model_dump(mode="json"),
                    "snapshot": _snapshot_state(snapshot),
                    "issue": issue.model_dump(mode="json"),
                },
                counters=final_state.counters.model_dump(mode="json"),
                events=tuple(
                    (
                        event.sequence,
                        event.kind,
                        event.model_dump(mode="json", exclude={"sequence", "kind"}),
                    )
                    for event in final_state.events
                ),
                lease=claim,
                result_key="initial",
            )
            await asyncio.to_thread(
                self._snapshot_cleaner.cleanup_expired, (snapshot,)
            )
            return result
        except AnalysisConflictError:
            raise
        except Exception as error:
            failure = map_public_failure(error)
            if snapshot is not None:
                try:
                    await asyncio.to_thread(
                        self._snapshot_cleaner.cleanup_expired, (snapshot,)
                    )
                except Exception:
                    pass
            return await self._repository.fail_safe(
                analysis_id, failure, lease=claim
            )

    async def _resume_feedback(
        self, stored: StoredAnalysis, claim: ClaimedAnalysis
    ) -> StoredAnalysis:
        analysis_id = stored.analysis_id
        snapshot: RepositorySnapshot | None = None
        try:
            snapshot = _restore_snapshot(stored.state)
            previous_state = AnalysisState.model_validate(stored.state["graph"])
            feedback = await self._repository.latest_feedback(analysis_id)
            if feedback.processed_at is not None:
                raise RuntimeError("Feedback command was already processed.")
            index = await asyncio.to_thread(self._index_builder, snapshot)
            tools = await asyncio.to_thread(self._tools_builder, index)
            async with self._checkpoint_factory.open(
                analysis_id,
                attempt_count=claim.attempt_count,
            ) as checkpoint:
                if not await _checkpoint_has_state(
                    checkpoint.checkpointer, checkpoint.config
                ):
                    raise RuntimeError("Revision checkpoint state is unavailable.")
                graph = self._graph_builder(tools, checkpoint.checkpointer)
                checkpoint_state = await _checkpoint_analysis_state(
                    checkpoint.checkpointer,
                    checkpoint.config,
                )
                if _is_applied_feedback_outcome(checkpoint_state, feedback):
                    final_state = checkpoint_state
                else:
                    output = await graph.ainvoke(
                        Command(
                            resume={
                                "action": feedback.action,
                                "text": feedback.comment,
                                "command_id": feedback.fingerprint,
                            }
                        ),
                        config=checkpoint.config,
                    )
                    final_state = _analysis_state(output)
            new_events = final_state.events[len(previous_state.events) :]
            durable_state = {
                "graph": final_state.model_dump(mode="json"),
                "snapshot": _snapshot_state(snapshot),
                "issue": _restore_issue(stored.state).model_dump(mode="json"),
            }
            if feedback.action == "accept":
                if final_state.status is not AnalysisStatus.COMPLETED:
                    raise RuntimeError("Accepted graph did not complete.")
                result = await self._repository.complete_accept(
                    analysis_id,
                    state=durable_state,
                    counters=final_state.counters.model_dump(mode="json"),
                    events=tuple(
                        (
                            event.sequence,
                            event.kind,
                            event.model_dump(
                                mode="json", exclude={"sequence", "kind"}
                            ),
                        )
                        for event in new_events
                    ),
                    lease=claim,
                )
                await asyncio.to_thread(
                    self._snapshot_cleaner.cleanup_expired, (snapshot,)
                )
                return result

            if feedback.comment is None:
                raise RuntimeError("Revision job has no durable revision comment.")
            if final_state.report is None:
                raise RuntimeError("Revised graph returned no report.")
            result = await self._repository.publish_review_result(
                analysis_id,
                report=final_state.report,
                state=durable_state,
                counters=final_state.counters.model_dump(mode="json"),
                events=tuple(
                    (
                        event.sequence,
                        event.kind,
                        event.model_dump(mode="json", exclude={"sequence", "kind"}),
                    )
                    for event in new_events
                ),
                lease=claim,
                result_key="revision:1",
                consume_feedback=True,
            )
            await asyncio.to_thread(
                self._snapshot_cleaner.cleanup_expired, (snapshot,)
            )
            return result
        except AnalysisConflictError:
            raise
        except Exception as error:
            failure = map_public_failure(error)
            if snapshot is not None:
                try:
                    await asyncio.to_thread(
                        self._snapshot_cleaner.cleanup_expired, (snapshot,)
                    )
                except Exception:
                    pass
            return await self._repository.fail_safe(
                analysis_id, failure, lease=claim
            )

    async def _persist_graph_event(
        self,
        analysis_id: UUID,
        event: AnalysisEvent,
        claim: ClaimedAnalysis,
    ) -> None:
        payload = event.model_dump(mode="json", exclude={"sequence", "kind"})
        await self._repository.append_event(
            analysis_id,
            event.kind,
            payload,
            lease=claim,
            dedupe_key=f"graph:{event.sequence}",
        )


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


def _restore_issue(state: dict[str, Any]) -> IssueIdentity:
    try:
        return IssueIdentity.model_validate(state["issue"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("Persisted issue metadata is invalid.") from exc


async def _checkpoint_has_state(
    checkpointer: Any, config: dict[str, Any]
) -> bool:
    getter = getattr(checkpointer, "aget_tuple", None)
    if getter is None:
        return False
    return await getter(config) is not None


async def _checkpoint_analysis_state(
    checkpointer: Any,
    config: dict[str, Any],
) -> AnalysisState | None:
    getter = getattr(checkpointer, "aget_tuple", None)
    if getter is None:
        return None
    checkpoint_tuple = await getter(config)
    checkpoint = getattr(checkpoint_tuple, "checkpoint", None)
    if not isinstance(checkpoint, Mapping):
        return None
    values = checkpoint.get("channel_values")
    if not isinstance(values, Mapping):
        return None
    try:
        return _analysis_state(values)
    except (TypeError, ValueError):
        return None


def _is_applied_feedback_outcome(
    state: AnalysisState | None,
    feedback: Any,
) -> bool:
    if state is None or state.applied_feedback_id != feedback.fingerprint:
        return False
    if feedback.action == "accept":
        return state.status is AnalysisStatus.COMPLETED
    return (
        feedback.action == "revise"
        and state.status is AnalysisStatus.REVIEW_READY
        and state.counters.user_revisions == 1
        and state.report is not None
    )


async def _cancel_and_await(task: asyncio.Task[Any]) -> None:
    if not task.done():
        task.cancel()
    try:
        await task
    except BaseException:
        pass


def _analysis_state(output: Any) -> AnalysisState:
    if isinstance(output, AnalysisState):
        return output
    if isinstance(output, Mapping):
        fields = AnalysisState.model_fields
        return AnalysisState.model_validate(
            {key: value for key, value in output.items() if key in fields}
        )
    return AnalysisState.model_validate(output)
