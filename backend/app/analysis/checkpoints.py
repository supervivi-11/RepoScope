from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
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


@dataclass(frozen=True, slots=True)
class CheckpointContext:
    checkpointer: Any
    config: dict[str, dict[str, str]]


class CheckpointFactory(Protocol):
    def open(self, analysis_id: UUID): ...


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

    @asynccontextmanager
    async def open(self, analysis_id: UUID) -> AsyncIterator[CheckpointContext]:
        serializer = build_checkpoint_serializer()
        async with AsyncPostgresSaver.from_conn_string(
            self._connection_string,
            pipeline=False,
            serde=serializer,
        ) as saver:
            yield CheckpointContext(
                checkpointer=saver,
                config={"configurable": {"thread_id": str(analysis_id)}},
            )


def _psycopg_connection_string(database_url: str) -> str:
    if database_url.startswith("postgresql+psycopg://"):
        return "postgresql://" + database_url.removeprefix(
            "postgresql+psycopg://"
        )
    if database_url.startswith(("postgresql://", "postgres://")):
        return database_url
    raise ValueError("PostgreSQL checkpoint storage requires a PostgreSQL URL")
