from __future__ import annotations

import asyncio
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
from app.analysis import (
    AnalysisConflictError,
    AnalysisRepository,
    PersistentAnalysisStatus,
)
from app.analysis.checkpoints import CheckpointContext
from app.analysis.failures import map_public_failure, redact_public_data
from app.analysis.failures import PublicFailure
from app.analysis.queue import PostgresJobQueue
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
    def __init__(self) -> None:
        self._checks = 0

    @asynccontextmanager
    async def open(self, analysis_id, *, attempt_count: int = 1):
        factory = self

        class FakeCheckpointer:
            async def aget_tuple(self, config):
                factory._checks += 1
                return object() if factory._checks > 1 else None

        yield CheckpointContext(
            checkpointer=FakeCheckpointer(),
            config={
                "configurable": {
                    "thread_id": f"{analysis_id}:attempt:{attempt_count}",
                    "checkpoint_ns": "",
                }
            },
        )


class FakeGraph:
    def __init__(self, calls: list[str], report: AnalysisReport) -> None:
        self._calls = calls
        self._report = report

    async def astream(self, state, config=None, *, stream_mode, durability):
        self._calls.append(f"graph:{config['configurable']['thread_id']}")
        event = AnalysisEvent(
            sequence=1,
            phase=AnalysisPhase.REVIEW,
            status=AnalysisStatus.REVIEW_READY,
            kind="review_ready",
        )
        yield state.model_copy(
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
    queue = PostgresJobQueue(repository.sessions)
    claim = await queue.claim_next(worker_id="worker-integration")
    assert claim is not None
    worker = AnalysisWorker(
        repository=repository,
        queue=queue,
        ingestion=FakeIngestion(),
        index_builder=lambda item: calls.append("index") or SimpleNamespace(snapshot=item),
        tools_builder=lambda index: calls.append("tools") or SimpleNamespace(index=index),
        graph_builder=lambda tools, checkpointer: FakeGraph(calls, _report()),
        checkpoint_factory=FakeCheckpointFactory(),
        snapshot_cleaner=FakeCleaner(),
    )

    first = await worker.run(claim)
    duplicate = await worker.run(claim)

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
    queue = PostgresJobQueue(repository.sessions)
    claim = await queue.claim_next(worker_id="worker-failure")
    assert claim is not None
    worker = AnalysisWorker(
        repository=repository,
        queue=queue,
        ingestion=FailingIngestion(),
        index_builder=lambda snapshot: None,
        tools_builder=lambda index: None,
        graph_builder=lambda tools, checkpointer: None,
        checkpoint_factory=FakeCheckpointFactory(),
        snapshot_cleaner=SimpleNamespace(cleanup_expired=lambda snapshots: ()),
    )

    failed = await worker.run(claim)
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


@pytest.mark.parametrize("delimiter", ["\n", "\r\n", "\r"])
def test_public_redaction_preserves_line_delimiters_and_is_idempotent(
    delimiter: str,
) -> None:
    """Breaks if credential removal changes citation line coordinates."""
    source = delimiter.join(
        (
            "Authorization:",
            "Bearer sk-live-secret",
            'OPENAI_API_KEY="opaqueCredential123456"',
            '"password": "hunter2"',
            "DATABASE_PASSWORD=supersecretvalue",
            "AWS_ACCESS_KEY_ID=AKIAABCDEFGHIJKLMNOP",
            'token = "abc def"',
            "token_count = 12",
            "AuthorizationPolicy = strict",
        )
    )

    cleaned = redact_public_data(source)

    assert isinstance(cleaned, str)
    assert cleaned.splitlines(keepends=True) == [
        item + delimiter
        for item in cleaned.split(delimiter)[:-1]
    ] + [cleaned.split(delimiter)[-1]]
    assert cleaned.count(delimiter) == source.count(delimiter)
    assert "sk-live-secret" not in cleaned
    assert "opaqueCredential123456" not in cleaned
    for secret in ("hunter2", "supersecretvalue", "AKIAABCDEFGHIJKLMNOP", "abc def"):
        assert secret not in cleaned
    assert "token_count = 12" in cleaned
    assert "AuthorizationPolicy = strict" in cleaned
    assert redact_public_data(cleaned) == cleaned


def test_recursive_redaction_covers_message_variants_and_short_sk_tokens() -> None:
    """Breaks if nested prompt/message aliases or short provider keys remain public."""
    cleaned = redact_public_data(
        {
            "outer": {
                "raw_message": "hidden",
                "input_messages": ["hidden"],
                "safe_text": "prefix sk-x suffix",
            }
        }
    )

    assert cleaned == {
        "outer": {
            "raw_message": "[REDACTED]",
            "input_messages": "[REDACTED]",
            "safe_text": "prefix [REDACTED] suffix",
        }
    }


def test_recursive_redaction_tokenizes_payload_keys_but_preserves_metadata() -> None:
    """Breaks if provider/model payload aliases remain public or metadata is erased."""
    cleaned = redact_public_data(
        {
            "raw_model_message": "hidden raw output",
            "provider_messages": ["hidden provider output"],
            "responseContent": "hidden response body",
            "prompt_payload": {"text": "hidden prompt"},
            "message_count": 2,
            "content_type": "application/json",
        }
    )

    assert cleaned == {
        "raw_model_message": "[REDACTED]",
        "provider_messages": "[REDACTED]",
        "responseContent": "[REDACTED]",
        "prompt_payload": "[REDACTED]",
        "message_count": 2,
        "content_type": "application/json",
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

        async def astream(self, command, config=None, *, stream_mode, durability):
            calls.append(command)
            if isinstance(command, Command):
                assert command.resume["action"] == "revise"
                assert command.resume["text"] == "Inspect the parser branch."
                assert len(command.resume["command_id"]) == 64
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
                yield self.state
                return
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
            yield self.state

    graph = RevisableGraph()
    analysis = await repository.create_analysis(
        repo_url="https://github.com/owner/repo", issue_number=14
    )
    queue = PostgresJobQueue(repository.sessions)
    initial_claim = await queue.claim_next(worker_id="worker-original")
    assert initial_claim is not None
    worker = AnalysisWorker(
        repository=repository,
        queue=queue,
        ingestion=FakeIngestion(),
        index_builder=lambda item: calls.append("index") or SimpleNamespace(snapshot=item),
        tools_builder=lambda index: SimpleNamespace(index=index),
        graph_builder=lambda tools, checkpointer: graph,
        checkpoint_factory=FakeCheckpointFactory(),
        snapshot_cleaner=SimpleNamespace(cleanup_expired=lambda snapshots: ()),
    )

    await worker.run(initial_claim)
    await repository.submit_feedback(
        analysis.analysis_id,
        action="revise",
        comment="Inspect the parser branch.",
    )
    revision_claim = await queue.claim_next(worker_id="worker-revision")
    assert revision_claim is not None
    result = await worker.run(revision_claim)

    assert calls.count("ingest") == 1
    assert calls.count("index") == 2
    assert any(isinstance(item, Command) for item in calls)
    assert result.status is PersistentAnalysisStatus.REVIEW_READY
    assert [item.report.issue_summary for item in result.report_history] == [
        "Parser issue",
        "Revised parser issue",
    ]


@pytest.mark.anyio
async def test_accept_resumes_checkpoint_and_completes_durably_once(
    repository: AnalysisRepository, tmp_path: Path
) -> None:
    """Breaks if accept bypasses Task 4 or terminal state/event can be half-written."""
    calls: list[object] = []
    snapshot = _snapshot(tmp_path)
    report = _report()

    class FakeIngestion:
        async def ingest(self, repo_url, issue_number):
            return SimpleNamespace(
                issue=GithubIssue(
                    number=issue_number,
                    title="Parser issue",
                    body="Parsing fails",
                    state="open",
                    html_url="https://github.com/owner/repo/issues/16",
                ),
                snapshot=snapshot,
            )

    class AcceptingGraph:
        state = None

        async def astream(self, value, config=None, *, stream_mode, durability):
            calls.append(value)
            if isinstance(value, Command):
                assert value.resume["action"] == "accept"
                assert value.resume["text"] is None
                assert len(value.resume["command_id"]) == 64
                await terminal_heartbeat_started.wait()
                accepted = AnalysisEvent(
                    sequence=2,
                    phase=AnalysisPhase.COMPLETED,
                    status=AnalysisStatus.COMPLETED,
                    kind="report_accepted",
                )
                self.state = self.state.model_copy(
                    update={
                        "phase": AnalysisPhase.COMPLETED,
                        "status": AnalysisStatus.COMPLETED,
                        "events": self.state.events + (accepted,),
                    }
                )
                yield self.state
                return
            ready = AnalysisEvent(
                sequence=1,
                phase=AnalysisPhase.REVIEW,
                status=AnalysisStatus.REVIEW_READY,
                kind="review_ready",
            )
            self.state = value.model_copy(
                update={
                    "phase": AnalysisPhase.REVIEW,
                    "status": AnalysisStatus.REVIEW_READY,
                    "report": report,
                    "events": (ready,),
                }
            )
            yield self.state

    graph = AcceptingGraph()
    terminal_heartbeat_started = asyncio.Event()
    analysis = await repository.create_analysis(
        repo_url="https://github.com/owner/repo", issue_number=16
    )
    queue = PostgresJobQueue(repository.sessions)
    initial_claim = await queue.claim_next(worker_id="worker-before-accept")
    assert initial_claim is not None
    checkpoint_factory = FakeCheckpointFactory()
    worker = AnalysisWorker(
        repository=repository,
        queue=queue,
        ingestion=FakeIngestion(),
        index_builder=lambda item: SimpleNamespace(snapshot=item),
        tools_builder=lambda index: SimpleNamespace(index=index),
        graph_builder=lambda tools, checkpointer: graph,
        checkpoint_factory=checkpoint_factory,
        snapshot_cleaner=SimpleNamespace(cleanup_expired=lambda snapshots: ()),
    )

    await worker.run(initial_claim)
    submitted = await repository.submit_feedback(
        analysis.analysis_id, action="accept"
    )
    assert submitted.status is PersistentAnalysisStatus.REVIEW_READY

    accept_claim = await queue.claim_next(worker_id="worker-accept")
    assert accept_claim is not None

    class TerminalHeartbeatQueue:
        heartbeat_interval = 0.001

        def __init__(self) -> None:
            self.calls = 0

        async def heartbeat(self, active_claim):
            self.calls += 1
            if self.calls == 1:
                return await queue.heartbeat(active_claim)
            terminal_heartbeat_started.set()
            while (
                await repository.get_analysis(active_claim.analysis_id)
            ).status is not PersistentAnalysisStatus.COMPLETED:
                await asyncio.sleep(0)
            raise AnalysisConflictError("Terminal analyses cannot be heartbeated.")

        async def release(self, active_claim):
            return await queue.release(active_claim)

    terminal_worker = AnalysisWorker(
        repository=repository,
        queue=TerminalHeartbeatQueue(),  # type: ignore[arg-type]
        ingestion=FakeIngestion(),
        index_builder=lambda item: SimpleNamespace(snapshot=item),
        tools_builder=lambda index: SimpleNamespace(index=index),
        graph_builder=lambda tools, checkpointer: graph,
        checkpoint_factory=checkpoint_factory,
        snapshot_cleaner=SimpleNamespace(cleanup_expired=lambda snapshots: ()),
    )
    terminal_heartbeat_started.clear()
    completed = await terminal_worker.run(accept_claim)
    duplicate_delivery = await terminal_worker.run(accept_claim)
    duplicate_feedback = await repository.submit_feedback(
        analysis.analysis_id, action="accept"
    )

    assert completed.status is PersistentAnalysisStatus.COMPLETED
    assert duplicate_delivery.status is PersistentAnalysisStatus.COMPLETED
    assert duplicate_feedback.replayed is True
    assert duplicate_feedback.status is PersistentAnalysisStatus.COMPLETED
    assert completed.state["graph"]["status"] == "COMPLETED"
    assert len(completed.report_history) == 1
    event_types = [
        event.event_type
        for event in await repository.list_events(analysis.analysis_id)
    ]
    assert event_types.count("report_accepted") == 1
    assert sum(isinstance(item, Command) for item in calls) == 1
