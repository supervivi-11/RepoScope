from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import pytest

from app.evaluation.catalog import load_candidate_catalog, load_slot_catalog
from app.evaluation.contracts import BenchmarkCase, CurationCandidate, EvaluationPrediction
from app.evaluation.jsonl import JsonlContractError, write_jsonl
from app.evaluation.runners import IssueOnlyRunner, RepoScopeRunner
from app.evaluation.errors import EvaluationRunAbort
from app.evaluation.snapshot import snapshot_tree_digest
from app.telemetry import StructuredTelemetry


SHA = "a" * 40
DIGEST = "b" * 64


def _candidate(candidate_id: str, repo_url: str, issue_number: int) -> CurationCandidate:
    return CurationCandidate(
        schema_version="reposcope.eval.candidate.v1",
        candidate_id=candidate_id,
        state="qualified_pending_dataset_lock",
        repo_url=repo_url,
        issue_number=issue_number,
        issue_title="Frozen issue",
        issue_body=None,
        pre_fix_commit_sha=SHA,
        snapshot_tree_digest=DIGEST,
    )


def _case() -> BenchmarkCase:
    return BenchmarkCase(
        schema_version="reposcope.eval.case.v1",
        case_id="dev-01",
        split="development",
        repo_url="https://github.com/example/project",
        issue_number=7,
        issue_title="Frozen title",
        issue_body="GOLD_CANARY must never be supplied separately.",
        pre_fix_commit_sha=SHA,
        snapshot_tree_digest=DIGEST,
    )


def test_committed_catalog_has_twelve_unfilled_slots_and_six_six_split() -> None:
    root = Path(__file__).resolve().parents[3]
    slots = load_slot_catalog(root / "evals" / "benchmark-slots.v1.jsonl")
    assert [item.slot_id for item in slots] == [
        *(f"dev-{index:02d}" for index in range(1, 7)),
        *(f"hidden-{index:02d}" for index in range(1, 7)),
    ]
    assert sum(item.split == "development" for item in slots) == 6
    assert sum(item.split == "hidden" for item in slots) == 6
    assert all(item.state == "unfilled" for item in slots)


def test_candidate_catalog_accepts_verified_inputs_without_assigning_a_split(
    tmp_path: Path,
) -> None:
    path = tmp_path / "candidates.jsonl"
    candidates = tuple(
        _candidate(candidate_id, repo_url, issue_number)
        for candidate_id, repo_url, issue_number in (
            ("python-hyper-h11-issue-92", "https://github.com/python-hyper/h11", 92),
            ("pallets-click-issue-2819", "https://github.com/pallets/click", 2819),
        )
    )
    write_jsonl(path, candidates)

    loaded = load_candidate_catalog(path)

    assert loaded == (candidates[1], candidates[0])
    assert all("split" not in type(item).model_fields for item in loaded)


def test_committed_candidates_remain_pre_split_and_runner_safe() -> None:
    root = Path(__file__).resolve().parents[3]

    candidates = load_candidate_catalog(root / "evals" / "curation-candidates.v1.jsonl")

    assert [item.candidate_id for item in candidates] == [
        "dateutil-dateutil-issue-926",
        "hynek-structlog-issue-476",
        "more-itertools-more-itertools-issue-658",
        "pallets-click-issue-2819",
        "pallets-flask-issue-2267",
        "pallets-jinja-issue-1198",
        "pydantic-pydantic-settings-issue-441",
        "pyinvoke-invoke-issue-533",
        "pytest-dev-pluggy-issue-544",
        "python-hyper-h11-issue-92",
        "python-poetry-tomlkit-issue-261",
        "tox-dev-platformdirs-issue-207",
    ]
    assert all("split" not in type(item).model_fields for item in candidates)
    assert all("gold_files" not in type(item).model_fields for item in candidates)


def test_candidate_catalog_rejects_a_second_case_from_the_same_repository(
    tmp_path: Path,
) -> None:
    path = tmp_path / "candidates.jsonl"
    write_jsonl(
        path,
        (
            _candidate("h11-issue-92", "https://github.com/python-hyper/h11", 92),
            _candidate("h11-issue-121", "https://github.com/PYTHON-HYPER/H11", 121),
        ),
    )

    with pytest.raises(JsonlContractError, match="one candidate per repository"):
        load_candidate_catalog(path)


def test_candidate_catalog_rejects_more_than_twelve_candidates(tmp_path: Path) -> None:
    path = tmp_path / "candidates.jsonl"
    write_jsonl(
        path,
        tuple(
            _candidate(
                f"owner-{index}-repo-issue-{index}",
                f"https://github.com/owner-{index}/repo",
                index,
            )
            for index in range(1, 14)
        ),
    )

    with pytest.raises(JsonlContractError, match="one to twelve"):
        load_candidate_catalog(path)


