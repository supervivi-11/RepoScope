from __future__ import annotations

from pathlib import Path

from .contracts import BenchmarkSlot, CurationCandidate
from .jsonl import JsonlContractError, read_jsonl


def load_slot_catalog(path: Path) -> tuple[BenchmarkSlot, ...]:
    slots = read_jsonl(path, BenchmarkSlot)
    expected = {*(f"dev-{index:02d}" for index in range(1, 7)), *(f"hidden-{index:02d}" for index in range(1, 7))}
    if {item.slot_id for item in slots} != expected:
        raise JsonlContractError("slot catalog must contain the fixed twelve IDs")
    if sum(item.split == "development" for item in slots) != 6:
        raise JsonlContractError("slot catalog must have a six/six split")
    return slots


def load_candidate_catalog(path: Path) -> tuple[CurationCandidate, ...]:
    candidates = read_jsonl(path, CurationCandidate)
    if not 1 <= len(candidates) <= 12:
        raise JsonlContractError("candidate catalog must contain one to twelve rows")
    repositories = [item.repo_url.casefold() for item in candidates]
    if len(set(repositories)) != len(repositories):
        raise JsonlContractError("candidate catalog allows one candidate per repository")
    return candidates
