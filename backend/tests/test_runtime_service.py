from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.analysis.failures import PublicFailure
from app.analysis.queue import ClaimedAnalysis
from app.analysis.service import WorkerService
from app.analysis.janitor import SnapshotJanitor
from app.config import Settings


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


@pytest.mark.parametrize(
    ("label", "root_factory"),
    [
        ("empty", lambda tmp: Path("")),
        ("relative-current", lambda tmp: Path(".")),
        ("current-directory", lambda tmp: Path.cwd()),
        ("filesystem-anchor", lambda tmp: Path(Path.cwd().anchor or "/")),
        ("home-directory", lambda tmp: Path.home()),
        ("non-dedicated-tmp-leaf", lambda tmp: tmp / "repo"),
    ],
)
def test_settings_and_janitor_reject_broad_snapshot_roots(
    label: str,
    root_factory: object,
    tmp_path: Path,
) -> None:
    bad_root = root_factory(tmp_path)  # type: ignore[operator]
    if label == "non-dedicated-tmp-leaf":
        bad_root.mkdir()
    repository = SimpleNamespace(active_snapshot_paths=AsyncMock(return_value=set()))

    with pytest.raises((ValidationError, ValueError)):
        Settings(snapshot_root=bad_root)
    with pytest.raises(ValueError):
        SnapshotJanitor(root=bad_root, repository=repository)


def test_snapshot_root_rejects_symlink_root(tmp_path: Path) -> None:
    target = tmp_path / "snapshots-target"
    link = tmp_path / "snapshots"
    target.mkdir()
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable in this environment: {exc}")
    repository = SimpleNamespace(active_snapshot_paths=AsyncMock(return_value=set()))

    with pytest.raises((ValidationError, ValueError)):
        Settings(snapshot_root=link)
    with pytest.raises(ValueError):
        SnapshotJanitor(root=link, repository=repository)


@pytest.mark.anyio
async def test_build_worker_runtime_accepts_compose_blank_openai_base_url(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from app.runtime import build_worker_runtime

    constructed: list[dict[str, object]] = []

    class FakeChatOpenAI:
        def __init__(self, **kwargs: object) -> None:
            constructed.append(kwargs)

    class FakeEngine:
        async def dispose(self) -> None:
            pass

    class FakeHttp:
        async def aclose(self) -> None:
            pass

    monkeypatch.setenv("REPOSCOPE_OPENAI_API_KEY", "sk-test-compose-secret")
    monkeypatch.setenv("REPOSCOPE_OPENAI_MODEL", "gpt-5-mini")
    monkeypatch.setenv("REPOSCOPE_OPENAI_BASE_URL", "")
    monkeypatch.setitem(
        sys.modules,
        "langchain_openai",
        SimpleNamespace(ChatOpenAI=FakeChatOpenAI),
    )

    runtime = build_worker_runtime(
        Settings(snapshot_root=tmp_path / "snapshots"),
        engine=FakeEngine(),  # type: ignore[arg-type]
        http=FakeHttp(),  # type: ignore[arg-type]
    )
    await runtime.close()

    assert constructed
    assert constructed[-1]["base_url"] is None


def test_build_worker_runtime_rejects_malformed_openai_base_without_logging_secret(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from app.runtime import build_worker_runtime

    class FakeEngine:
        async def dispose(self) -> None:
            pass

    class FakeHttp:
        async def aclose(self) -> None:
            pass

    monkeypatch.setenv("REPOSCOPE_OPENAI_API_KEY", "sk-test-do-not-log")
    monkeypatch.setenv("REPOSCOPE_OPENAI_BASE_URL", "ftp://bad.example")
    caplog.set_level(logging.DEBUG)

    with pytest.raises(ValidationError):
        build_worker_runtime(
            Settings(snapshot_root=tmp_path / "snapshots"),
            engine=FakeEngine(),  # type: ignore[arg-type]
            http=FakeHttp(),  # type: ignore[arg-type]
        )
    assert "sk-test-do-not-log" not in caplog.text


@pytest.mark.anyio
async def test_worker_main_exits_nonzero_and_disposes_when_worker_fails() -> None:
    from app import worker_main

    class FailingService:
        async def run_forever(self, stop: asyncio.Event) -> None:
            raise RuntimeError("boom")

    class WaitingJanitor:
        def __init__(self) -> None:
            self.cleaned_up = False

        async def run_forever(self, stop: asyncio.Event) -> None:
            try:
                await stop.wait()
            finally:
                self.cleaned_up = True

    janitor = WaitingJanitor()
    runtime = SimpleNamespace(
        service=FailingService(),
        janitor=janitor,
        close=AsyncMock(),
    )

    exit_code = await worker_main.run_worker(
        runtime_factory=lambda: runtime,
        signal_installer=lambda _request_stop: None,
    )

    assert exit_code == 1
    assert janitor.cleaned_up is True
    runtime.close.assert_awaited_once()


@pytest.mark.anyio
async def test_worker_main_exits_nonzero_and_disposes_when_janitor_fails() -> None:
    from app import worker_main

    class WaitingService:
        def __init__(self) -> None:
            self.cleaned_up = False

        async def run_forever(self, stop: asyncio.Event) -> None:
            try:
                await stop.wait()
            finally:
                self.cleaned_up = True

    class FailingJanitor:
        async def run_forever(self, stop: asyncio.Event) -> None:
            raise RuntimeError("janitor failed")

    service = WaitingService()
    runtime = SimpleNamespace(
        service=service,
        janitor=FailingJanitor(),
        close=AsyncMock(),
    )

    exit_code = await worker_main.run_worker(
        runtime_factory=lambda: runtime,
        signal_installer=lambda _request_stop: None,
    )

    assert exit_code == 1
    assert service.cleaned_up is True
    runtime.close.assert_awaited_once()


@pytest.mark.anyio
async def test_worker_main_treats_unexpected_task_cancellation_as_failure() -> None:
    from app import worker_main

    class CancelledService:
        async def run_forever(self, stop: asyncio.Event) -> None:
            raise asyncio.CancelledError()

    class WaitingJanitor:
        async def run_forever(self, stop: asyncio.Event) -> None:
            await stop.wait()

    runtime = SimpleNamespace(
        service=CancelledService(),
        janitor=WaitingJanitor(),
        close=AsyncMock(),
    )

    exit_code = await worker_main.run_worker(
        runtime_factory=lambda: runtime,
        signal_installer=lambda _request_stop: None,
    )

    assert exit_code == 1
    runtime.close.assert_awaited_once()


@pytest.mark.anyio
async def test_worker_main_graceful_signal_stops_tasks_and_disposes_once() -> None:
    from app import worker_main

    signal_callback = None

    class CooperativeComponent:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.stopped = False

        async def run_forever(self, stop: asyncio.Event) -> None:
            self.started.set()
            await stop.wait()
            self.stopped = True

    def install_signal(request_stop: object) -> None:
        nonlocal signal_callback
        signal_callback = request_stop

    service = CooperativeComponent()
    janitor = CooperativeComponent()
    runtime = SimpleNamespace(service=service, janitor=janitor, close=AsyncMock())

    task = asyncio.create_task(
        worker_main.run_worker(
            runtime_factory=lambda: runtime,
            signal_installer=install_signal,
        )
    )
    await asyncio.wait_for(service.started.wait(), timeout=1)
    await asyncio.wait_for(janitor.started.wait(), timeout=1)

    assert signal_callback is not None
    signal_callback()

    assert await asyncio.wait_for(task, timeout=1) == 0
    assert service.stopped is True
    assert janitor.stopped is True
    runtime.close.assert_awaited_once()
