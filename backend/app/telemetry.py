from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any


_LOG = logging.getLogger("reposcope.telemetry")
_ALLOWED = frozenset(
    {
        "analysis_id",
        "case_id",
        "status",
        "error_code",
        "tool_calls",
        "model_attempts",
        "citations_emitted",
        "citations_valid",
        "removed_count",
    }
)


class StructuredTelemetry:
    """Emit small, allowlisted JSON observations without user/model content."""

    def __init__(self, *, service: str, clock: Callable[[], float] = time.monotonic) -> None:
        if not service or not service.strip():
            raise ValueError("telemetry service must be nonblank")
        self._service = service
        self._clock = clock

    def start(self) -> float:
        return self._clock()

    def emit(self, event: str, *, started_at: float | None = None, **fields: Any) -> None:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": "INFO",
            "service": self._service,
            "event": event,
        }
        if started_at is not None:
            payload["duration_ms"] = max(0, round((self._clock() - started_at) * 1000))
        for key in sorted(_ALLOWED):
            value = fields.get(key)
            if value is None:
                continue
            if type(value) in {int, float, bool}:
                payload[key] = value
            elif isinstance(value, str):
                payload[key] = value[:200]
        _LOG.info(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
