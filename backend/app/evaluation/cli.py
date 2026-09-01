from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path

from .catalog import (
    dataset_digest,
    load_candidate_catalog,
    load_case_catalog,
    load_locked_slot_catalog,
    load_slot_catalog,
    lock_candidates,
    validate_locked_dataset,
)
from .contracts import BenchmarkCase, BenchmarkGold, BenchmarkResult, ScriptedPrediction
from .jsonl import read_jsonl, write_jsonl
from .metrics import score_benchmark
from .runners import IssueOnlyRunner, RepoScopeRunner


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="reposcope-eval")
    commands = parser.add_subparsers(dest="command", required=True)
    validate_slots = commands.add_parser("validate-slots")
    validate_slots.add_argument("catalog")
    validate_candidates = commands.add_parser("validate-candidates")
    validate_candidates.add_argument("catalog")
    lock = commands.add_parser("lock-dataset")
    lock.add_argument("--candidates", required=True)
    lock.add_argument("--cases-output", required=True)
    lock.add_argument("--slots-output", required=True)
    lock.add_argument("--digest-output", required=True)
    validate_dataset = commands.add_parser("validate-dataset")
    validate_dataset.add_argument("--candidates", required=True)
    validate_dataset.add_argument("--cases", required=True)
    validate_dataset.add_argument("--slots", required=True)
    validate_dataset.add_argument("--development-gold", required=True)
    validate_dataset.add_argument("--hidden-gold")
    validate_dataset.add_argument("--snapshots-root")
    run = commands.add_parser("run")
    run.add_argument("--system", choices=("issue_only", "reposcope"), required=True)
    run.add_argument("--cases", required=True)
    run.add_argument("--scripted-predictions", required=True)
    run.add_argument("--output", required=True)
    run.add_argument("--snapshots-root")
    run.add_argument("--split", choices=("development", "hidden", "all"), default="all")
    run.add_argument("--dataset-digest-file")
    run.add_argument("--allow-unlocked-fixture", action="store_true", help=argparse.SUPPRESS)
    score = commands.add_parser("score")
    score.add_argument("--cases", required=True)
    score.add_argument("--gold", required=True)
    score.add_argument("--results", required=True)
    score.add_argument("--snapshots-root")
    score.add_argument("--output", required=True)
    score.add_argument("--split", choices=("development", "hidden", "all"), default="all")
    score.add_argument("--dataset-digest-file")
    score.add_argument("--allow-unlocked-fixture", action="store_true", help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "validate-slots":
        slots = load_slot_catalog(Path(args.catalog))
        development = sum(item.split == "development" for item in slots)
        hidden = len(slots) - development
        print(
            f"validated {len(slots)} metadata-only slots "
            f"({development} development, {hidden} hidden)"
        )
        return 0
    if args.command == "validate-candidates":
        candidates = load_candidate_catalog(Path(args.catalog))
        noun = "candidate" if len(candidates) == 1 else "candidates"
        print(
            f"validated {len(candidates)} qualified pre-split {noun}; "
            "no split was assigned and no model was called"
        )
        return 0
    if args.command == "lock-dataset":
        candidates = load_candidate_catalog(Path(args.candidates))
        cases, slots = lock_candidates(candidates)
        write_jsonl(Path(args.cases_output), cases)
        write_jsonl(Path(args.slots_output), slots)
        digest = dataset_digest(cases)
        _write_bytes(Path(args.digest_output), f"{digest}\n".encode("ascii"))
        print(
            "locked 12 cases into a deterministic 6/6 split; "
            f"dataset digest {digest}; no model was called"
        )
        return 0
    if args.command == "validate-dataset":
        return _validate_dataset(args)
    if args.command == "run":
        return asyncio.run(_run_scripted(args))
    if args.command == "score":
        return _score(args)
    raise AssertionError("unreachable")


async def _run_scripted(args: argparse.Namespace) -> int:
    complete_cases, complete_digest = _load_execution_cases(args)
    cases = _select_cases(complete_cases, args.split)
    scripts = read_jsonl(Path(args.scripted_predictions), ScriptedPrediction)
    script_by_case = {item.case_id: item for item in scripts if item.system == args.system}
    if set(script_by_case) != {item.case_id for item in cases}:
        raise ValueError("scripts must contain exactly one row for every selected case")
    results = []
    for case in cases:
        scripted = script_by_case[case.case_id]

        async def prediction(*_args, value=scripted):
            return value

        if args.system == "issue_only":
            result = await IssueOnlyRunner(prediction, runner_id="scripted-offline-v1").run(
                case, dataset_digest=complete_digest
            )
        else:
            if args.snapshots_root is None:
                raise ValueError("RepoScope scripted runs require --snapshots-root")
            result = await RepoScopeRunner(prediction, runner_id="scripted-offline-v1").run(
                case,
                snapshot_source=Path(args.snapshots_root) / case.case_id,
                dataset_digest=complete_digest,
            )
        results.append(result)
    write_jsonl(Path(args.output), tuple(results))
    print(f"wrote {len(results)} {args.system} result rows; no model was called")
    return 0


def _score(args: argparse.Namespace) -> int:
    complete_cases, complete_digest = _load_execution_cases(args)
    cases = _select_cases(complete_cases, args.split)
    gold = read_jsonl(Path(args.gold), BenchmarkGold)
    results = read_jsonl(Path(args.results), BenchmarkResult)
    if any(item.dataset_digest != complete_digest for item in results):
        raise ValueError("result dataset digest does not match the case file")
    if args.snapshots_root is None:
        raise ValueError("scoring requires --snapshots-root for every frozen case")
    indexes = {}
    cases_by_id = {item.case_id: item for item in cases}
    from app.ingestion import RepositoryCoordinates, RepositorySnapshot
    from app.investigation import PythonRepositoryIndex
    from .snapshot import snapshot_tree_digest

    root = Path(args.snapshots_root)
    for case_id, case in cases_by_id.items():
        snapshot_root = root / case_id
        if snapshot_tree_digest(snapshot_root) != case.snapshot_tree_digest:
            raise ValueError("snapshot digest does not match the frozen benchmark case")
        coordinates = RepositoryCoordinates.parse(case.repo_url)
        indexes[case_id] = PythonRepositoryIndex.build(
            RepositorySnapshot.create(
                owner=coordinates.owner,
                repository=coordinates.repository,
                commit_sha=case.pre_fix_commit_sha,
                root_path=snapshot_root,
                indexed_byte_count=sum(
                    path.stat().st_size for path in snapshot_root.rglob("*") if path.is_file()
                ),
            )
        )
    summary = score_benchmark(cases=cases, gold=gold, results=results, indexes=indexes)
    _write_json(Path(args.output), summary.model_dump(mode="json"))
    print(f"scored {len(cases)} cases from explicit gold; no model was called")
    return 0


def _select_cases(
    cases: tuple[BenchmarkCase, ...], split: str
) -> tuple[BenchmarkCase, ...]:
    if split == "all":
        return cases
    selected = tuple(case for case in cases if case.split == split)
    if not selected:
        raise ValueError(f"case catalog contains no {split} cases")
    return selected


def _load_execution_cases(
    args: argparse.Namespace,
) -> tuple[tuple[BenchmarkCase, ...], str]:
    cases_path = Path(args.cases)
    if args.allow_unlocked_fixture:
        cases = read_jsonl(cases_path, BenchmarkCase)
        return cases, dataset_digest(cases)
    cases = load_case_catalog(cases_path)
    if args.dataset_digest_file is None:
        raise ValueError("benchmark execution requires --dataset-digest-file")
    raw_digest = Path(args.dataset_digest_file).read_bytes()
    try:
        digest_text = raw_digest.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ValueError("dataset digest file must contain one SHA-256 line") from exc
    lines = digest_text.splitlines()
    if len(lines) != 1 or not raw_digest.endswith(b"\n"):
        raise ValueError("dataset digest file must contain one SHA-256 line")
    expected_digest = lines[0]
    if len(expected_digest) != 64 or any(
        character not in "0123456789abcdef" for character in expected_digest
    ):
        raise ValueError("dataset digest file must contain one SHA-256 line")
    actual_digest = dataset_digest(cases)
    if actual_digest != expected_digest:
        raise ValueError("case catalog does not match the expected locked digest")
    return cases, actual_digest


def _validate_dataset(args: argparse.Namespace) -> int:
    candidates = load_candidate_catalog(Path(args.candidates))
    cases = load_case_catalog(Path(args.cases))
    slots = load_locked_slot_catalog(Path(args.slots))
    validate_locked_dataset(candidates, cases, slots)
    development = tuple(case for case in cases if case.split == "development")
    development_gold = read_jsonl(Path(args.development_gold), BenchmarkGold)
    if {gold.case_id for gold in development_gold} != {
        case.case_id for case in development
    }:
        raise ValueError("development gold must match every development case exactly")
    hidden_gold: tuple[BenchmarkGold, ...] | None = None
    if args.hidden_gold is not None:
        hidden = tuple(case for case in cases if case.split == "hidden")
        hidden_gold = read_jsonl(Path(args.hidden_gold), BenchmarkGold)
        if {gold.case_id for gold in hidden_gold} != {
            case.case_id for case in hidden
        }:
            raise ValueError("hidden gold must match every hidden case exactly")
    if args.snapshots_root is not None:
        if hidden_gold is None:
            raise ValueError("snapshot validation requires explicit private hidden gold")
        from .snapshot import snapshot_tree_digest

        gold_by_case = {gold.case_id: gold for gold in (*development_gold, *hidden_gold)}
        root = Path(args.snapshots_root)
        for case in cases:
            snapshot = root / case.case_id
            if snapshot_tree_digest(snapshot) != case.snapshot_tree_digest:
                raise ValueError("snapshot digest does not match the frozen benchmark case")
            if any(
                not (snapshot / path).is_file()
                for path in gold_by_case[case.case_id].gold_files
            ):
                raise ValueError("gold file is absent from the pre-fix snapshot")
    digest = dataset_digest(cases)
    detail = (
        "including private hidden gold and snapshots"
        if args.snapshots_root
        else "public artifacts only"
    )
    print(
        f"validated locked 12-case dataset ({detail}); "
        f"dataset digest {digest}; no model was called"
    )
    return 0


def _write_json(path: Path, value: object) -> None:
    payload = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        + "\n"
    ).encode("utf-8")
    _write_bytes(path, payload)


def _write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
