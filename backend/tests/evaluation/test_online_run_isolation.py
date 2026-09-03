from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from argparse import Namespace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.agent import (
    AnalysisReport,
    CritiqueResult,
    IssueUnderstanding,
    ModelPhase,
    ToolRequest,
)
from app.evaluation.contracts import BenchmarkCase, BenchmarkResult, EvaluationUsage
from app.evaluation.jsonl import read_jsonl
from app.evaluation.online_run import (
    _parser,
    _run_online,
    _source_provenance,
    install_gold_read_guard,
    load_development_inputs,
    preflight_structured_models,
    run_paired_predictions,
    write_prediction_artifacts,
)
from app.evaluation.predictors import IssueOnlyModelOutput
from app.evaluation.real_contracts import (
    DeepSeekRunConfig,
    DependencyVersion,
    ProviderCallUsage,
    REQUIRED_DEPENDENCIES,
    RunArtifactManifest,
)
from app.evaluation.real_validation import validate_complete_prediction_evidence


DATASET_DIGEST = "de76c423ffe936743f979f35cc634caed064e571b3f5c9563b219ac5025ceea7"


def _root() -> Path:
    return Path(__file__).resolve().parents[3]


def _result(case_id: str, system: str) -> BenchmarkResult:
    return BenchmarkResult(
        schema_version="reposcope.eval.result.v1",
        case_id=case_id,
        system=system,
        status="ok",
        predicted_files=("src/example.py",),
        citations=(),
        report_outcome="insufficient_evidence",
        runner_id="deepseek-development-v1",
        model_id="deepseek-v4-flash",
        dataset_digest=DATASET_DIGEST,
        usage=EvaluationUsage(
            latency_ms=10,
            input_tokens=10,
            output_tokens=2,
            estimated_cost_usd=Decimal("0.00000352"),
            tool_calls=0,
            model_attempts=1,
            rate_card_version="deepseek-v4-2026-08-16-v1",
        ),
    )


def _six_synthetic_cases() -> tuple[BenchmarkCase, ...]:
    return tuple(
        BenchmarkCase(
            schema_version="reposcope.eval.case.v1",
            case_id=f"development-case-{number}",
            split="development",
            repo_url="https://github.com/example/project",
            issue_number=number,
            issue_title=f"Synthetic issue {number}",
            pre_fix_commit_sha="a" * 40,
            snapshot_tree_digest="b" * 64,
        )
        for number in range(1, 7)
    )


