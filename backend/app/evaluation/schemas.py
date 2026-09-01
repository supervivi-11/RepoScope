from __future__ import annotations

import json
from pathlib import Path

from .contracts import (
    BenchmarkCase,
    BenchmarkGold,
    BenchmarkResult,
    BenchmarkSlot,
    CurationCandidate,
    EvaluationSummary,
    ScriptedPrediction,
)


_SCHEMAS = {
    "candidate.v1.schema.json": CurationCandidate,
    "slot.v1.schema.json": BenchmarkSlot,
    "case.v1.schema.json": BenchmarkCase,
    "gold.v1.schema.json": BenchmarkGold,
    "result.v1.schema.json": BenchmarkResult,
    "summary.v1.schema.json": EvaluationSummary,
    "script.v1.schema.json": ScriptedPrediction,
}


def export_schemas(directory: Path, *, check: bool = False) -> tuple[Path, ...]:
    changed: list[Path] = []
    for filename, model in _SCHEMAS.items():
        path = directory / filename
        payload = (
            json.dumps(model.model_json_schema(), ensure_ascii=False, sort_keys=True, indent=2)
            + "\n"
        ).encode("utf-8")
        existing = path.read_bytes() if path.exists() else None
        if existing == payload:
            continue
        changed.append(path)
        if not check:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
    return tuple(changed)
