from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agent import AnalysisReport
from app.analysis import (
    AnalysisConflictError,
    AnalysisRepository,
    PersistentAnalysisStatus,
)
from app.db import metadata


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
        analysis.analysis_id, PersistentAnalysisStatus.REVIEW_READY
    )
    with pytest.raises(AnalysisConflictError):
        await repository.submit_feedback(
            analysis.analysis_id, action="revise", comment="Try another revision."
        )

    accepted = await repository.submit_feedback(analysis.analysis_id, action="accept")
    accepted_again = await repository.submit_feedback(
        analysis.analysis_id, action="accept"
    )
    assert accepted.status is PersistentAnalysisStatus.COMPLETED
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
