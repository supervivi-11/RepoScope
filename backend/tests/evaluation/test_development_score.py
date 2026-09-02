from __future__ import annotations

import json
import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.evaluation.catalog import dataset_digest
from app.evaluation.contracts import (
    BenchmarkCase,
    BenchmarkGold,
    BenchmarkResult,
    EvaluationUsage,
)
from app.evaluation.development_score import score_development
from app.evaluation.jsonl import read_jsonl, write_jsonl
from app.evaluation.online_run import write_prediction_artifacts
from app.evaluation.real_contracts import (
    DeepSeekRunConfig,
    DependencyVersion,
    ProviderCallUsage,
    REQUIRED_DEPENDENCIES,
)
from app.evaluation.snapshot import snapshot_tree_digest


def _dataset(tmp_path: Path) -> tuple[Path, Path, Path, tuple[BenchmarkCase, ...], str]:
    snapshots = tmp_path / "snapshots"
    cases: list[BenchmarkCase] = []
    for number in range(1, 7):
        case_id = f"development-case-{number}"
        snapshot = snapshots / case_id
        source = snapshot / "src" / f"module_{number}.py"
        source.parent.mkdir(parents=True)
        source.write_text(f"VALUE = {number}\n", encoding="utf-8", newline="")
        cases.append(
            BenchmarkCase(
                schema_version="reposcope.eval.case.v1",
                case_id=case_id,
                split="development",
                repo_url=f"https://github.com/example/development-{number}",
                issue_number=number,
                issue_title=f"Development issue {number}",
                pre_fix_commit_sha=f"{number:x}" * 40,
                snapshot_tree_digest=snapshot_tree_digest(snapshot),
            )
        )
    for number in range(1, 7):
        cases.append(
            BenchmarkCase(
                schema_version="reposcope.eval.case.v1",
                case_id=f"hidden-case-{number}",
                split="hidden",
                repo_url=f"https://github.com/example/hidden-{number}",
                issue_number=number,
                issue_title=f"Hidden issue {number}",
                pre_fix_commit_sha=f"{number + 6:x}" * 40,
                snapshot_tree_digest=f"{number + 6:x}" * 64,
            )
        )
    ordered = tuple(cases)
    cases_path = tmp_path / "benchmark-cases.v1.jsonl"
    write_jsonl(cases_path, ordered)
    digest = dataset_digest(tuple(sorted(ordered, key=lambda item: item.case_id)))
    digest_path = tmp_path / "benchmark-cases.v1.sha256"
    digest_path.write_text(f"{digest}\n", encoding="ascii")
    gold_path = tmp_path / "development-gold.v1.jsonl"
    write_jsonl(
        gold_path,
        tuple(
            BenchmarkGold(
                schema_version="reposcope.eval.gold.v1",
                case_id=f"development-case-{number}",
                gold_files=(f"src/module_{number}.py",),
            )
            for number in range(1, 7)
        ),
    )
    return cases_path, digest_path, snapshots, ordered, digest


def _result(case_id: str, system: str, digest: str, number: int) -> BenchmarkResult:
    return BenchmarkResult(
        schema_version="reposcope.eval.result.v1",
        case_id=case_id,
        system=system,
        status="ok",
        predicted_files=(f"src/module_{number}.py",),
        citations=(),
        report_outcome="insufficient_evidence",
        runner_id="deepseek-development-v1",
        model_id="deepseek-v4-flash",
        dataset_digest=digest,
        usage=EvaluationUsage(
            latency_ms=number * 10,
            input_tokens=100,
            output_tokens=20,
            estimated_cost_usd="0.0000352",
            tool_calls=0,
            model_attempts=1,
            rate_card_version="deepseek-v4-2026-08-16-v1",
        ),
    )


def _predictions(tmp_path: Path, digest: str) -> Path:
    output = tmp_path / "predictions"
    issue = tuple(
        _result(f"development-case-{number}", "issue_only", digest, number)
        for number in range(1, 7)
    )
    repo = tuple(
        _result(f"development-case-{number}", "reposcope", digest, number)
        for number in range(1, 7)
    )
    started = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
    calls: list[ProviderCallUsage] = []
    phases = (
        "issue_understanding",
        "tool_selection",
        "evidence_critique",
        "report_composition",
        "issue_only_prediction",
    )

    def add_call(case_id: str, system: str, phase: str, input_tokens: int, output_tokens: int, cost: str) -> None:
        at = started + timedelta(seconds=len(calls) + 1)
        calls.append(
            ProviderCallUsage(
                schema_version="reposcope.eval.call-usage.v1",
                call_id=f"call-{len(calls) + 1:04d}",
                case_id=case_id,
                system=system,
                phase=phase,
                attempt=1,
                requested_model="deepseek-v4-flash",
                returned_model="deepseek-v4-flash",
                started_at=at,
                finished_at=at + timedelta(milliseconds=1),
                latency_ms=1,
                input_tokens=input_tokens,
                cached_input_tokens=0,
                output_tokens=output_tokens,
                rate_period="off_peak",
                cache_hit_usd_per_million=Decimal("0.007"),
                cache_miss_usd_per_million=Decimal("0.22"),
                output_usd_per_million=Decimal("0.66"),
                estimated_cost_usd=Decimal(cost),
            )
        )

    for phase in phases:
        add_call("schema-preflight", "preflight", phase, 1, 1, "0.00000088")
    for number in range(1, 7):
        case_id = f"development-case-{number}"
        add_call(case_id, "issue_only", "issue_only_prediction", 100, 20, "0.0000352")
        add_call(case_id, "reposcope", "report_composition", 100, 20, "0.0000352")
    finished = started + timedelta(minutes=1)
    write_prediction_artifacts(
        output_directory=output,
        run_id="20260902T010203Z-deepseek-v4-flash",
        configuration=DeepSeekRunConfig.approved(),
        dataset_digest=digest,
        issue_only_results=issue,
        reposcope_results=repo,
        call_usage=tuple(calls),
        reproduction_commands=("predict", "score"),
        reposcope_commit="a" * 40,
        dependency_versions=tuple(
            DependencyVersion(name=name, version="test")
            for name in REQUIRED_DEPENDENCIES
        ),
        execution_order=tuple(
            f"development-case-{number}:{system}"
            for number in range(1, 7)
            for system in ("issue_only", "reposcope")
        ),
        started_at=started,
        finished_at=finished,
    )
    return output


