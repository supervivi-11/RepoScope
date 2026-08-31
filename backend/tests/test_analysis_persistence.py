from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agent import AnalysisReport
from app.analysis import (
    AnalysisConflictError,
    AnalysisRepository,
    PersistentAnalysisStatus,
)
from app.db import metadata
from app.analysis.models import (
    AnalysisEventRow,
    AnalysisFeedbackCommandRow,
    AnalysisJobRow,
)
from app.analysis.failures import PublicFailure


@pytest.fixture
async def repository(tmp_path: Path) -> AsyncIterator[AnalysisRepository]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'analyses.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    yield AnalysisRepository(sessions)
    await engine.dispose()


def _report(summary: str) -> AnalysisReport:
    return AnalysisReport(
        outcome="insufficient_evidence",
        issue_summary=summary,
        observed_behavior="Observed behavior",
        expected_behavior="Expected behavior",
        uncertainties=("Unknown root cause",),
        confidence=0.2,
    )


async def _make_review_ready(
    repository: AnalysisRepository, analysis_id: object
) -> None:
    for status in (
        PersistentAnalysisStatus.INGESTING,
        PersistentAnalysisStatus.INDEXING,
        PersistentAnalysisStatus.INVESTIGATING,
        PersistentAnalysisStatus.REVIEW_READY,
    ):
        await repository.transition(analysis_id, status)


def test_persistent_status_contract_is_exact() -> None:
    """Breaks if storage/API drift from the public Task 5 status contract."""
    assert tuple(status.value for status in PersistentAnalysisStatus) == (
        "QUEUED",
        "INGESTING",
        "INDEXING",
        "INVESTIGATING",
        "REVIEW_READY",
        "REVISING",
        "COMPLETED",
        "FAILED",
    )


@pytest.mark.anyio
async def test_create_replays_same_idempotent_request_and_rejects_key_reuse(
    repository: AnalysisRepository,
) -> None:
    """Breaks if retries create duplicate jobs or a key can alias another request."""
    created = await repository.create_analysis(
        repo_url="https://github.com/owner/repo",
        issue_number=12,
        idempotency_key="request-12",
    )
    replayed = await repository.create_analysis(
        repo_url="https://github.com/owner/repo",
        issue_number=12,
        idempotency_key="request-12",
    )

    assert created.replayed is False
    assert replayed.replayed is True
    assert replayed.analysis_id == created.analysis_id
    assert replayed.status is PersistentAnalysisStatus.QUEUED

    with pytest.raises(AnalysisConflictError):
        await repository.create_analysis(
            repo_url="https://github.com/owner/other",
            issue_number=12,
            idempotency_key="request-12",
        )


@pytest.mark.anyio
async def test_events_receive_unique_monotonic_sequences(
    repository: AnalysisRepository,
) -> None:
    """Breaks if reconnect replay can observe duplicate or non-monotonic event IDs."""
    analysis = await repository.create_analysis(
        repo_url="https://github.com/owner/repo", issue_number=1
    )

    first = await repository.append_event(
        analysis.analysis_id, "analysis_queued", {"status": "QUEUED"}
    )
    second = await repository.append_event(
        analysis.analysis_id, "ingestion_started", {"status": "INGESTING"}
    )

    assert (first.sequence, second.sequence) == (1, 2)
    assert [event.sequence for event in await repository.list_events(analysis.analysis_id)] == [
        1,
        2,
    ]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "event_type",
    ["safe\nid: 999", "safe\revent: injected", "has-dash", "UPPERCASE"],
)
async def test_event_type_rejects_sse_header_injection_and_unsafe_grammar(
    repository: AnalysisRepository, event_type: str
) -> None:
    """Breaks if stored event names can inject SSE fields or drift from stable names."""
    analysis = await repository.create_analysis(
        repo_url="https://github.com/owner/repo", issue_number=24
    )

    with pytest.raises(ValueError):
        await repository.append_event(analysis.analysis_id, event_type, {})

    assert await repository.list_events(analysis.analysis_id) == ()


@pytest.mark.anyio
async def test_report_versions_preserve_original_and_revised_reports(
    repository: AnalysisRepository,
) -> None:
    """Breaks if saving a revision overwrites the original investigation report."""
    analysis = await repository.create_analysis(
        repo_url="https://github.com/owner/repo", issue_number=2
    )

    original = await repository.save_report(
        analysis.analysis_id, _report("Original"), state={"phase": "review"}
    )
    revised = await repository.save_report(
        analysis.analysis_id, _report("Revised"), state={"phase": "revising"}
    )
    stored = await repository.get_analysis(analysis.analysis_id)

    assert (original.version, revised.version) == (1, 2)
    assert [item.report.issue_summary for item in stored.report_history] == [
        "Original",
        "Revised",
    ]
    assert stored.current_report == _report("Revised")
    assert stored.state == {"phase": "revising"}


