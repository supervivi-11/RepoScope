from __future__ import annotations

import asyncio
import logging
from typing import Any

from .failures import ATTEMPTS_EXHAUSTED_FAILURE
from app.telemetry import StructuredTelemetry


_LOG = logging.getLogger("reposcope.worker")


class DisabledWorkerService:
    """Keep maintenance alive without claiming jobs when no model is configured."""

    def __init__(self, telemetry: StructuredTelemetry | None = None) -> None:
        self._telemetry = telemetry or StructuredTelemetry(service="worker")

    async def run_forever(self, stop: asyncio.Event) -> None:
        _LOG.warning("worker_disabled code=model_not_configured")
        self._telemetry.emit(
            "worker_disabled",
            status="DEGRADED",
            error_code="model_not_configured",
        )
        await stop.wait()


class WorkerService:
    """Supervise durable claims without exposing exception or secret details."""

    def __init__(
        self,
        *,
        queue: Any,
        worker: Any,
        repository: Any,
        worker_id: str,
        max_attempts: int = 3,
        idle_delay: float = 0.25,
        max_backoff: float = 5.0,
        telemetry: StructuredTelemetry | None = None,
    ) -> None:
        if not worker_id.strip():
            raise ValueError("worker ID must be nonblank")
        if max_attempts < 1 or idle_delay <= 0 or max_backoff < idle_delay:
            raise ValueError("worker service limits are invalid")
        self._queue = queue
        self._worker = worker
        self._repository = repository
        self._worker_id = worker_id
        self._max_attempts = max_attempts
        self._idle_delay = idle_delay
        self._max_backoff = max_backoff
        self._telemetry = telemetry or StructuredTelemetry(service="worker")

    async def run_once(self) -> bool:
        claim = await self._queue.claim_next(worker_id=self._worker_id)
        if claim is None:
            return False
        started = self._telemetry.start()
        if claim.attempt_count > self._max_attempts:
            await self._repository.fail_safe(
                claim.analysis_id, ATTEMPTS_EXHAUSTED_FAILURE, lease=claim
            )
            _LOG.warning("worker_job_failed code=attempts_exhausted analysis_id=%s", claim.analysis_id)
            self._telemetry.emit(
                "analysis_attempt_finished",
                started_at=started,
                analysis_id=str(claim.analysis_id),
                status="FAILED",
                error_code="attempts_exhausted",
            )
            return True
        try:
            result = await self._worker.run(claim)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._telemetry.emit(
                "analysis_attempt_finished",
                started_at=started,
                analysis_id=str(claim.analysis_id),
                status="FAILED",
                error_code="internal_error",
            )
            raise
        counters = result.counters if isinstance(result.counters, dict) else {}
        raw_status = getattr(result, "status", None)
        status_value = getattr(raw_status, "value", raw_status)
        status = status_value if isinstance(status_value, str) else "UNKNOWN"
        self._telemetry.emit(
            "analysis_attempt_finished",
            started_at=started,
            analysis_id=str(claim.analysis_id),
            status=status,
            error_code=getattr(result, "error_code", None),
            tool_calls=counters.get("tool_calls"),
            model_attempts=counters.get("model_attempts"),
        )
        return True

    async def run_forever(self, stop: asyncio.Event) -> None:
        backoff = self._idle_delay
        while not stop.is_set():
            try:
                worked = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOG.error("worker_iteration_failed code=internal_error")
                worked = False
            if worked:
                backoff = self._idle_delay
                continue
            try:
                await asyncio.wait_for(stop.wait(), timeout=backoff)
            except TimeoutError:
                pass
            backoff = min(self._max_backoff, backoff * 2)