def test_candidate_catalog_rejects_an_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "candidates.jsonl"
    path.write_bytes(b"")

    with pytest.raises(JsonlContractError, match="one to twelve"):
        load_candidate_catalog(path)


@pytest.mark.anyio
async def test_issue_only_runner_exposes_no_snapshot_or_gold_to_predictor() -> None:
    received = []

    async def predictor(value):
        received.append(value)
        return EvaluationPrediction(predicted_files=("src/a.py",))

    result = await IssueOnlyRunner(predictor, runner_id="scripted-v1").run(
        _case(), dataset_digest=DIGEST
    )

    assert result.system == "issue_only"
    assert result.predicted_files == ("src/a.py",)
    assert received[0].model_dump() == {
        "repo_url": "https://github.com/example/project",
        "issue_number": 7,
        "issue_title": "Frozen title",
        "issue_body": "GOLD_CANARY must never be supplied separately.",
    }
    assert "pre_fix_commit_sha" not in type(received[0]).model_fields


@pytest.mark.anyio
async def test_runner_failures_become_safe_failed_rows_without_exception_text() -> None:
    async def predictor(value):
        raise RuntimeError("Authorization: Bearer SECRET_PROVIDER_KEY")

    result = await IssueOnlyRunner(predictor, runner_id="scripted-v1").run(
        _case(), dataset_digest=DIGEST
    )

    assert result.status == "failed"
    assert result.safe_error_code == "runner_failure"
    assert result.predicted_files == ()
    assert "SECRET_PROVIDER_KEY" not in result.model_dump_json()


@pytest.mark.anyio
async def test_runner_never_swallows_evaluation_safety_abort() -> None:
    async def predictor(value):
        raise EvaluationRunAbort("stop the paid run")

    with pytest.raises(EvaluationRunAbort, match="stop the paid run"):
        await IssueOnlyRunner(predictor, runner_id="scripted-v1").run(
            _case(), dataset_digest="a" * 64
        )


@pytest.mark.anyio
async def test_reposcope_runner_copies_snapshot_and_always_cleans_temporary_root(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "module.py").write_text("value = 1\n", encoding="utf-8")
    case = _case().model_copy(update={"snapshot_tree_digest": snapshot_tree_digest(source)})
    seen = []

    async def analyzer(case, snapshot):
        seen.append(snapshot)
        assert snapshot != source
        assert snapshot.is_dir()
        return EvaluationPrediction(predicted_files=("module.py",))

    runner = RepoScopeRunner(analyzer, runner_id="scripted-v1")
    result = await runner.run(case, snapshot_source=source, dataset_digest=DIGEST)

    assert result.system == "reposcope"
    assert not seen[0].exists()
    assert source.exists()

    async def cancelled(case, snapshot):
        seen.append(snapshot)
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await RepoScopeRunner(cancelled, runner_id="scripted-v1").run(
            case, snapshot_source=source, dataset_digest=DIGEST
        )
    assert not seen[-1].exists()
    assert source.exists()


@pytest.mark.anyio
async def test_reposcope_runner_rejects_a_snapshot_that_does_not_match_frozen_digest(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "module.py").write_bytes(b"value = 1\n")

    async def analyzer(case, snapshot):
        raise AssertionError("analyzer must not receive an unverified snapshot")

    with pytest.raises(ValueError, match="digest"):
        await RepoScopeRunner(analyzer, runner_id="scripted-v1").run(
            _case(), snapshot_source=source, dataset_digest=DIGEST
        )


def test_snapshot_digest_rejects_files_above_the_v1_per_file_limit(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "oversized.py").write_bytes(b"x" * 500_001)

    with pytest.raises(ValueError, match="per-file"):
        snapshot_tree_digest(source)


def test_structured_telemetry_emits_allowlisted_json_without_sensitive_values(
    caplog: pytest.LogCaptureFixture,
) -> None:
    telemetry = StructuredTelemetry(service="worker", clock=lambda: 42.0)
    with caplog.at_level(logging.INFO, logger="reposcope.telemetry"):
        started = telemetry.start()
        telemetry.emit(
            "analysis_completed",
            started_at=started,
            analysis_id="analysis-1",
            status="COMPLETED",
            tool_calls=3,
            issue_body="SECRET_ISSUE_BODY",
            snapshot_path="C:/SECRET/SNAPSHOT",
            exception="SECRET_EXCEPTION",
        )

    payload = json.loads(caplog.records[-1].message)
    timestamp = payload.pop("timestamp")
    assert timestamp.endswith("+00:00")
    assert payload == {
        "analysis_id": "analysis-1",
        "duration_ms": 0,
        "event": "analysis_completed",
        "level": "INFO",
        "service": "worker",
        "status": "COMPLETED",
        "tool_calls": 3,
    }
    assert "SECRET" not in caplog.records[-1].message