def _rehash_artifact(predictions: Path, name: str) -> None:
    manifest_path = predictions / "prediction-manifest.v1.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    digest = hashlib.sha256((predictions / name).read_bytes()).hexdigest()
    for artifact in manifest["artifacts"]:
        if artifact["path"] == name:
            artifact["sha256"] = digest
            break
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
        newline="",
    )


def test_development_score_verifies_manifest_before_opening_gold(tmp_path: Path) -> None:
    cases, digest_file, snapshots, _, digest = _dataset(tmp_path)
    predictions = _predictions(tmp_path, digest)
    with (predictions / "issue-only.results.v1.jsonl").open("ab") as stream:
        stream.write(b"tampered\n")

    with pytest.raises(ValueError, match="artifact digest"):
        score_development(
            cases_path=cases,
            digest_path=digest_file,
            snapshots_root=snapshots,
            prediction_directory=predictions,
            development_gold_path=tmp_path / "missing-development-gold.v1.jsonl",
            output_path=tmp_path / "summary.v2.json",
        )


def test_development_score_rejects_hidden_gold_path(tmp_path: Path) -> None:
    cases, digest_file, snapshots, _, digest = _dataset(tmp_path)
    predictions = _predictions(tmp_path, digest)

    with pytest.raises(ValueError, match="development gold filename"):
        score_development(
            cases_path=cases,
            digest_path=digest_file,
            snapshots_root=snapshots,
            prediction_directory=predictions,
            development_gold_path=tmp_path / "hidden-gold.v1.jsonl",
            output_path=tmp_path / "summary.v2.json",
        )


def test_development_score_emits_real_summary_v2_with_provenance(tmp_path: Path) -> None:
    cases, digest_file, snapshots, _, digest = _dataset(tmp_path)
    predictions = _predictions(tmp_path, digest)
    gold = tmp_path / "development-gold.v1.jsonl"
    output = tmp_path / "summary.v2.json"

    summary = score_development(
        cases_path=cases,
        digest_path=digest_file,
        snapshots_root=snapshots,
        prediction_directory=predictions,
        development_gold_path=gold,
        output_path=output,
    )

    assert summary.schema_version == "reposcope.eval.summary.v2"
    assert summary.split == "development"
    assert summary.case_count == 6
    assert summary.dataset_digest == digest
    assert summary.run_id == "20260902T010203Z-deepseek-v4-flash"
    assert summary.provider == "deepseek_official"
    assert summary.requested_model == "deepseek-v4-flash"
    assert summary.configuration_digest is not None
    assert summary.prediction_manifest_digest is not None
    assert summary.provider_backend_drift is False
    assert summary.preflight_usage is not None
    assert summary.preflight_usage.input_tokens == 5
    assert summary.preflight_usage.estimated_cost_usd == Decimal("0.00000440")
    assert summary.total_provider_usage is not None
    assert summary.total_provider_usage.input_tokens == 1205
    assert summary.total_provider_usage.output_tokens == 245
    assert summary.total_provider_usage.estimated_cost_usd == Decimal("0.00042680")
    assert {item.file_recall_at_5 for item in summary.systems} == {1.0}
    assert {item.mrr for item in summary.systems} == {1.0}
    stored = json.loads(output.read_text(encoding="utf-8"))
    assert stored["run_id"] == summary.run_id
    assert "hidden" not in stored


@pytest.mark.parametrize("tamper", ("empty_ledger", "failed_result", "usage_mismatch"))
def test_development_score_rejects_unverifiable_predictions_before_gold(
    tmp_path: Path, tamper: str
) -> None:
    cases, digest_file, snapshots, _, digest = _dataset(tmp_path)
    predictions = _predictions(tmp_path, digest)
    if tamper == "empty_ledger":
        (predictions / "call-usage.v1.jsonl").write_bytes(b"")
        _rehash_artifact(predictions, "call-usage.v1.jsonl")
        match = "non-empty provider ledger"
    else:
        path = predictions / "issue-only.results.v1.jsonl"
        rows = list(read_jsonl(path, BenchmarkResult))
        if tamper == "failed_result":
            rows[0] = BenchmarkResult(
                schema_version="reposcope.eval.result.v1",
                case_id=rows[0].case_id,
                system="issue_only",
                status="failed",
                runner_id="deepseek-development-v1",
                dataset_digest=digest,
                safe_error_code="runner_failure",
            )
            match = "successful predictions"
        else:
            rows[0] = rows[0].model_copy(
                update={
                    "usage": rows[0].usage.model_copy(update={"input_tokens": 101})
                }
            )
            match = "usage does not match"
        write_jsonl(path, tuple(rows))
        _rehash_artifact(predictions, "issue-only.results.v1.jsonl")

    with pytest.raises(ValueError, match=match):
        score_development(
            cases_path=cases,
            digest_path=digest_file,
            snapshots_root=snapshots,
            prediction_directory=predictions,
            development_gold_path=tmp_path / "missing" / "development-gold.v1.jsonl",
            output_path=tmp_path / "summary.v2.json",
        )
