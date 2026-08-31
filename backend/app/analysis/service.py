from __future__ import annotations

import asyncio
import logging
from typing import Any

from .failures import PublicFailure


_LOG = logging.getLogger("reposcope.worker")
_POISON_FAILURE = PublicFailure(
    "attempts_exhausted",
    "Analysis retry budget was exhausted.",
    500,
)


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

    async def run_once(self) -> bool:
        claim = await self._queue.claim_next(worker_id=self._worker_id)
        if claim is None:
            return False
        if claim.attempt_count > self._max_attempts:
            await self._repository.fail_safe(
                claim.analysis_id, _POISON_FAILURE, lease=claim
            )
            _LOG.warning("worker_job_failed code=attempts_exhausted analysis_id=%s", claim.analysis_id)
            return True
        await self._worker.run(claim)
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
