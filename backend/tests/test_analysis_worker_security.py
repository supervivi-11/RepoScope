from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from langgraph.types import Command
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agent import (
    AnalysisEvent,
    AnalysisPhase,
    AnalysisReport,
    AnalysisStatus,
)
from app.analysis import AnalysisRepository, PersistentAnalysisStatus
from app.analysis.checkpoints import CheckpointContext
from app.analysis.failures import map_public_failure, redact_public_data
from app.analysis.failures import PublicFailure
from app.analysis.worker import AnalysisWorker
from app.db import metadata
from app.ingestion import (
    GithubIssue,
    GithubRateLimitError,
    RepositorySnapshot,
)


@pytest.fixture
async def repository(tmp_path: Path) -> AsyncIterator[AnalysisRepository]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'worker.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    yield AnalysisRepository(sessions)
    await engine.dispose()


class FakeCheckpointFactory:
    @asynccontextmanager
    async def open(self, analysis_id):
        yield CheckpointContext(
            checkpointer=object(),
            config={"configurable": {"thread_id": str(analysis_id)}},
        )


class FakeGraph:
    def __init__(self, calls: list[str], report: AnalysisReport) -> None:
        self._calls = calls
        self._report = report

    async def ainvoke(self, state, *, config):
        self._calls.append(f"graph:{config['configurable']['thread_id']}")
        event = AnalysisEvent(
            sequence=1,
            phase=AnalysisPhase.REVIEW,
            status=AnalysisStatus.REVIEW_READY,
            kind="review_ready",
        )
        return state.model_copy(
            update={
                "phase": AnalysisPhase.REVIEW,
                "status": AnalysisStatus.REVIEW_READY,
                "report": self._report,
                "events": (event,),
            }
        )


def _report() -> AnalysisReport:
    return AnalysisReport(
        outcome="insufficient_evidence",
        issue_summary="Parser issue",
        observed_behavior="Parsing fails",
        expected_behavior="Parsing succeeds",
        uncertainties=("Static evidence is incomplete",),
        confidence=0.2,
    )


def _snapshot(tmp_path: Path) -> RepositorySnapshot:
    root = tmp_path / "snapshot"
    root.mkdir()
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


@pytest.mark.anyio
async def test_worker_integrates_ingestion_index_graph_and_ignores_duplicate_delivery(
    repository: AnalysisRepository, tmp_path: Path
) -> None:
    """Breaks if delivery retries rerun the model or create duplicate reports/events."""
    calls: list[str] = []
    snapshot = _snapshot(tmp_path)

    class FakeIngestion:
        async def ingest(self, repo_url, issue_number):
            calls.append("ingest")
            return SimpleNamespace(
                issue=GithubIssue(
                    number=issue_number,
                    title="Parser issue",
                    body="Parsing fails",
                    state="open",
                    html_url="https://github.com/owner/repo/issues/1",
                ),
                snapshot=snapshot,
            )

    class FakeCleaner:
        def cleanup_expired(self, snapshots):
            calls.append("cleanup")
            return ()

    analysis = await repository.create_analysis(
        repo_url="https://github.com/owner/repo", issue_number=1
    )
    worker = AnalysisWorker(
        repository=repository,
        ingestion=FakeIngestion(),
        index_builder=lambda item: calls.append("index") or SimpleNamespace(snapshot=item),
        tools_builder=lambda index: calls.append("tools") or SimpleNamespace(index=index),
        graph_builder=lambda tools, checkpointer: FakeGraph(calls, _report()),
        checkpoint_factory=FakeCheckpointFactory(),
        snapshot_cleaner=FakeCleaner(),
    )

    first = await worker.run(analysis.analysis_id)
    duplicate = await worker.run(analysis.analysis_id)

    assert first.status is PersistentAnalysisStatus.REVIEW_READY
    assert duplicate.status is PersistentAnalysisStatus.REVIEW_READY
    assert calls.count("ingest") == 1
    assert calls.count("index") == 1
    assert len(first.report_history) == 1
    assert [event.sequence for event in await repository.list_events(analysis.analysis_id)] == [
        1,
        2,
        3,
        4,
    ]


@pytest.mark.anyio
async def test_worker_maps_typed_failure_without_persisting_exception_or_secret(
    repository: AnalysisRepository,
) -> None:
    """Breaks if upstream exception text or credentials reach durable/API errors."""
    class FailingIngestion:
        async def ingest(self, repo_url, issue_number):
            raise GithubRateLimitError(
                "Authorization: Bearer sk-live-secret at https://u:p@api.github.com"
            )

    analysis = await repository.create_analysis(
        repo_url="https://github.com/owner/repo", issue_number=2
    )
    worker = AnalysisWorker(
        repository=repository,
        ingestion=FailingIngestion(),
        index_builder=lambda snapshot: None,
        tools_builder=lambda index: None,
        graph_builder=lambda tools, checkpointer: None,
        checkpoint_factory=FakeCheckpointFactory(),
        snapshot_cleaner=SimpleNamespace(cleanup_expired=lambda snapshots: ()),
    )

    failed = await worker.run(analysis.analysis_id)
    events = await repository.list_events(analysis.analysis_id)

    assert failed.status is PersistentAnalysisStatus.FAILED
    assert failed.error_code == "upstream_unavailable"
    assert failed.error_message == "GitHub is temporarily unavailable."
    serialized = str((failed, events))
    assert "sk-live-secret" not in serialized
    assert "u:p@" not in serialized
    assert "Authorization" not in serialized


