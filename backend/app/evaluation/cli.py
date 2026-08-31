from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path

from .catalog import load_slot_catalog
from .contracts import BenchmarkCase, BenchmarkGold, BenchmarkResult, ScriptedPrediction
from .jsonl import read_jsonl, write_jsonl
from .metrics import score_benchmark
from .runners import IssueOnlyRunner, RepoScopeRunner


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="reposcope-eval")
    commands = parser.add_subparsers(dest="command", required=True)
    validate_slots = commands.add_parser("validate-slots")
    validate_slots.add_argument("catalog")
    run = commands.add_parser("run")
    run.add_argument("--system", choices=("issue_only", "reposcope"), required=True)
    run.add_argument("--cases", required=True)
    run.add_argument("--scripted-predictions", required=True)
    run.add_argument("--output", required=True)
    run.add_argument("--snapshots-root")
    score = commands.add_parser("score")
    score.add_argument("--cases", required=True)
    score.add_argument("--gold", required=True)
    score.add_argument("--results", required=True)
    score.add_argument("--snapshots-root")
    score.add_argument("--output", required=True)
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
    if args.command == "run":
        return asyncio.run(_run_scripted(args))
    if args.command == "score":
        return _score(args)
    raise AssertionError("unreachable")


async def _run_scripted(args: argparse.Namespace) -> int:
    cases_path = Path(args.cases)
    cases = read_jsonl(cases_path, BenchmarkCase)
    scripts = read_jsonl(Path(args.scripted_predictions), ScriptedPrediction)
    script_by_case = {item.case_id: item for item in scripts if item.system == args.system}
    if set(script_by_case) != {item.case_id for item in cases}:
        raise ValueError("scripts must contain exactly one row for every selected case")
    dataset_digest = hashlib.sha256(cases_path.read_bytes()).hexdigest()
    results = []
    for case in cases:
        scripted = script_by_case[case.case_id]

        async def prediction(*_args, value=scripted):
            return value

        if args.system == "issue_only":
            result = await IssueOnlyRunner(prediction, runner_id="scripted-offline-v1").run(
                case, dataset_digest=dataset_digest
            )
        else:
            if args.snapshots_root is None:
                raise ValueError("RepoScope scripted runs require --snapshots-root")
            result = await RepoScopeRunner(prediction, runner_id="scripted-offline-v1").run(
                case,
                snapshot_source=Path(args.snapshots_root) / case.case_id,
                dataset_digest=dataset_digest,
            )
        results.append(result)
    write_jsonl(Path(args.output), tuple(results))
    print(f"wrote {len(results)} {args.system} result rows; no model was called")
    return 0


def _score(args: argparse.Namespace) -> int:
    cases_path = Path(args.cases)
    cases = read_jsonl(cases_path, BenchmarkCase)
    gold = read_jsonl(Path(args.gold), BenchmarkGold)
    results = read_jsonl(Path(args.results), BenchmarkResult)
    dataset_digest = hashlib.sha256(cases_path.read_bytes()).hexdigest()
    if any(item.dataset_digest != dataset_digest for item in results):
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


def _write_json(path: Path, value: object) -> None:
    payload = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        + "\n"
    ).encode("utf-8")
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
