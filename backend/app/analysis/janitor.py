from __future__ import annotations

import asyncio
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.snapshot_paths import validate_snapshot_root
from app.telemetry import StructuredTelemetry


class SnapshotJanitor:
    """Delete only expired, inactive directories under one dedicated root."""

    def __init__(
        self,
        *,
        root: Path,
        repository: Any,
        retention: timedelta = timedelta(hours=24),
        interval: float = 60 * 60,
        telemetry: StructuredTelemetry | None = None,
    ) -> None:
        if retention <= timedelta(0) or interval <= 0:
            raise ValueError("janitor limits must be positive")
        self._root = validate_snapshot_root(root)
        self._repository = repository
        self._retention = retention
        self._interval = interval
        self._telemetry = telemetry or StructuredTelemetry(service="worker")

    async def run_once(self, *, now: datetime | None = None) -> int:
        started = self._telemetry.start()
        active = {
            Path(path).resolve()
            for path in await self._repository.active_snapshot_paths()
        }
        timestamp = now or datetime.now(UTC)
        removed = await asyncio.to_thread(self._cleanup, active, timestamp)
        self._telemetry.emit(
            "snapshot_cleanup_finished",
            started_at=started,
            removed_count=removed,
        )
        return removed

    def _cleanup(self, active: set[Path], now: datetime) -> int:
        if not self._root.exists() or self._root.is_symlink():
            return 0
        removed = 0
        for candidate in self._root.iterdir():
            if candidate.is_symlink() or not candidate.is_dir():
                continue
            resolved = candidate.resolve(strict=False)
            if (
                resolved == self._root
                or not resolved.is_relative_to(self._root)
                or resolved.parent != self._root
                or resolved in active
            ):
                continue
            modified = datetime.fromtimestamp(candidate.stat().st_mtime, UTC)
            if now - modified < self._retention:
                continue
            shutil.rmtree(candidate)
            removed += 1
        return removed

    async def run_forever(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self.run_once()
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._interval)
            except TimeoutError:
                pass