def test_failure_mapping_and_recursive_redaction_are_stable() -> None:
    """Breaks if unknown failures leak text or observable payloads retain secrets."""
    unknown = map_public_failure(RuntimeError("sk-private prompt contents"))
    cleaned = redact_public_data(
        {
            "Authorization": "Bearer token",
            "model_key": "sk-abcdef123456",
            "messages": ["raw prompt"],
            "url": "https://alice:password@example.com/path",
            "safe": "keep me",
        }
    )

    assert (unknown.code, unknown.message, unknown.http_status) == (
        "internal_error",
        "Analysis failed safely.",
        500,
    )
    assert cleaned == {
        "Authorization": "[REDACTED]",
        "model_key": "[REDACTED]",
        "messages": "[REDACTED]",
        "url": "https://[REDACTED]@example.com/path",
        "safe": "keep me",
    }


@pytest.mark.anyio
async def test_safe_failure_persistence_canonicalizes_untrusted_public_failure(
    repository: AnalysisRepository,
) -> None:
    """Breaks if a misused service boundary can persist caller-controlled errors."""
    analysis = await repository.create_analysis(
        repo_url="https://github.com/owner/repo", issue_number=15
    )

    failed = await repository.fail_safe(
        analysis.analysis_id,
        PublicFailure("invented_code", "Authorization sk-unsafe-secret", 500),
    )

    assert (failed.error_code, failed.error_message) == (
        "internal_error",
        "Analysis failed safely.",
    )
    assert "sk-unsafe-secret" not in str(
        await repository.list_events(analysis.analysis_id)
    )


@pytest.mark.anyio
async def test_revision_resumes_checkpoint_and_reuses_immutable_snapshot(
    repository: AnalysisRepository, tmp_path: Path
) -> None:
    """Breaks if revision re-ingests a newer commit or starts a fresh graph thread."""
    calls: list[object] = []
    snapshot = _snapshot(tmp_path)
    original = _report()
    revised = original.model_copy(update={"issue_summary": "Revised parser issue"})

    class FakeIngestion:
        async def ingest(self, repo_url, issue_number):
            calls.append("ingest")
            return SimpleNamespace(
                issue=GithubIssue(
                    number=issue_number,
                    title="Parser issue",
                    body="Parsing fails",
                    state="open",
                    html_url="https://github.com/owner/repo/issues/14",
                ),
                snapshot=snapshot,
            )

    class RevisableGraph:
        state = None

        async def ainvoke(self, command, *, config):
            calls.append(command)
            if isinstance(command, Command):
                assert command.resume == {
                    "action": "revise",
                    "text": "Inspect the parser branch.",
                }
                event = AnalysisEvent(
                    sequence=2,
                    phase=AnalysisPhase.REVIEW,
                    status=AnalysisStatus.REVIEW_READY,
                    kind="review_ready",
                )
                self.state = self.state.model_copy(
                    update={
                        "report": revised,
                        "original_report": original,
                        "report_history": (original, revised),
                        "events": self.state.events + (event,),
                    }
                )
                return self.state
            event = AnalysisEvent(
                sequence=1,
                phase=AnalysisPhase.REVIEW,
                status=AnalysisStatus.REVIEW_READY,
                kind="review_ready",
            )
            self.state = command.model_copy(
                update={
                    "phase": AnalysisPhase.REVIEW,
                    "status": AnalysisStatus.REVIEW_READY,
                    "report": original,
                    "events": (event,),
                }
            )
            return self.state

    graph = RevisableGraph()
    analysis = await repository.create_analysis(
        repo_url="https://github.com/owner/repo", issue_number=14
    )
    worker = AnalysisWorker(
        repository=repository,
        ingestion=FakeIngestion(),
        index_builder=lambda item: calls.append("index") or SimpleNamespace(snapshot=item),
        tools_builder=lambda index: SimpleNamespace(index=index),
        graph_builder=lambda tools, checkpointer: graph,
        checkpoint_factory=FakeCheckpointFactory(),
        snapshot_cleaner=SimpleNamespace(cleanup_expired=lambda snapshots: ()),
    )

    await worker.run(analysis.analysis_id)
    await repository.submit_feedback(
        analysis.analysis_id,
        action="revise",
        comment="Inspect the parser branch.",
    )
    result = await worker.run(analysis.analysis_id)

    assert calls.count("ingest") == 1
    assert calls.count("index") == 2
    assert any(isinstance(item, Command) for item in calls)
    assert result.status is PersistentAnalysisStatus.REVIEW_READY
    assert [item.report.issue_summary for item in result.report_history] == [
        "Parser issue",
        "Revised parser issue",
    ]
