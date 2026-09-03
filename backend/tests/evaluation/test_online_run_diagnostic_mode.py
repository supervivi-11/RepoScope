"""Targeted tests for the partial diagnostic mode of the online evaluation run.

Diagnostic mode must never weaken the formal six-case benchmark guards: the
manifest pipeline, complete-evidence validation, and six-case diagnostics
validation stay untouched, while one-to-five locked development cases can be
run for debugging with an explicit non-benchmark marker.
"""

from __future__ import annotations

import asyncio
import json
from argparse import Namespace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.evaluation import online_run as module
from app.evaluation.contracts import BenchmarkCase, BenchmarkResult, EvaluationUsage
from app.evaluation.diagnostics import (
    CaseDiagnostic,
    DiagnosticStep,
    validate_diagnostic_artifact,
)
from app.evaluation.jsonl import write_jsonl
from app.evaluation.online_run import (
    _parser,
    run_paired_predictions,
    select_diagnostic_cases,
    write_diagnostic_artifacts,
)
from app.evaluation.real_contracts import DeepSeekRunConfig


DATASET_DIGEST = "b" * 64
COMMIT_SHA = "a" * 40
RUN_ID = "20260903T000000Z-deepseek-v4-flash-diagnostic"


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
            pre_fix_commit_sha=COMMIT_SHA,
            snapshot_tree_digest=DATASET_DIGEST,
        )
        for number in range(1, 7)
    )


def _steps() -> tuple[DiagnosticStep, ...]:
    nodes = (
        "understand_issue",
        "begin_round",
        "select_tool",
        "critique_evidence",
        "compose_report",
        "validate_report",
        "prepare_review",
    )
    return tuple(
        DiagnosticStep(
            sequence=sequence,
            node=node,
            tool_calls=0,
            evidence_rounds=0 if sequence == 1 else 1,
            model_attempts=1,
            evidence_before=0,
            evidence_after=0,
            hypothesis_count=0,
            hypothesis_reference_count=0,
            hypothesis_tool_matches=0,
            report_outcome=(
                "insufficient_evidence" if node == "prepare_review" else None
            ),
        )
        for sequence, node in enumerate(nodes, start=1)
    )


def _row(case_id: str = "development-case-1") -> CaseDiagnostic:
    return CaseDiagnostic(
        case_id=case_id,
        dataset_digest=DATASET_DIGEST,
        commit_sha=COMMIT_SHA,
        status="complete",
        steps=_steps(),
    )


def _write_journal(path: Path, rows: tuple[CaseDiagnostic, ...]) -> Path:
    write_jsonl(path, rows)
    return path


def test_parser_collects_repeated_case_flags() -> None:
    args = _parser().parse_args(
        [
            "--cases",
            "cases.jsonl",
            "--dataset-digest-file",
            "digest.sha256",
            "--snapshots-root",
            "snapshots",
            "--output-directory",
            "out",
            "--case",
            "alpha",
            "--case",
            "beta",
        ]
    )
    assert args.case_filter == ["alpha", "beta"]


def test_parser_defaults_case_filter_to_none() -> None:
    args = _parser().parse_args(
        [
            "--cases",
            "cases.jsonl",
            "--dataset-digest-file",
            "digest.sha256",
            "--snapshots-root",
            "snapshots",
            "--output-directory",
            "out",
        ]
    )
    assert args.case_filter is None


def test_select_diagnostic_cases_dedupes_and_keeps_requested_order() -> None:
    selected = select_diagnostic_cases(
        _six_synthetic_cases(),
        ("development-case-3", "development-case-1", "development-case-3"),
    )
    assert [case.case_id for case in selected] == [
        "development-case-3",
        "development-case-1",
    ]


def test_select_diagnostic_cases_enforces_bounds_and_membership() -> None:
    cases = _six_synthetic_cases()
    with pytest.raises(ValueError, match="one to five"):
        select_diagnostic_cases(cases, ())
    with pytest.raises(ValueError, match="one to five"):
        select_diagnostic_cases(cases, tuple(f"development-case-{i}" for i in range(1, 7)))
    with pytest.raises(ValueError, match="not locked development cases"):
        select_diagnostic_cases(cases, ("hidden-case-1",))