def _ledger(case_ids: tuple[str, ...]) -> tuple[ProviderCallUsage, ...]:
    started = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
    rows: list[ProviderCallUsage] = []

    def add(case_id: str, system: str, phase: str, input_tokens: int, output_tokens: int, cost: str) -> None:
        at = started + timedelta(seconds=len(rows) + 1)
        rows.append(
            ProviderCallUsage(
                schema_version="reposcope.eval.call-usage.v1",
                call_id=f"call-{len(rows) + 1:04d}",
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

    for phase in (
        "issue_understanding",
        "tool_selection",
        "evidence_critique",
        "report_composition",
        "issue_only_prediction",
    ):
        add("schema-preflight", "preflight", phase, 1, 1, "0.00000088")
    for case_id in case_ids:
        add(case_id, "issue_only", "issue_only_prediction", 10, 2, "0.00000352")
        add(case_id, "reposcope", "report_composition", 10, 2, "0.00000352")
    return tuple(rows)


def _with_schema_retry(
    rows: tuple[ProviderCallUsage, ...], *, index: int
) -> tuple[ProviderCallUsage, ...]:
    original = rows[index]
    failed = original.model_copy(
        update={
            "status": "failed",
            "safe_error_code": "schema_error",
            "attempt": 1,
        }
    )
    retried = original.model_copy(update={"attempt": 2})
    expanded = (*rows[:index], failed, retried, *rows[index + 1 :])
    return tuple(
        item.model_copy(update={"call_id": f"call-{number:04d}"})
        for number, item in enumerate(expanded, start=1)
    )


def test_online_module_has_no_gold_or_scoring_imports_and_no_gold_argument() -> None:
    import app.evaluation.online_run as module

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert "BenchmarkGold" not in imported
    assert not any(name.endswith("metrics") for name in imported)

    parser = _parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--gold", "development-gold.v1.jsonl"])
    with pytest.raises(SystemExit):
        parser.parse_args(["--split", "hidden"])
    with pytest.raises(SystemExit):
        parser.parse_args(["--split", "all"])


def test_online_cli_reports_safe_error_without_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    import app.evaluation.online_run as module

    async def fail_safely(args):
        from app.evaluation.deepseek_provider import DeepSeekCredentials
        from pydantic import ValidationError

        try:
            DeepSeekCredentials(_env_file=None)
        except ValidationError as exc:
            raise exc
        raise AssertionError("missing credentials should fail")

    monkeypatch.delenv("REPOSCOPE_DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(module, "install_gold_read_guard", lambda *args: None)
    monkeypatch.setattr(module, "_run_online", fail_safely)

    assert module.main(
        [
            "--cases",
            "cases.jsonl",
            "--dataset-digest-file",
            "cases.sha256",
            "--snapshots-root",
            "snapshots",
            "--output-directory",
            "output",
        ]
    ) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "evaluation failed safely: credentials_not_configured\n"
    assert "Traceback" not in captured.err


def test_online_cli_classifies_exhausted_schema_retry_as_run_abort(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    import app.evaluation.online_run as module
    from app.evaluation.deepseek_provider import SchemaUnverifiable

    async def fail_safely(args):
        raise SchemaUnverifiable("schema retries exhausted")

    monkeypatch.setattr(module, "install_gold_read_guard", lambda *args: None)
    monkeypatch.setattr(module, "_run_online", fail_safely)

    assert module.main(
        [
            "--cases",
            "cases.jsonl",
            "--dataset-digest-file",
            "cases.sha256",
            "--snapshots-root",
            "snapshots",
            "--output-directory",
            "output",
        ]
    ) == 2
    assert capsys.readouterr().err == "evaluation failed safely: run_safety_abort\n"


def test_gold_read_guard_fails_closed_in_online_process(tmp_path: Path) -> None:
    gold = tmp_path / "development-gold.v1.jsonl"
    gold.write_text("must not be read", encoding="utf-8")
    script = (
        "from pathlib import Path; "
        "from app.evaluation.online_run import install_gold_read_guard; "
        "install_gold_read_guard(); "
        f"Path({str(gold)!r}).read_text(encoding='utf-8')"
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(_root() / "backend")

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=_root(),
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert completed.returncode != 0
    assert "PermissionError" in completed.stderr
    assert "must not be read" not in completed.stderr


def test_gold_read_guard_blocks_hardlink_alias_in_online_process(tmp_path: Path) -> None:
    gold = tmp_path / "development-gold.v1.jsonl"
    alias = tmp_path / "neutral-input.jsonl"
    gold.write_text("GOLD_ALIAS_CANARY", encoding="utf-8")
    os.link(gold, alias)
    script = (
        "from pathlib import Path; "
        "from app.evaluation.online_run import install_gold_read_guard; "
        f"gold=Path({str(gold)!r}); alias=Path({str(alias)!r}); "
        "install_gold_read_guard((gold,)); alias.read_text(encoding='utf-8')"
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(_root() / "backend")

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=_root(),
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert completed.returncode != 0
    assert "PermissionError" in completed.stderr
    assert "GOLD_ALIAS_CANARY" not in completed.stderr


def test_locked_development_inputs_validate_all_six_snapshots_without_gold() -> None:
    root = _root()
    snapshots_root = root / "local" / "evaluation" / "snapshots"
    if not snapshots_root.is_dir():
        pytest.skip("local evaluation snapshots are intentionally not committed")

    cases, digest = load_development_inputs(
        cases_path=root / "evals" / "benchmark-cases.v1.jsonl",
        digest_path=root / "evals" / "benchmark-cases.v1.sha256",
        snapshots_root=snapshots_root,
    )

    assert len(cases) == 6
    assert {case.split for case in cases} == {"development"}
    assert digest == DATASET_DIGEST
    assert all((snapshots_root / case.case_id).is_dir() for case in cases)


def test_dataset_digest_mismatch_fails_before_snapshot_access(tmp_path: Path) -> None:
    root = _root()
    digest = tmp_path / "cases.sha256"
    digest.write_text(f"{'0' * 64}\n", encoding="ascii")

    with pytest.raises(ValueError, match="locked digest"):
        load_development_inputs(
            cases_path=root / "evals" / "benchmark-cases.v1.jsonl",
            digest_path=digest,
            snapshots_root=tmp_path / "missing-snapshots",
        )


def test_source_provenance_rejects_dirty_tree_before_dependency_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake_run(arguments, **kwargs):
        calls.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, stdout=" M backend/app.py\n")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(ValueError, match="clean source tree"):
        _source_provenance(tmp_path)
    assert calls == [["git", "status", "--porcelain", "--untracked-files=all"]]


@pytest.mark.anyio
async def test_online_run_validates_dataset_before_loading_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _root()
    bad_digest = tmp_path / "cases.sha256"
    bad_digest.write_text(f"{'0' * 64}\n", encoding="ascii")
    monkeypatch.delenv("REPOSCOPE_DEEPSEEK_API_KEY", raising=False)

    with pytest.raises(ValueError, match="locked digest"):
        await _run_online(
            Namespace(
                split="development",
                cases=str(root / "evals" / "benchmark-cases.v1.jsonl"),
                dataset_digest_file=str(bad_digest),
                snapshots_root=str(tmp_path / "missing-snapshots"),
                output_directory=str(tmp_path / "output"),
            )
        )


def test_prediction_artifacts_are_atomic_redacted_and_manifested(tmp_path: Path) -> None:
    case_ids = tuple(f"development-case-{number}" for number in range(1, 7))
    issue_results = tuple(_result(case_id, "issue_only") for case_id in case_ids)
    reposcope_results = tuple(_result(case_id, "reposcope") for case_id in case_ids)

    manifest = write_prediction_artifacts(
        output_directory=tmp_path,
        run_id="20260902T010203Z-deepseek-v4-flash",
        configuration=DeepSeekRunConfig.approved(),
        dataset_digest=DATASET_DIGEST,
        issue_only_results=issue_results,
        reposcope_results=reposcope_results,
        call_usage=_ledger(case_ids),
        reproduction_commands=(
            "python -m app.evaluation.online_run --split development ...",
            "python -m app.evaluation.development_score ...",
        ),
        reposcope_commit="a" * 40,
        dependency_versions=tuple(
            DependencyVersion(name=name, version="test")
            for name in REQUIRED_DEPENDENCIES
        ),
        execution_order=tuple(
            f"{case_id}:{system}"
            for case_id in case_ids
            for system in ("issue_only", "reposcope")
        ),
        started_at=datetime(2026, 9, 2, 12, 0, tzinfo=UTC),
        finished_at=datetime(2026, 9, 2, 12, 1, tzinfo=UTC),
    )

    assert read_jsonl(
        tmp_path / "issue-only.results.v1.jsonl", BenchmarkResult
    ) == issue_results
    assert read_jsonl(
        tmp_path / "reposcope.results.v1.jsonl", BenchmarkResult
    ) == reposcope_results
    redacted = (tmp_path / "run-config.json").read_text(encoding="utf-8").casefold()
    assert "api_key" not in redacted
    assert "secret" not in redacted
    assert len(read_jsonl(tmp_path / "call-usage.v1.jsonl", ProviderCallUsage)) == 17
    stored = RunArtifactManifest.model_validate_json(
        (tmp_path / "prediction-manifest.v1.json").read_text(encoding="utf-8")
    )
    assert stored == manifest
    assert {item.path for item in stored.artifacts} == {
        "run-config.json",
        "issue-only.results.v1.jsonl",
        "reposcope.results.v1.jsonl",
        "call-usage.v1.jsonl",
        "reproduce.txt",
    }
    for item in stored.artifacts:
        import hashlib

        assert hashlib.sha256((tmp_path / item.path).read_bytes()).hexdigest() == item.sha256


def test_complete_manifest_rejects_empty_ledger_and_failed_results(tmp_path: Path) -> None:
    case_ids = tuple(f"development-case-{number}" for number in range(1, 7))
    issue_results = tuple(_result(case_id, "issue_only") for case_id in case_ids)
    reposcope_results = tuple(_result(case_id, "reposcope") for case_id in case_ids)
    common = {
        "run_id": "20260902T010203Z-deepseek-v4-flash",
        "configuration": DeepSeekRunConfig.approved(),
        "dataset_digest": DATASET_DIGEST,
        "reposcope_results": reposcope_results,
        "reproduction_commands": ("predict", "score"),
        "reposcope_commit": "a" * 40,
        "dependency_versions": tuple(
            DependencyVersion(name=name, version="test")
            for name in REQUIRED_DEPENDENCIES
        ),
        "execution_order": tuple(
            f"{case_id}:{system}"
            for case_id in case_ids
            for system in ("issue_only", "reposcope")
        ),
        "started_at": datetime(2026, 9, 2, 12, 0, tzinfo=UTC),
        "finished_at": datetime(2026, 9, 2, 12, 1, tzinfo=UTC),
    }

    with pytest.raises(ValueError, match="non-empty provider ledger"):
        write_prediction_artifacts(
            output_directory=tmp_path / "empty-ledger",
            issue_only_results=issue_results,
            call_usage=(),
            **common,
        )
    failed = issue_results[0].model_copy(
        update={"status": "failed", "safe_error_code": "runner_failure"}
    )
    with pytest.raises(ValueError, match="successful predictions"):
        write_prediction_artifacts(
            output_directory=tmp_path / "failed-result",
            issue_only_results=(failed, *issue_results[1:]),
            call_usage=_ledger(case_ids),
            **common,
        )


def test_complete_manifest_accepts_bounded_successful_schema_retry(
    tmp_path: Path,
) -> None:
    case_ids = tuple(f"development-case-{number}" for number in range(1, 7))
    ledger = _with_schema_retry(_ledger(case_ids), index=5)
    original = _result(case_ids[0], "issue_only")
    retried_usage = original.usage.model_copy(
        update={
            "latency_ms": 10,
            "input_tokens": 20,
            "output_tokens": 4,
            "estimated_cost_usd": Decimal("0.00000704"),
            "model_attempts": 2,
        }
    )
    issue_results = (
        original.model_copy(update={"usage": retried_usage}),
        *tuple(_result(case_id, "issue_only") for case_id in case_ids[1:]),
    )
    reposcope_results = tuple(_result(case_id, "reposcope") for case_id in case_ids)

    manifest = write_prediction_artifacts(
        output_directory=tmp_path,
        run_id="20260902T010203Z-deepseek-v4-flash",
        configuration=DeepSeekRunConfig.approved(),
        dataset_digest=DATASET_DIGEST,
        issue_only_results=issue_results,
        reposcope_results=reposcope_results,
        call_usage=ledger,
        reproduction_commands=("predict", "score"),
        reposcope_commit="a" * 40,
        dependency_versions=tuple(
            DependencyVersion(name=name, version="test")
            for name in REQUIRED_DEPENDENCIES
        ),
        execution_order=tuple(
            f"{case_id}:{system}"
            for case_id in case_ids
            for system in ("issue_only", "reposcope")
        ),
        started_at=datetime(2026, 9, 2, 12, 0, tzinfo=UTC),
        finished_at=datetime(2026, 9, 2, 12, 1, tzinfo=UTC),
    )

    assert manifest.completion_status == "complete"
    assert len(read_jsonl(tmp_path / "call-usage.v1.jsonl", ProviderCallUsage)) == 18


@pytest.mark.parametrize(
    "update",
    (
        {"phase": "report_composition"},
        {"attempt": 3},
        {"status": "failed", "safe_error_code": "output_budget_exhausted"},
    ),
)
def test_complete_validation_rejects_invalid_schema_retry_sequence(
    update: dict[str, object],
) -> None:
    case_ids = tuple(f"development-case-{number}" for number in range(1, 7))
    ledger = list(_with_schema_retry(_ledger(case_ids), index=5))
    ledger[6] = ledger[6].model_copy(update=update)

    with pytest.raises(ValueError, match="retry sequence"):
        validate_complete_prediction_evidence(
            configuration=DeepSeekRunConfig.approved(),
            dataset_digest=DATASET_DIGEST,
            issue_only=tuple(_result(case_id, "issue_only") for case_id in case_ids),
            reposcope=tuple(_result(case_id, "reposcope") for case_id in case_ids),
            call_usage=tuple(ledger),
            started_at=datetime(2026, 9, 2, 12, 0, tzinfo=UTC),
            finished_at=datetime(2026, 9, 2, 12, 1, tzinfo=UTC),
        )


@pytest.mark.anyio
async def test_predictions_run_in_fixed_paired_order_for_six_cases(
    tmp_path: Path,
) -> None:
    cases = _six_synthetic_cases()
    calls: list[tuple[str, str, Path | None]] = []

    async def execute(system, case, snapshot):
        calls.append((system, case.case_id, snapshot))
        return _result(case.case_id, system)

    issue_only, reposcope = await run_paired_predictions(
        cases=cases,
        dataset_digest=DATASET_DIGEST,
        snapshots_root=tmp_path / "snapshots",
        execute=execute,
    )

    assert len(issue_only) == len(reposcope) == 6
    assert [(system, case_id) for system, case_id, _ in calls] == [
        pair
        for case in cases
        for pair in (("issue_only", case.case_id), ("reposcope", case.case_id))
    ]
    assert all(snapshot is None for system, _, snapshot in calls if system == "issue_only")
    assert all(snapshot is not None for system, _, snapshot in calls if system == "reposcope")


@pytest.mark.parametrize(
    ("failed_system", "expected_calls"),
    (
        ("issue_only", ("issue_only",)),
        ("reposcope", ("issue_only", "reposcope")),
    ),
)
@pytest.mark.anyio
async def test_paired_predictions_stop_after_first_failed_result(
    tmp_path: Path, failed_system: str, expected_calls: tuple[str, ...]
) -> None:
    cases = _six_synthetic_cases()
    calls: list[tuple[str, str]] = []

    async def execute(system, case, snapshot):
        calls.append((system, case.case_id))
        result = _result(case.case_id, system)
        if system == failed_system:
            return result.model_copy(
                update={"status": "failed", "safe_error_code": "runner_failure"}
            )
        return result

    from app.evaluation.errors import EvaluationRunAbort

    with pytest.raises(EvaluationRunAbort):
        await run_paired_predictions(
            cases=cases,
            dataset_digest=DATASET_DIGEST,
            snapshots_root=tmp_path / "snapshots",
            execute=execute,
        )

    assert calls == [(system, cases[0].case_id) for system in expected_calls]


def test_complete_validation_rejects_total_tokens_above_hard_cap() -> None:
    case_ids = tuple(f"development-case-{number}" for number in range(1, 7))
    ledger = list(_ledger(case_ids))
    ledger[0] = ledger[0].model_copy(update={"input_tokens": 2_500_000})

    with pytest.raises(ValueError, match="fixed token budget"):
        validate_complete_prediction_evidence(
            configuration=DeepSeekRunConfig.approved(),
            dataset_digest=DATASET_DIGEST,
            issue_only=tuple(_result(case_id, "issue_only") for case_id in case_ids),
            reposcope=tuple(_result(case_id, "reposcope") for case_id in case_ids),
            call_usage=tuple(ledger),
            started_at=datetime(2026, 9, 2, 12, 0, tzinfo=UTC),
            finished_at=datetime(2026, 9, 2, 12, 1, tzinfo=UTC),
        )


@pytest.mark.anyio
async def test_schema_preflight_covers_every_real_structured_response() -> None:
    calls: list[tuple[ModelPhase, type[object], int]] = []

    class _Gateway:
        async def generate(self, *, phase, response_model, context, attempt=1):
            calls.append((phase, response_model, attempt))
            values = {
                IssueUnderstanding: IssueUnderstanding(
                    summary="Synthetic issue.",
                    observed_behavior="Observed.",
                    expected_behavior="Expected.",
                ),
                ToolRequest: ToolRequest(complete=True),
                CritiqueResult: CritiqueResult(sufficient=False),
                AnalysisReport: AnalysisReport(
                    outcome="insufficient_evidence",
                    issue_summary="Synthetic issue.",
                    observed_behavior="Observed.",
                    expected_behavior="Expected.",
                    uncertainties=("Synthetic preflight has no source evidence.",),
                    confidence=0,
                ),
                IssueOnlyModelOutput: IssueOnlyModelOutput(
                    report_outcome="insufficient_evidence"
                ),
            }
            return values[response_model]

    await preflight_structured_models(_Gateway())

    assert [response_model for _, response_model, _ in calls] == [
        IssueUnderstanding,
        ToolRequest,
        CritiqueResult,
        AnalysisReport,
        IssueOnlyModelOutput,
    ]
    assert all(attempt == 1 for _, _, attempt in calls)
