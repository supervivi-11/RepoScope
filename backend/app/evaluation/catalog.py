from __future__ import annotations

import hashlib
from pathlib import Path

from app.ingestion import RepositoryCoordinates

from .contracts import (
    BenchmarkCase,
    BenchmarkSlot,
    CurationCandidate,
    LockedBenchmarkSlot,
)
from .jsonl import JsonlContractError, canonical_jsonl_bytes, read_jsonl


def load_slot_catalog(path: Path) -> tuple[BenchmarkSlot, ...]:
    slots = read_jsonl(path, BenchmarkSlot)
    expected = {
        *(f"dev-{index:02d}" for index in range(1, 7)),
        *(f"hidden-{index:02d}" for index in range(1, 7)),
    }
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


def split_key(candidate: CurationCandidate) -> str:
    coordinates = RepositoryCoordinates.parse(candidate.repo_url)
    value = (
        f"reposcope-v1|{coordinates.owner.casefold()}/"
        f"{coordinates.repository.casefold()}#{candidate.issue_number}"
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def lock_candidates(
    candidates: tuple[CurationCandidate, ...],
) -> tuple[tuple[BenchmarkCase, ...], tuple[LockedBenchmarkSlot, ...]]:
    if len(candidates) != 12:
        raise ValueError("dataset lock requires exactly twelve candidates")
    repositories = [candidate.repo_url.casefold() for candidate in candidates]
    if len(set(repositories)) != len(repositories):
        raise ValueError("dataset lock requires one candidate per repository")
    keyed = tuple((split_key(candidate), candidate) for candidate in candidates)
    if len({digest for digest, _ in keyed}) != len(keyed):
        raise ValueError("dataset lock split digest collision")
    ordered = tuple(sorted(keyed, key=lambda item: item[0]))
    cases: list[BenchmarkCase] = []
    slots: list[LockedBenchmarkSlot] = []
    for position, (digest, candidate) in enumerate(ordered):
        split = "development" if position < 6 else "hidden"
        split_position = position + 1 if split == "development" else position - 5
        slot_id = f"{'dev' if split == 'development' else 'hidden'}-{split_position:02d}"
        cases.append(
            BenchmarkCase(
                schema_version="reposcope.eval.case.v1",
                case_id=candidate.candidate_id,
                split=split,
                repo_url=candidate.repo_url,
                issue_number=candidate.issue_number,
                issue_title=candidate.issue_title,
                issue_body=candidate.issue_body,
                pre_fix_commit_sha=candidate.pre_fix_commit_sha,
                snapshot_tree_digest=candidate.snapshot_tree_digest,
            )
        )
        slots.append(
            LockedBenchmarkSlot(
                schema_version="reposcope.eval.slot.v2",
                slot_id=slot_id,
                split=split,
                case_id=candidate.candidate_id,
                split_key_digest=digest,
            )
        )
    return (
        tuple(sorted(cases, key=lambda item: item.case_id)),
        tuple(sorted(slots, key=lambda item: item.slot_id)),
    )


def load_case_catalog(path: Path) -> tuple[BenchmarkCase, ...]:
    cases = read_jsonl(path, BenchmarkCase)
    if len(cases) != 12:
        raise JsonlContractError("locked case catalog must contain twelve rows")
    if sum(case.split == "development" for case in cases) != 6:
        raise JsonlContractError("locked case catalog must have a six/six split")
    repositories = [case.repo_url.casefold() for case in cases]
    if len(set(repositories)) != len(repositories):
        raise JsonlContractError("locked case catalog allows one case per repository")
    return cases


def load_locked_slot_catalog(path: Path) -> tuple[LockedBenchmarkSlot, ...]:
    slots = read_jsonl(path, LockedBenchmarkSlot)
    expected = {*(f"dev-{index:02d}" for index in range(1, 7)), *(f"hidden-{index:02d}" for index in range(1, 7))}
    if {slot.slot_id for slot in slots} != expected:
        raise JsonlContractError("locked slot catalog must contain the fixed twelve IDs")
    return slots


def validate_locked_dataset(
    candidates: tuple[CurationCandidate, ...],
    cases: tuple[BenchmarkCase, ...],
    slots: tuple[LockedBenchmarkSlot, ...],
) -> None:
    expected_cases, expected_slots = lock_candidates(candidates)
    if tuple(sorted(cases, key=lambda item: item.case_id)) != expected_cases:
        raise ValueError("cases do not match the deterministic lock")
    if tuple(sorted(slots, key=lambda item: item.slot_id)) != expected_slots:
        raise ValueError("slots do not match the deterministic lock")


def dataset_digest(cases: tuple[BenchmarkCase, ...]) -> str:
    return hashlib.sha256(canonical_jsonl_bytes(cases)).hexdigest()
