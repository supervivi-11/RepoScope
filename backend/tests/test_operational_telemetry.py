from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.analysis.janitor import SnapshotJanitor
from app.analysis.service import WorkerService
from app.telemetry import StructuredTelemetry


class OneClaimQueue:
    def __init__(self, claim):
        self.claim = claim

    async def claim_next(self, *, worker_id):
        value, self.claim = self.claim, None
        return value


@pytest.mark.anyio
async def test_worker_service_emits_one_safe_terminal_metric(
    caplog: pytest.LogCaptureFixture,
) -> None:
    claim = SimpleNamespace(analysis_id=uuid4(), attempt_count=1)
    worker = SimpleNamespace(
        run=lambda value: _async_value(
            SimpleNamespace(status=SimpleNamespace(value="REVIEW_READY"), counters={"tool_calls": 4})
        )
    )
    telemetry = StructuredTelemetry(service="worker", clock=lambda: 10.0)
    service = WorkerService(
        queue=OneClaimQueue(claim),
        worker=worker,
        repository=SimpleNamespace(),
        worker_id="worker-1",
        telemetry=telemetry,
    )

    with caplog.at_level(logging.INFO, logger="reposcope.telemetry"):
        assert await service.run_once() is True

    payloads = [json.loads(item.message) for item in caplog.records]
    terminals = [item for item in payloads if item["event"] == "analysis_attempt_finished"]
    assert len(terminals) == 1
    assert terminals[0]["status"] == "REVIEW_READY"
    assert terminals[0]["tool_calls"] == 4
    assert "exception" not in terminals[0]


@pytest.mark.anyio
async def test_worker_service_emits_the_canonical_code_for_handled_failures(
    caplog: pytest.LogCaptureFixture,
) -> None:
    claim = SimpleNamespace(analysis_id=uuid4(), attempt_count=1)
    result = SimpleNamespace(
        status=SimpleNamespace(value="FAILED"),
        counters={},
        error_code="upstream_unavailable",
    )
    service = WorkerService(
        queue=OneClaimQueue(claim),
        worker=SimpleNamespace(run=lambda value: _async_value(result)),
        repository=SimpleNamespace(),
        worker_id="worker-1",
    )

    with caplog.at_level(logging.INFO, logger="reposcope.telemetry"):
        await service.run_once()

    payload = json.loads(caplog.records[-1].message)
    assert payload["status"] == "FAILED"
    assert payload["error_code"] == "upstream_unavailable"


@pytest.mark.anyio
async def test_snapshot_janitor_reports_removed_count_without_paths(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    root = tmp_path / "reposcope-snapshots"
    expired = root / "expired"
    expired.mkdir(parents=True)
    old = datetime.now(UTC) - timedelta(days=2)
    timestamp = old.timestamp()
    import os

    os.utime(expired, (timestamp, timestamp))
    repository = SimpleNamespace(active_snapshot_paths=lambda: _async_value(set()))
    janitor = SnapshotJanitor(
        root=root,
        repository=repository,
        telemetry=StructuredTelemetry(service="worker", clock=lambda: 20.0),
    )

    with caplog.at_level(logging.INFO, logger="reposcope.telemetry"):
        assert await janitor.run_once(now=datetime.now(UTC)) == 1

    payload = json.loads(caplog.records[-1].message)
    assert payload["event"] == "snapshot_cleanup_finished"
    assert payload["removed_count"] == 1
    assert str(expired) not in caplog.records[-1].message


async def _async_value(value):
    return value
