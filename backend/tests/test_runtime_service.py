from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.analysis.failures import PublicFailure
from app.analysis.queue import ClaimedAnalysis
from app.analysis.service import WorkerService
from app.analysis.janitor import SnapshotJanitor


def _claim(attempt: int = 1) -> ClaimedAnalysis:
    from app.analysis.domain import PersistentAnalysisStatus

    return ClaimedAnalysis(
        analysis_id=uuid4(),
        worker_id="worker-test",
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
        attempt_count=attempt,
        status=PersistentAnalysisStatus.QUEUED,
    )


@pytest.mark.anyio
async def test_worker_service_claims_exactly_one_job_per_run() -> None:
    claim = _claim()
    queue = SimpleNamespace(claim_next=AsyncMock(return_value=claim))
    worker = SimpleNamespace(run=AsyncMock())
    repository = SimpleNamespace(fail_safe=AsyncMock())
    service = WorkerService(
        queue=queue,
        worker=worker,
        repository=repository,
        worker_id="worker-test",
    )

    assert await service.run_once() is True
    queue.claim_next.assert_awaited_once_with(worker_id="worker-test")
    worker.run.assert_awaited_once_with(claim)


@pytest.mark.anyio
async def test_worker_service_marks_poison_claim_failed_without_running_it() -> None:
    claim = _claim(attempt=4)
    queue = SimpleNamespace(claim_next=AsyncMock(return_value=claim))
    worker = SimpleNamespace(run=AsyncMock())
    repository = SimpleNamespace(fail_safe=AsyncMock())
    service = WorkerService(
        queue=queue,
        worker=worker,
        repository=repository,
        worker_id="worker-test",
        max_attempts=3,
    )

    assert await service.run_once() is True
    worker.run.assert_not_awaited()
    failure = repository.fail_safe.await_args.args[1]
    assert isinstance(failure, PublicFailure)
    assert failure.code == "attempts_exhausted"
    assert repository.fail_safe.await_args.kwargs["lease"] == claim


@pytest.mark.anyio
async def test_snapshot_janitor_only_removes_expired_inactive_real_directories(
    tmp_path: Path,
) -> None:
    root = tmp_path / "snapshots"
    root.mkdir()
    expired = root / "expired"
    active = root / "active"
    fresh = root / "fresh"
    outside = tmp_path / "outside"
    for path in (expired, active, fresh, outside):
        path.mkdir()
    old = datetime.now(UTC) - timedelta(hours=30)
    for path in (expired, active, outside):
        os.utime(path, (old.timestamp(), old.timestamp()))
    link = root / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        link = None

    repository = SimpleNamespace(
        active_snapshot_paths=AsyncMock(return_value={active.resolve()})
    )
    janitor = SnapshotJanitor(root=root, repository=repository)

    assert await janitor.run_once(now=datetime.now(UTC)) == 1
    assert not expired.exists()
    assert active.exists() and fresh.exists() and outside.exists()
    if link is not None:
        assert link.is_symlink()
    assert await janitor.run_once(now=datetime.now(UTC)) == 0
