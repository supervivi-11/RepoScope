from __future__ import annotations

from pathlib import Path

from .contracts import BenchmarkSlot
from .jsonl import JsonlContractError, read_jsonl


def load_slot_catalog(path: Path) -> tuple[BenchmarkSlot, ...]:
    slots = read_jsonl(path, BenchmarkSlot)
    expected = {*(f"dev-{index:02d}" for index in range(1, 7)), *(f"hidden-{index:02d}" for index in range(1, 7))}
    if {item.slot_id for item in slots} != expected:
        raise JsonlContractError("slot catalog must contain the fixed twelve IDs")
    if sum(item.split == "development" for item in slots) != 6:
        raise JsonlContractError("slot catalog must have a six/six split")
    return slots
