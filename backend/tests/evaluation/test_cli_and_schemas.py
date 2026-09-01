from __future__ import annotations

from pathlib import Path

import json
import hashlib

import pytest

from app.evaluation.contracts import (
    BenchmarkCase,
    BenchmarkGold,
    BenchmarkResult,
    CurationCandidate,
    EvaluationUsage,
    ScriptedPrediction,
)
from app.evaluation.cli import main
from app.evaluation.jsonl import read_jsonl, write_jsonl
from app.evaluation.contracts import BenchmarkResult
from app.evaluation.schemas import export_schemas
from app.evaluation.snapshot import snapshot_tree_digest


def test_checked_in_evaluation_schemas_match_pydantic_contracts(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[3]
    assert export_schemas(root / "evals" / "schemas", check=True) == ()


def test_schema_export_includes_the_pre_split_candidate_contract(tmp_path: Path) -> None:
    export_schemas(tmp_path)

    schema = json.loads((tmp_path / "candidate.v1.schema.json").read_text(encoding="utf-8"))

    assert schema["properties"]["schema_version"]["const"] == "reposcope.eval.candidate.v1"
    assert "split" not in schema["properties"]


def test_schema_export_includes_locked_slot_v2_without_changing_slot_v1(
    tmp_path: Path,
) -> None:
    export_schemas(tmp_path)

    legacy = json.loads((tmp_path / "slot.v1.schema.json").read_text(encoding="utf-8"))
    locked = json.loads((tmp_path / "slot.v2.schema.json").read_text(encoding="utf-8"))

    assert legacy["properties"]["state"]["const"] == "unfilled"
    assert locked["properties"]["state"]["const"] == "locked"
    assert locked["properties"]["schema_version"]["const"] == "reposcope.eval.slot.v2"
    assert {"case_id", "split_key_digest"}.issubset(locked["properties"])


def test_summary_v2_adds_provenance_without_changing_summary_v1(
    tmp_path: Path,
) -> None:
    export_schemas(tmp_path)

    legacy = json.loads((tmp_path / "summary.v1.schema.json").read_text(encoding="utf-8"))
    provenance = json.loads(
        (tmp_path / "summary.v2.schema.json").read_text(encoding="utf-8")
    )

    assert "split" not in legacy["properties"]
    assert provenance["properties"]["schema_version"]["const"] == (
        "reposcope.eval.summary.v2"
    )
    assert {"split", "dataset_digest"}.issubset(provenance["required"])


def test_cli_validates_catalog_without_running_models_or_claiming_results(
    capsys,
) -> None:
    root = Path(__file__).resolve().parents[3]
    exit_code = main(
        ["validate-slots", str(root / "evals" / "benchmark-slots.v1.jsonl")]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert captured.out == "validated 12 metadata-only slots (6 development, 6 hidden)\n"
    assert "score" not in captured.out.casefold()


def test_cli_validates_pre_split_candidates_without_running_a_model(
    tmp_path: Path,
    capsys,
) -> None:
    path = tmp_path / "candidates.jsonl"
    write_jsonl(
        path,
        (
            CurationCandidate(
                schema_version="reposcope.eval.candidate.v1",
                candidate_id="python-hyper-h11-issue-92",
                state="qualified_pending_dataset_lock",
                repo_url="https://github.com/python-hyper/h11",
                issue_number=92,
                issue_title="Frozen issue",
                issue_body=None,
                pre_fix_commit_sha="a" * 40,
                snapshot_tree_digest="b" * 64,
            ),
        ),
    )

    exit_code = main(["validate-candidates", str(path)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert captured.out == (
        "validated 1 qualified pre-split candidate; "
        "no split was assigned and no model was called\n"
    )


def test_empty_result_template_is_really_empty() -> None:
    root = Path(__file__).resolve().parents[3]
    assert (root / "evals" / "results-empty.v1.jsonl").read_bytes() == b""


def test_cli_runs_issue_only_from_frozen_script_without_external_calls(
    tmp_path: Path,
) -> None:
    cases_path = tmp_path / "cases.jsonl"
    scripts_path = tmp_path / "scripts.jsonl"
    output_path = tmp_path / "results.jsonl"
    case = BenchmarkCase(
        schema_version="reposcope.eval.case.v1",
        case_id="dev-01",
        split="development",
        repo_url="https://github.com/example/project",
        issue_number=1,
        issue_title="Frozen issue",
        issue_body=None,
        pre_fix_commit_sha="a" * 40,
        snapshot_tree_digest="b" * 64,
    )
    write_jsonl(cases_path, (case,))
    write_jsonl(
        scripts_path,
        (
            ScriptedPrediction(
                schema_version="reposcope.eval.script.v1",
                case_id="dev-01",
                system="issue_only",
                predicted_files=("src/parser.py",),
                report_outcome="insufficient_evidence",
            ),
        ),
    )

    exit_code = main(
        [
            "run",
            "--system",
            "issue_only",
            "--allow-unlocked-fixture",
            "--cases",
            str(cases_path),
            "--scripted-predictions",
            str(scripts_path),
            "--output",
            str(output_path),
        ]
    )

    results = read_jsonl(output_path, BenchmarkResult)
    assert exit_code == 0
    assert len(results) == 1
    assert results[0].system == "issue_only"
    assert results[0].predicted_files == ("src/parser.py",)
    assert results[0].usage.estimated_cost_usd is None


def test_cli_scores_only_when_gold_is_supplied_to_separate_score_command(
    tmp_path: Path,
) -> None:
    cases_path = tmp_path / "cases.jsonl"
    gold_path = tmp_path / "gold.jsonl"
    results_path = tmp_path / "results.jsonl"
    summary_path = tmp_path / "summary.json"
    snapshots_root = tmp_path / "snapshots"
    snapshot = snapshots_root / "dev-01"
    (snapshot / "src").mkdir(parents=True)
    (snapshot / "src" / "a.py").write_bytes(b"value = 1\n")
    case = BenchmarkCase(
        schema_version="reposcope.eval.case.v1",
        case_id="dev-01",
        split="development",
        repo_url="https://github.com/example/project",
        issue_number=1,
        issue_title="Frozen issue",
        issue_body=None,
        pre_fix_commit_sha="a" * 40,
        snapshot_tree_digest=snapshot_tree_digest(snapshot),
    )
    write_jsonl(cases_path, (case,))
    write_jsonl(
        gold_path,
        (
            BenchmarkGold(
                schema_version="reposcope.eval.gold.v1",
                case_id="dev-01",
                gold_files=("src/a.py",),
            ),
        ),
    )
    write_jsonl(
        results_path,
        (
            BenchmarkResult(
                schema_version="reposcope.eval.result.v1",
                case_id="dev-01",
                system="issue_only",
                status="ok",
                predicted_files=("src/a.py",),
                report_outcome="insufficient_evidence",
                runner_id="scripted-offline-v1",
                dataset_digest=hashlib.sha256(cases_path.read_bytes()).hexdigest(),
                usage=EvaluationUsage(),
            ),
        ),
    )

    assert main(
        [
            "score",
            "--allow-unlocked-fixture",
            "--cases",
            str(cases_path),
            "--gold",
            str(gold_path),
            "--results",
            str(results_path),
            "--snapshots-root",
            str(snapshots_root),
            "--output",
            str(summary_path),
        ]
    ) == 0

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["schema_version"] == "reposcope.eval.summary.v2"
    assert summary["split"] == "development"
    assert summary["dataset_digest"] == hashlib.sha256(cases_path.read_bytes()).hexdigest()
    assert summary["systems"][0]["file_recall_at_5"] == 1.0
    assert summary["systems"][0]["citation_validity"] is None


def test_split_score_uses_selected_gold_but_keeps_complete_dataset_digest(
    tmp_path: Path,
) -> None:
    cases_path = tmp_path / "cases.jsonl"
    gold_path = tmp_path / "development-gold.jsonl"
    results_path = tmp_path / "development-results.jsonl"
    summary_path = tmp_path / "summary.json"
    snapshots_root = tmp_path / "snapshots"
    development_snapshot = snapshots_root / "development-case"
    development_snapshot.mkdir(parents=True)
    (development_snapshot / "module.py").write_bytes(b"value = 1\n")
    cases = (
        BenchmarkCase(
            schema_version="reposcope.eval.case.v1",
            case_id="development-case",
            split="development",
            repo_url="https://github.com/example/development",
            issue_number=1,
            issue_title="Development issue",
            pre_fix_commit_sha="a" * 40,
            snapshot_tree_digest=snapshot_tree_digest(development_snapshot),
        ),
        BenchmarkCase(
            schema_version="reposcope.eval.case.v1",
            case_id="hidden-case",
            split="hidden",
            repo_url="https://github.com/example/hidden",
            issue_number=2,
            issue_title="Hidden issue",
            pre_fix_commit_sha="b" * 40,
            snapshot_tree_digest="c" * 64,
        ),
    )
    write_jsonl(cases_path, cases)
    write_jsonl(
        gold_path,
        (
            BenchmarkGold(
                schema_version="reposcope.eval.gold.v1",
                case_id="development-case",
                gold_files=("module.py",),
            ),
        ),
    )
    write_jsonl(
        results_path,
        (
            BenchmarkResult(
                schema_version="reposcope.eval.result.v1",
                case_id="development-case",
                system="issue_only",
                status="ok",
                predicted_files=("module.py",),
                runner_id="scripted-offline-v1",
                dataset_digest=hashlib.sha256(cases_path.read_bytes()).hexdigest(),
            ),
        ),
    )

    assert main(
        [
            "score",
            "--allow-unlocked-fixture",
            "--split",
            "development",
            "--cases",
            str(cases_path),
            "--gold",
            str(gold_path),
            "--results",
            str(results_path),
            "--snapshots-root",
            str(snapshots_root),
            "--output",
            str(summary_path),
        ]
    ) == 0

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["case_count"] == 1
    assert summary["systems"][0]["file_recall_at_5"] == 1.0


def test_benchmark_run_rejects_a_reduced_catalog_instead_of_self_certifying_it(
    tmp_path: Path,
) -> None:
    cases_path = tmp_path / "cases.jsonl"
    digest_path = tmp_path / "cases.sha256"
    scripts_path = tmp_path / "scripts.jsonl"
    output_path = tmp_path / "results.jsonl"
    case = BenchmarkCase(
        schema_version="reposcope.eval.case.v1",
        case_id="only-case",
        split="development",
        repo_url="https://github.com/example/project",
        issue_number=1,
        issue_title="Reduced catalog",
        pre_fix_commit_sha="a" * 40,
        snapshot_tree_digest="b" * 64,
    )
    write_jsonl(cases_path, (case,))
    digest_path.write_text(
        hashlib.sha256(cases_path.read_bytes()).hexdigest() + "\n", encoding="ascii"
    )
    scripts_path.write_bytes(b"")

    with pytest.raises(ValueError, match="twelve"):
        main(
            [
                "run",
                "--system",
                "issue_only",
                "--cases",
                str(cases_path),
                "--dataset-digest-file",
                str(digest_path),
                "--scripted-predictions",
                str(scripts_path),
                "--output",
                str(output_path),
            ]
        )


def test_benchmark_run_rejects_an_unexpected_locked_dataset_digest(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[3]
    scripts_path = tmp_path / "scripts.jsonl"
    digest_path = tmp_path / "wrong.sha256"
    scripts_path.write_bytes(b"")
    digest_path.write_text("0" * 64 + "\n", encoding="ascii")

    with pytest.raises(ValueError, match="expected locked digest"):
        main(
            [
                "run",
                "--system",
                "issue_only",
                "--cases",
                str(root / "evals" / "benchmark-cases.v1.jsonl"),
                "--dataset-digest-file",
                str(digest_path),
                "--scripted-predictions",
                str(scripts_path),
                "--output",
                str(tmp_path / "results.jsonl"),
            ]
        )
