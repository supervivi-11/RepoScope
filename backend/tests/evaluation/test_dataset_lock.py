from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.evaluation.catalog import (
    dataset_digest,
    load_candidate_catalog,
    load_case_catalog,
    load_locked_slot_catalog,
    lock_candidates,
    validate_locked_dataset,
)
from app.evaluation.cli import main
from app.evaluation.contracts import (
    BenchmarkCase,
    BenchmarkGold,
    LockedBenchmarkSlot,
    ScriptedPrediction,
)
from app.evaluation.jsonl import read_jsonl, write_jsonl


EXPECTED_SLOTS = {
    "dev-01": "pyinvoke-invoke-issue-533",
    "dev-02": "tox-dev-platformdirs-issue-207",
    "dev-03": "hynek-structlog-issue-476",
    "dev-04": "dateutil-dateutil-issue-926",
    "dev-05": "pallets-click-issue-2819",
    "dev-06": "pallets-flask-issue-2267",
    "hidden-01": "pallets-jinja-issue-1198",
    "hidden-02": "pydantic-pydantic-settings-issue-441",
    "hidden-03": "python-hyper-h11-issue-92",
    "hidden-04": "pytest-dev-pluggy-issue-544",
    "hidden-05": "python-poetry-tomlkit-issue-261",
    "hidden-06": "more-itertools-more-itertools-issue-658",
}
EXPECTED_DATASET_DIGEST = "de76c423ffe936743f979f35cc634caed064e571b3f5c9563b219ac5025ceea7"


def _committed_candidates():
    root = Path(__file__).resolve().parents[3]
    return load_candidate_catalog(root / "evals" / "curation-candidates.v1.jsonl")


def test_twelve_candidates_lock_to_the_frozen_six_six_mapping() -> None:
    cases, slots = lock_candidates(_committed_candidates())

    assert {slot.slot_id: slot.case_id for slot in slots} == EXPECTED_SLOTS
    assert sum(case.split == "development" for case in cases) == 6
    assert sum(case.split == "hidden" for case in cases) == 6
    assert dataset_digest(cases) == EXPECTED_DATASET_DIGEST
    validate_locked_dataset(_committed_candidates(), cases, slots)


def test_committed_locked_artifacts_and_public_development_gold_are_exact() -> None:
    root = Path(__file__).resolve().parents[3]
    cases = load_case_catalog(root / "evals" / "benchmark-cases.v1.jsonl")
    slots = load_locked_slot_catalog(root / "evals" / "benchmark-slots.v2.jsonl")
    development_gold = read_jsonl(
        root / "evals" / "development-gold.v1.jsonl", BenchmarkGold
    )

    validate_locked_dataset(_committed_candidates(), cases, slots)
    assert {slot.slot_id: slot.case_id for slot in slots} == EXPECTED_SLOTS
    assert dataset_digest(cases) == EXPECTED_DATASET_DIGEST
    assert (root / "evals" / "benchmark-cases.v1.sha256").read_text(
        encoding="ascii"
    ) == f"{EXPECTED_DATASET_DIGEST}\n"
    assert {gold.case_id for gold in development_gold} == {
        slot.case_id for slot in slots if slot.split == "development"
    }
    assert not (
        {gold.case_id for gold in development_gold}
        & {slot.case_id for slot in slots if slot.split == "hidden"}
    )


def test_locked_dataset_rejects_a_case_moved_between_splits() -> None:
    candidates = _committed_candidates()
    cases, slots = lock_candidates(candidates)
    changed = tuple(
        case.model_copy(update={"split": "hidden"}) if case.case_id == "pyinvoke-invoke-issue-533" else case
        for case in cases
    )

    with pytest.raises(ValueError, match="deterministic lock"):
        validate_locked_dataset(candidates, changed, slots)


def test_lock_cli_reads_only_candidates_and_writes_canonical_public_artifacts(
    tmp_path: Path,
) -> None:
    candidates_path = Path(__file__).resolve().parents[3] / "evals" / "curation-candidates.v1.jsonl"
    cases_path = tmp_path / "cases.jsonl"
    slots_path = tmp_path / "slots.jsonl"
    digest_path = tmp_path / "cases.sha256"

    assert main(
        [
            "lock-dataset",
            "--candidates",
            str(candidates_path),
            "--cases-output",
            str(cases_path),
            "--slots-output",
            str(slots_path),
            "--digest-output",
            str(digest_path),
        ]
    ) == 0

    cases = read_jsonl(cases_path, BenchmarkCase)
    slots = read_jsonl(slots_path, LockedBenchmarkSlot)
    assert len(cases) == len(slots) == 12
    assert hashlib.sha256(cases_path.read_bytes()).hexdigest() == EXPECTED_DATASET_DIGEST
    assert digest_path.read_text(encoding="ascii") == f"{EXPECTED_DATASET_DIGEST}\n"


def test_split_run_keeps_the_complete_dataset_digest(tmp_path: Path) -> None:
    cases, _ = lock_candidates(_committed_candidates())
    cases_path = tmp_path / "cases.jsonl"
    scripts_path = tmp_path / "scripts.jsonl"
    results_path = tmp_path / "results.jsonl"
    digest_path = tmp_path / "cases.sha256"
    write_jsonl(cases_path, cases)
    digest_path.write_text(f"{EXPECTED_DATASET_DIGEST}\n", encoding="ascii")
    development = tuple(case for case in cases if case.split == "development")
    write_jsonl(
        scripts_path,
        tuple(
            ScriptedPrediction(
                schema_version="reposcope.eval.script.v1",
                case_id=case.case_id,
                system="issue_only",
                predicted_files=(),
                report_outcome="insufficient_evidence",
            )
            for case in development
        ),
    )

    assert main(
        [
            "run",
            "--system",
            "issue_only",
            "--split",
            "development",
            "--cases",
            str(cases_path),
            "--dataset-digest-file",
            str(digest_path),
            "--scripted-predictions",
            str(scripts_path),
            "--output",
            str(results_path),
        ]
    ) == 0

    from app.evaluation.contracts import BenchmarkResult

    results = read_jsonl(results_path, BenchmarkResult)
    assert len(results) == 6
    assert {result.dataset_digest for result in results} == {EXPECTED_DATASET_DIGEST}
