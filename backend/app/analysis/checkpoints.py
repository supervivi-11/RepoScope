from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from app.agent.models import (
    AnalysisEvent,
    AnalysisPhase,
    AnalysisReport,
    AnalysisState,
    AnalysisStatus,
    BudgetCounters,
    CritiqueResult,
    EvidenceCitation,
    EvidenceCitationSummary,
    FeedbackCommand,
    FrozenModel,
    Hypothesis,
    ImpactedFile,
    InvestigationBudget,
    IssueIdentity,
    IssueUnderstanding,
    ProposedTest,
    RepositoryIdentity,
    ToolArgument,
    ToolRequest,
    ToolResultSummary,
)


_TASK4_STATE_TYPES = (
    AnalysisEvent,
    AnalysisPhase,
    AnalysisReport,
    AnalysisState,
    AnalysisStatus,
    BudgetCounters,
    CritiqueResult,
    EvidenceCitation,
    EvidenceCitationSummary,
    FeedbackCommand,
    FrozenModel,
    Hypothesis,
    ImpactedFile,
    InvestigationBudget,
    IssueIdentity,
    IssueUnderstanding,
    ProposedTest,
    RepositoryIdentity,
    ToolArgument,
    ToolRequest,
    ToolResultSummary,
)
_TASK4_ALLOWLIST = tuple(
    (state_type.__module__, state_type.__name__) for state_type in _TASK4_STATE_TYPES
)
_CHECKPOINT_BOOTSTRAP_LOCK = "reposcope:langgraph-checkpoint-bootstrap:v1"


@dataclass(frozen=True, slots=True)
class CheckpointContext:
    checkpointer: Any
    config: dict[str, dict[str, str]]


class CheckpointFactory(Protocol):
    def open(self, analysis_id: UUID, *, attempt_count: int = 1): ...


def build_checkpoint_serializer() -> JsonPlusSerializer:
    """Allow only the explicit Task 4 graph-state model constructors."""
    return JsonPlusSerializer(
        pickle_fallback=False,
        allowed_json_modules=_TASK4_ALLOWLIST,
        allowed_msgpack_modules=_TASK4_ALLOWLIST,
    )


class PostgresCheckpointFactory:
    def __init__(self, database_url: str) -> None:
        self._connection_string = _psycopg_connection_string(database_url)
        self._setup_complete = False
        self._setup_lock = asyncio.Lock()

    @asynccontextmanager
    async def open(
        self, analysis_id: UUID, *, attempt_count: int = 1
    ) -> AsyncIterator[CheckpointContext]:
        serializer = build_checkpoint_serializer()
        async with AsyncPostgresSaver.from_conn_string(
            self._connection_string,
            pipeline=False,
            serde=serializer,
        ) as saver:
            if not self._setup_complete:
                async with self._setup_lock:
                    if not self._setup_complete:
                        await _setup_with_advisory_lock(saver)
                        self._setup_complete = True
            config = await prepare_attempt_checkpoint(
                saver,
                analysis_id,
                attempt_count=attempt_count,
            )
            yield CheckpointContext(
                checkpointer=saver,
                config=config,
            )


def attempt_checkpoint_config(
    analysis_id: UUID, *, attempt_count: int
) -> dict[str, dict[str, str]]:
    if not isinstance(analysis_id, UUID):
        raise TypeError("analysis ID must be a UUID")
    if type(attempt_count) is not int or attempt_count < 1:
        raise ValueError("checkpoint attempt count must be positive")
    return {
        "configurable": {
            "thread_id": f"{analysis_id}:attempt:{attempt_count}",
            "checkpoint_ns": "",
        }
    }


async def prepare_attempt_checkpoint(
    saver: Any,
    analysis_id: UUID,
    *,
    attempt_count: int,
) -> dict[str, dict[str, str]]:
    """Create one immutable checkpoint handoff into an isolated attempt thread."""
    target = attempt_checkpoint_config(
        analysis_id,
        attempt_count=attempt_count,
    )
    existing = await saver.aget_tuple(target)
    if existing is not None:
        _validate_checkpoint_owner(existing, target)
        return target
    if attempt_count == 1:
        return target

    source = None
    for source_attempt in range(attempt_count - 1, 0, -1):
        source_config = attempt_checkpoint_config(
            analysis_id,
            attempt_count=source_attempt,
        )
        candidate = await saver.aget_tuple(source_config)
        if candidate is not None:
            _validate_checkpoint_owner(candidate, source_config)
            source = deepcopy(candidate)
            break
    if source is None:
        return target

    checkpoint_id = str(source.checkpoint["id"])
    write_config = {
        "configurable": {
            **target["configurable"],
            "checkpoint_id": checkpoint_id,
        }
    }
    writes_by_task: dict[str, list[tuple[str, Any]]] = {}
    for task_id, channel, value in source.pending_writes or ():
        writes_by_task.setdefault(task_id, []).append((channel, value))
    for task_id, writes in writes_by_task.items():
        await saver.aput_writes(write_config, writes, task_id)
    saved_config = await saver.aput(
        target,
        source.checkpoint,
        source.metadata,
        source.checkpoint["channel_versions"],
    )
    _validate_checkpoint_config_owner(
        saved_config,
        target,
        checkpoint_id=checkpoint_id,
    )
    return target


def _validate_checkpoint_owner(
    checkpoint_tuple: Any,
    expected_config: Mapping[str, Any],
) -> None:
    config = getattr(checkpoint_tuple, "config", None)
    checkpoint = getattr(checkpoint_tuple, "checkpoint", None)
    if not isinstance(checkpoint, Mapping) or not isinstance(checkpoint.get("id"), str):
        raise RuntimeError("Checkpoint ownership could not be validated.")
    _validate_checkpoint_config_owner(
        config,
        expected_config,
        checkpoint_id=checkpoint["id"],
    )


def _validate_checkpoint_config_owner(
    actual_config: Any,
    expected_config: Mapping[str, Any],
    *,
    checkpoint_id: str,
) -> None:
    actual = (
        actual_config.get("configurable")
        if isinstance(actual_config, Mapping)
        else None
    )
    expected = expected_config.get("configurable")
    if (
        not isinstance(actual, Mapping)
        or not isinstance(expected, Mapping)
        or actual.get("thread_id") != expected.get("thread_id")
        or actual.get("checkpoint_ns", "") != expected.get("checkpoint_ns", "")
        or actual.get("checkpoint_id") != checkpoint_id
    ):
        raise RuntimeError("Checkpoint ownership could not be validated.")


async def _setup_with_advisory_lock(saver: Any) -> None:
    connection = saver.conn
    async with connection.cursor() as cursor:
        await cursor.execute(
            "SELECT pg_advisory_lock(hashtextextended(%s, 0))",
            (_CHECKPOINT_BOOTSTRAP_LOCK,),
        )
        try:
            await saver.setup()
        finally:
            await cursor.execute(
                "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
                (_CHECKPOINT_BOOTSTRAP_LOCK,),
            )


def _psycopg_connection_string(database_url: str) -> str:
    if database_url.startswith("postgresql+psycopg://"):
        return "postgresql://" + database_url.removeprefix(
            "postgresql+psycopg://"
        )
    if database_url.startswith(("postgresql://", "postgres://")):
        return database_url
    raise ValueError("PostgreSQL checkpoint storage requires a PostgreSQL URL")