@pytest.mark.anyio
async def test_publish_review_result_atomically_exposes_report_events_and_status(
    repository: AnalysisRepository,
) -> None:
    analysis = await repository.create_analysis(
        repo_url="https://github.com/owner/repo", issue_number=202
    )
    for status in (
        PersistentAnalysisStatus.INGESTING,
        PersistentAnalysisStatus.INDEXING,
        PersistentAnalysisStatus.INVESTIGATING,
    ):
        await repository.transition(analysis.analysis_id, status)

    published = await repository.publish_review_result(
        analysis.analysis_id,
        report=_report("Atomic report"),
        state={"graph": {"status": "REVIEW_READY"}},
        counters={"tool_calls": 2},
        events=((1, "review_ready", {"status": "REVIEW_READY"}),),
        result_key="initial",
    )
    replayed = await repository.publish_review_result(
        analysis.analysis_id,
        report=_report("Atomic report"),
        state={"graph": {"status": "REVIEW_READY"}},
        counters={"tool_calls": 2},
        events=((1, "review_ready", {"status": "REVIEW_READY"}),),
        result_key="initial",
    )

    assert published.status is PersistentAnalysisStatus.REVIEW_READY
    assert published.current_report == _report("Atomic report")
    assert published.counters == {"tool_calls": 2}
    assert [
        event.event_type
        for event in await repository.list_events(analysis.analysis_id)
    ] == ["review_ready"]
    assert len(replayed.report_history) == 1
    assert len(await repository.list_events(analysis.analysis_id)) == 1


@pytest.mark.anyio
async def test_active_snapshot_paths_excludes_terminal_jobs(
    repository: AnalysisRepository, tmp_path: Path
) -> None:
    active = await repository.create_analysis(
        repo_url="https://github.com/owner/active", issue_number=1
    )
    terminal = await repository.create_analysis(
        repo_url="https://github.com/owner/terminal", issue_number=2
    )
    active_path = tmp_path / "active"
    terminal_path = tmp_path / "terminal"
    await repository.transition(
        active.analysis_id,
        PersistentAnalysisStatus.INGESTING,
        state={"snapshot": {"root_path": str(active_path)}},
    )
    await repository.fail_safe(
        terminal.analysis_id,
        PublicFailure("analysis_failed", "Analysis failed safely.", 500),
    )
    await repository.transition(
        terminal.analysis_id,
        PersistentAnalysisStatus.FAILED,
        state={"snapshot": {"root_path": str(terminal_path)}},
    )

    assert await repository.active_snapshot_paths() == {active_path.resolve()}


@pytest.mark.anyio
async def test_feedback_is_guarded_idempotent_and_limited_to_one_revision(
    repository: AnalysisRepository,
) -> None:
    """Breaks if feedback races bypass state guards or the one-revision budget."""
    analysis = await repository.create_analysis(
        repo_url="https://github.com/owner/repo", issue_number=3
    )
    await _make_review_ready(repository, analysis.analysis_id)

    revised = await repository.submit_feedback(
        analysis.analysis_id, action="revise", comment="Check the parser branch."
    )
    duplicate = await repository.submit_feedback(
        analysis.analysis_id, action="revise", comment="Check the parser branch."
    )

    assert revised.replayed is False
    assert duplicate.replayed is True
    assert duplicate.status is PersistentAnalysisStatus.REVISING

    with pytest.raises(AnalysisConflictError):
        await repository.submit_feedback(analysis.analysis_id, action="accept")

    await repository.transition(
        analysis.analysis_id,
        PersistentAnalysisStatus.REVIEW_READY,
        consume_feedback=True,
    )
    with pytest.raises(AnalysisConflictError):
        await repository.submit_feedback(
            analysis.analysis_id, action="revise", comment="Try another revision."
        )

    accepted = await repository.submit_feedback(analysis.analysis_id, action="accept")
    accepted_again = await repository.submit_feedback(
        analysis.analysis_id, action="accept"
    )
    assert accepted.status is PersistentAnalysisStatus.REVIEW_READY
    assert accepted_again.replayed is True