@pytest.mark.anyio
async def test_partial_paired_predictions_accept_one_development_case(
    tmp_path: Path,
) -> None:
    executed: list[tuple[str, str, Path | None]] = []

    async def execute(system, case, snapshot):
        executed.append((system, case.case_id, snapshot))
        return _result(case.case_id, system)

    issue_only, reposcope = await run_paired_predictions(
        cases=_six_synthetic_cases()[:1],
        dataset_digest=DATASET_DIGEST,
        snapshots_root=tmp_path,
        execute=execute,
        require_complete_split=False,
    )
    assert [item.case_id for item in issue_only] == ["development-case-1"]
    assert [item.case_id for item in reposcope] == ["development-case-1"]
    assert executed == [
        ("issue_only", "development-case-1", None),
        ("reposcope", "development-case-1", tmp_path / "development-case-1"),
    ]


@pytest.mark.anyio
async def test_formal_paired_predictions_still_require_exactly_six(
    tmp_path: Path,
) -> None:
    async def execute(system, case, snapshot):  # pragma: no cover - must not run
        raise AssertionError("formal guard must reject before execution")

    with pytest.raises(ValueError, match="exactly six"):
        await run_paired_predictions(
            cases=_six_synthetic_cases()[:1],
            dataset_digest=DATASET_DIGEST,
            snapshots_root=tmp_path,
            execute=execute,
        )


@pytest.mark.anyio
async def test_partial_paired_predictions_reject_hidden_split(
    tmp_path: Path,
) -> None:
    hidden = BenchmarkCase(
        schema_version="reposcope.eval.case.v1",
        case_id="hidden-case-1",
        split="hidden",
        repo_url="https://github.com/example/project",
        issue_number=99,
        issue_title="Synthetic hidden issue",
        pre_fix_commit_sha=COMMIT_SHA,
        snapshot_tree_digest=DATASET_DIGEST,
    )

    async def execute(system, case, snapshot):  # pragma: no cover - must not run
        raise AssertionError("split guard must reject before execution")

    with pytest.raises(ValueError, match="development"):
        await run_paired_predictions(
            cases=(hidden,),
            dataset_digest=DATASET_DIGEST,
            snapshots_root=tmp_path,
            execute=execute,
            require_complete_split=False,
        )


def test_partial_diagnostic_validation_accepts_matching_single_case(
    tmp_path: Path,
) -> None:
    path = _write_journal(tmp_path / "diagnostics.v1.jsonl", (_row(),))
    validate_diagnostic_artifact(
        path,
        results=(_result("development-case-1", "reposcope"),),
        dataset_digest=DATASET_DIGEST,
        allow_partial=True,
    )


def test_partial_diagnostic_validation_rejects_six_results(tmp_path: Path) -> None:
    path = _write_journal(tmp_path / "diagnostics.v1.jsonl", (_row(),))
    with pytest.raises(ValueError, match="one to five"):
        validate_diagnostic_artifact(
            path,
            results=tuple(
                _result(f"development-case-{number}", "reposcope")
                for number in range(1, 7)
            ),
            dataset_digest=DATASET_DIGEST,
            allow_partial=True,
        )


def test_default_diagnostic_validation_still_requires_six_rows(
    tmp_path: Path,
) -> None:
    path = _write_journal(tmp_path / "diagnostics.v1.jsonl", (_row(),))
    with pytest.raises(ValueError, match="six predictions"):
        validate_diagnostic_artifact(
            path,
            results=(_result("development-case-1", "reposcope"),),
            dataset_digest=DATASET_DIGEST,
        )


def test_partial_diagnostic_validation_rejects_identity_mismatch(
    tmp_path: Path,
) -> None:
    path = _write_journal(tmp_path / "diagnostics.v1.jsonl", (_row(),))
    with pytest.raises(ValueError, match="every prediction"):
        validate_diagnostic_artifact(
            path,
            results=(_result("development-case-2", "reposcope"),),
            dataset_digest=DATASET_DIGEST,
            allow_partial=True,
        )


