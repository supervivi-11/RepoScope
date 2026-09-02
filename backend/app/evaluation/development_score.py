from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path

from app.ingestion import RepositoryCoordinates, RepositorySnapshot
from app.investigation import PythonRepositoryIndex

from .contracts import BenchmarkGold, BenchmarkResult, EvaluationSummaryV2
from .jsonl import read_jsonl
from .metrics import score_benchmark
from .online_run import load_development_inputs
from .real_contracts import (
    DeepSeekRunConfig,
    ProviderCallUsage,
    RunArtifactManifest,
    configuration_digest,
)
from .real_validation import (
    aggregate_provider_usage,
    provider_backend_drift,
    validate_complete_prediction_evidence,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="reposcope-development-score")
    parser.add_argument("--cases", required=True)
    parser.add_argument("--dataset-digest-file", required=True)
    parser.add_argument("--snapshots-root", required=True)
    parser.add_argument("--prediction-directory", required=True)
    parser.add_argument("--development-gold", required=True)
    parser.add_argument("--output")
    return parser


def score_development(
    *,
    cases_path: Path,
    digest_path: Path,
    snapshots_root: Path,
    prediction_directory: Path,
    development_gold_path: Path,
    output_path: Path,
) -> EvaluationSummaryV2:
    prediction_root = prediction_directory.resolve(strict=True)
    if prediction_directory.is_symlink() or not prediction_root.is_dir():
        raise ValueError("prediction directory must be a real directory")
    manifest_path = prediction_root / "prediction-manifest.v1.json"
    manifest = RunArtifactManifest.model_validate_json(
        manifest_path.read_text(encoding="utf-8")
    )
    _verify_artifacts(prediction_root, manifest)
    config = DeepSeekRunConfig.model_validate_json(
        (prediction_root / "run-config.json").read_text(encoding="utf-8")
    )
    if config != DeepSeekRunConfig.approved():
        raise ValueError("prediction configuration is not the approved fixed run")
    if manifest.configuration_digest != configuration_digest(config):
        raise ValueError("manifest configuration digest does not match run config")
    cases, digest = load_development_inputs(
        cases_path=cases_path,
        digest_path=digest_path,
        snapshots_root=snapshots_root,
    )
    if manifest.dataset_digest != digest:
        raise ValueError("prediction manifest does not match the locked dataset")
    issue_only = read_jsonl(
        prediction_root / "issue-only.results.v1.jsonl", BenchmarkResult
    )
    reposcope = read_jsonl(
        prediction_root / "reposcope.results.v1.jsonl", BenchmarkResult
    )
    call_usage = read_jsonl(
        prediction_root / "call-usage.v1.jsonl", ProviderCallUsage
    )
    expected_order = tuple(
        f"{case.case_id}:{system}"
        for case in cases
        for system in ("issue_only", "reposcope")
    )
    if manifest.execution_order != expected_order:
        raise ValueError("manifest execution order does not match the locked cases")
    validate_complete_prediction_evidence(
        configuration=config,
        dataset_digest=digest,
        issue_only=issue_only,
        reposcope=reposcope,
        call_usage=call_usage,
        started_at=manifest.started_at,
        finished_at=manifest.finished_at,
    )
    if development_gold_path.name != "development-gold.v1.jsonl":
        raise ValueError("development gold filename must be development-gold.v1.jsonl")
    gold = read_jsonl(development_gold_path, BenchmarkGold)
    if {item.case_id for item in gold} != {item.case_id for item in cases}:
        raise ValueError("development gold must match the six development cases")
    indexes = _build_indexes(cases, snapshots_root)
    scored = score_benchmark(
        cases=cases,
        gold=gold,
        results=(*issue_only, *reposcope),
        indexes=indexes,
    )
    summary = scored.model_copy(
        update={
            "run_id": manifest.run_id,
            "configuration_digest": manifest.configuration_digest,
            "prediction_manifest_digest": hashlib.sha256(
                manifest_path.read_bytes()
            ).hexdigest(),
            "provider": config.provider,
            "requested_model": config.requested_model,
            "documented_model_version": config.documented_model_version,
            "provider_backend_drift": (
                provider_backend_drift(call_usage, config.requested_model)
            ),
            "preflight_usage": aggregate_provider_usage(
                tuple(item for item in call_usage if item.system == "preflight"),
                rate_card_version=config.rate_card_version,
            ),
            "total_provider_usage": aggregate_provider_usage(
                call_usage,
                rate_card_version=config.rate_card_version,
            ),
        }
    )
    _write_json(output_path, summary.model_dump(mode="json"))
    return summary


def _verify_artifacts(root: Path, manifest: RunArtifactManifest) -> None:
    required = {
        "run-config.json",
        "issue-only.results.v1.jsonl",
        "reposcope.results.v1.jsonl",
        "call-usage.v1.jsonl",
        "reproduce.txt",
    }
    by_path = {item.path: item.sha256 for item in manifest.artifacts}
    if set(by_path) != required:
        raise ValueError("prediction manifest has an unexpected artifact set")
    for relative, expected in by_path.items():
        path = root / relative
        try:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            raise ValueError("prediction artifact is unavailable") from exc
        if actual != expected:
            raise ValueError("prediction artifact digest does not match manifest")


def _verify_results(
    cases: tuple[object, ...],
    digest: str,
    config: DeepSeekRunConfig,
    issue_only: tuple[BenchmarkResult, ...],
    reposcope: tuple[BenchmarkResult, ...],
) -> None:
    expected = {item.case_id for item in cases}
    if len(issue_only) != 6 or len(reposcope) != 6:
        raise ValueError("predictions must contain six results per system")
    if {item.case_id for item in issue_only} != expected or {
        item.case_id for item in reposcope
    } != expected:
        raise ValueError("predictions must cover exactly the development cases")
    if any(item.system != "issue_only" for item in issue_only) or any(
        item.system != "reposcope" for item in reposcope
    ):
        raise ValueError("prediction result system does not match its file")
    if any(
        item.dataset_digest != digest
        or item.runner_id != config.runner_version
        or (item.status == "ok" and item.model_id is None)
        for item in (*issue_only, *reposcope)
    ):
        raise ValueError("prediction provenance does not match the approved run")


def _build_indexes(cases: tuple[object, ...], snapshots_root: Path):
    indexes: dict[str, PythonRepositoryIndex] = {}
    root = snapshots_root.resolve(strict=True)
    for case in cases:
        snapshot_root = root / case.case_id
        coordinates = RepositoryCoordinates.parse(case.repo_url)
        indexes[case.case_id] = PythonRepositoryIndex.build(
            RepositorySnapshot.create(
                owner=coordinates.owner,
                repository=coordinates.repository,
                commit_sha=case.pre_fix_commit_sha,
                root_path=snapshot_root,
                indexed_byte_count=sum(
                    path.stat().st_size
                    for path in snapshot_root.rglob("*")
                    if path.is_file()
                ),
            )
        )
    return indexes


def _write_json(path: Path, value: object) -> None:
    payload = (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    output = (
        Path(args.output)
        if args.output is not None
        else Path(args.prediction_directory) / "summary.v2.json"
    )
    summary = score_development(
        cases_path=Path(args.cases),
        digest_path=Path(args.dataset_digest_file),
        snapshots_root=Path(args.snapshots_root),
        prediction_directory=Path(args.prediction_directory),
        development_gold_path=Path(args.development_gold),
        output_path=output,
    )
    print(
        f"scored {summary.case_count} development cases from verified predictions; "
        "no model was called"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