@pytest.mark.anyio
async def test_feedback_rejects_blank_revision_comment(
    repository: AnalysisRepository,
) -> None:
    """Breaks if an unusable revision request is persisted."""
    analysis = await repository.create_analysis(
        repo_url="https://github.com/owner/repo", issue_number=4
    )
    await _make_review_ready(repository, analysis.analysis_id)

    with pytest.raises(ValueError):
        await repository.submit_feedback(
            analysis.analysis_id, action="revise", comment="   "
        )


@pytest.mark.anyio
async def test_feedback_action_is_runtime_validated_without_accept_fallback(
    repository: AnalysisRepository,
) -> None:
    """Breaks if an arbitrary runtime action falls through to report acceptance."""
    analysis = await repository.create_analysis(
        repo_url="https://github.com/owner/repo", issue_number=19
    )
    await _make_review_ready(repository, analysis.analysis_id)

    with pytest.raises(ValueError):
        await repository.submit_feedback(
            analysis.analysis_id,
            action="approve",  # type: ignore[arg-type]
        )

    assert (await repository.get_analysis(analysis.analysis_id)).status is (
        PersistentAnalysisStatus.REVIEW_READY
    )


@pytest.mark.anyio
async def test_database_rejects_feedback_action_outside_public_contract(
    repository: AnalysisRepository,
) -> None:
    """Breaks if a service bug can persist a non-domain feedback action."""
    analysis = await repository.create_analysis(
        repo_url="https://github.com/owner/repo", issue_number=20
    )

    with pytest.raises(IntegrityError):
        async with repository.sessions.begin() as session:
            session.add(
                AnalysisFeedbackCommandRow(
                    analysis_id=analysis.analysis_id,
                    action="approve",
                    comment=None,
                    fingerprint="a" * 64,
                )
            )
            await session.flush()


@pytest.mark.anyio
async def test_progress_counters_and_legacy_errors_are_sanitized_before_public_read(
    repository: AnalysisRepository,
) -> None:
    """Breaks if nested operational data or old unsafe errors leak through GET."""
    analysis = await repository.create_analysis(
        repo_url="https://github.com/owner/repo", issue_number=23
    )
    await repository.transition(
        analysis.analysis_id,
        PersistentAnalysisStatus.INGESTING,
        progress={
            "nested": {
                "raw_message": "Authorization sk-x",
                "safe": "working",
            }
        },
        counters={"tool_calls": 1, "input_messages": ["sk-y"]},
    )

    with pytest.raises(ValueError):
        await repository.transition(
            analysis.analysis_id,
            PersistentAnalysisStatus.INGESTING,
            counters={"tool_calls": -1},
        )
    with pytest.raises(ValueError):
        await repository.transition(
            analysis.analysis_id,
            PersistentAnalysisStatus.INGESTING,
            progress={"unsafe": object()},
        )

    async with repository.sessions.begin() as session:
        row = await session.scalar(
            select(AnalysisJobRow).where(AnalysisJobRow.id == analysis.analysis_id)
        )
        assert row is not None
        row.error_code = "hostile"
        row.error_message = "Authorization sk-z"

    stored = await repository.get_analysis(analysis.analysis_id)

    assert stored.progress == {
        "nested": {"raw_message": "[REDACTED]", "safe": "working"}
    }
    assert stored.counters == {
        "tool_calls": 1,
        "input_messages": "[REDACTED]",
    }
    assert (stored.error_code, stored.error_message) == (
        "internal_error",
        "Analysis failed safely.",
    )


@pytest.mark.anyio
async def test_event_payloads_are_bounded_on_write_and_sanitized_on_read(
    repository: AnalysisRepository,
) -> None:
    """Breaks if legacy event JSON or oversized observable data bypasses safety."""
    analysis = await repository.create_analysis(
        repo_url="https://github.com/owner/repo", issue_number=25
    )
    event = await repository.append_event(
        analysis.analysis_id, "safe_event", {"safe": "stored"}
    )
    with pytest.raises(ValueError):
        await repository.append_event(
            analysis.analysis_id,
            "oversized_event",
            {"safe": "x" * 20_000},
        )

    async with repository.sessions.begin() as session:
        row = await session.scalar(
            select(AnalysisEventRow).where(
                AnalysisEventRow.analysis_id == analysis.analysis_id,
                AnalysisEventRow.sequence == event.sequence,
            )
        )
        assert row is not None
        row.data = {
            "nested": {
                "input_messages": ["hidden"],
                "token": "sk-q",
            }
        }

    replayed = await repository.list_events(analysis.analysis_id)
    assert replayed[0].data == {
        "nested": {
            "input_messages": "[REDACTED]",
            "token": "[REDACTED]",
        }
    }