def test_write_diagnostic_artifacts_never_writes_a_manifest(tmp_path: Path) -> None:
    out = tmp_path / "diagnostic-out"
    out.mkdir()
    _write_journal(out / "diagnostics.v1.jsonl", (_row(),))
    started = datetime(2026, 9, 3, tzinfo=UTC)
    summary = write_diagnostic_artifacts(
        output_directory=out,
        run_id=RUN_ID,
        configuration=DeepSeekRunConfig.approved(),
        dataset_digest=DATASET_DIGEST,
        requested_cases=("development-case-1",),
        issue_only_results=(_result("development-case-1", "issue_only"),),
        reposcope_results=(_result("development-case-1", "reposcope"),),
        call_usage=(),
        reproduction_commands=("& 'python' '--case' 'development-case-1'",),
        started_at=started,
        finished_at=started,
    )
    assert summary.run_id == RUN_ID
    assert summary.case_ids == ("development-case-1",)
    names = {path.name for path in out.iterdir()}
    assert names == {
        "diagnostics.v1.jsonl",
        "run-config.json",
        "issue-only.results.v1.jsonl",
        "reposcope.results.v1.jsonl",
        "call-usage.v1.jsonl",
        "reproduce.txt",
        "diagnostic-run.v1.json",
    }
    marker = json.loads(
        (out / "diagnostic-run.v1.json").read_text(encoding="utf-8")
    )
    assert marker["mode"] == "diagnostic"
    assert marker["completed_cases"] == ["development-case-1"]
    assert "not a benchmark" in marker["note"]
    reproduce = (out / "reproduce.txt").read_text(encoding="utf-8")
    assert "--case" in reproduce and "development-case-1" in reproduce


def _diagnostic_args(tmp_path: Path, case_filter: list[str]) -> Namespace:
    return Namespace(
        split="development",
        cases="evals/benchmark-cases.v1.jsonl",
        dataset_digest_file="evals/benchmark-cases.v1.sha256",
        snapshots_root=str(tmp_path / "snapshots"),
        output_directory=str(tmp_path / "out"),
        case_filter=case_filter,
    )


def _patch_diagnostic_flow(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        module,
        "load_development_inputs",
        lambda **kwargs: (_six_synthetic_cases(), DATASET_DIGEST),
    )

    class FakeJournal:
        def __init__(self, path: Path, *, dataset_digest: str) -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            write_jsonl(path, (_row(),))

    monkeypatch.setattr(module, "DiagnosticJournal", FakeJournal)

    async def fake_execute(**kwargs):
        assert kwargs["require_complete_split"] is False
        assert [case.case_id for case in kwargs["cases"]] == ["development-case-1"]
        started = datetime(2026, 9, 3, tzinfo=UTC)
        return (
            (_result("development-case-1", "issue_only"),),
            (_result("development-case-1", "reposcope"),),
            (),
            started,
            started,
        )

    monkeypatch.setattr(module, "_execute_paired_predictions", fake_execute)


def test_run_online_diagnostic_branch_writes_partial_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_diagnostic_flow(monkeypatch, tmp_path)
    summary = asyncio.run(
        module._run_online(_diagnostic_args(tmp_path, ["development-case-1"]))
    )
    assert summary.case_ids == ("development-case-1",)
    out = tmp_path / "out"
    assert not (out / "prediction-manifest.v1.json").exists()
    assert (out / "diagnostic-run.v1.json").exists()
    reproduce = (out / "reproduce.txt").read_text(encoding="utf-8")
    assert "--case" in reproduce and "development-case-1" in reproduce


def test_run_online_diagnostic_branch_rejects_unknown_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        module,
        "load_development_inputs",
        lambda **kwargs: (_six_synthetic_cases(), DATASET_DIGEST),
    )
    with pytest.raises(ValueError, match="not locked development cases"):
        asyncio.run(module._run_online(_diagnostic_args(tmp_path, ["not-a-case"])))
    assert not (tmp_path / "out").exists()


def test_run_online_diagnostic_branch_rejects_full_split(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        module,
        "load_development_inputs",
        lambda **kwargs: (_six_synthetic_cases(), DATASET_DIGEST),
    )
    with pytest.raises(ValueError, match="one to five"):
        asyncio.run(
            module._run_online(
                _diagnostic_args(
                    tmp_path, [f"development-case-{number}" for number in range(1, 7)]
                )
            )
        )
    assert not (tmp_path / "out").exists()
