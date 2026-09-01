from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from app.agent import EvidenceCitation
from app.evaluation.contracts import (
    BenchmarkCase,
    BenchmarkGold,
    BenchmarkResult,
    BenchmarkSlot,
    CurationCandidate,
    EvaluationUsage,
    EVALUATION_JSONL_ROW_MAX_BYTES,
)
from app.evaluation.jsonl import JsonlContractError, read_jsonl, write_jsonl
from app.evaluation.metrics import score_benchmark
from app.ingestion import RepositorySnapshot
from app.investigation import PythonRepositoryIndex


SHA = "a" * 40
DIGEST = "b" * 64


def _case(case_id: str = "dev-01") -> BenchmarkCase:
    return BenchmarkCase(
        schema_version="reposcope.eval.case.v1",
        case_id=case_id,
        split="development",
        repo_url="https://github.com/example/project",
        issue_number=17,
        issue_title="Parser fails at an empty boundary",
        issue_body="A frozen issue body.",
        pre_fix_commit_sha=SHA,
        snapshot_tree_digest=DIGEST,
    )


def _result(
    *,
    system: str = "reposcope",
    predicted_files: tuple[str, ...] = ("src/x.py", "src/a.py"),
    citations: tuple[EvidenceCitation, ...] = (),
    status: str = "ok",
) -> BenchmarkResult:
    return BenchmarkResult(
        schema_version="reposcope.eval.result.v1",
        case_id="dev-01",
        system=system,
        status=status,
        predicted_files=predicted_files if status == "ok" else (),
        citations=citations,
        report_outcome="root_cause_identified" if status == "ok" else None,
        runner_id="scripted-offline-v1",
        model_id=None,
        dataset_digest=DIGEST,
        usage=EvaluationUsage(),
        safe_error_code=None if status == "ok" else "scripted_failure",
    )


def _index(tmp_path: Path) -> PythonRepositoryIndex:
    root = tmp_path / "snapshot"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_bytes(b"first = 1\nsecond = 2\n")
    (root / "src" / "b.py").write_bytes(b"third = 3\n")
    snapshot = RepositorySnapshot.create(
        owner="example",
        repository="project",
        commit_sha=SHA,
        root_path=root,
        indexed_byte_count=21,
    )
    return PythonRepositoryIndex.build(snapshot)


def test_contracts_are_strict_versioned_and_keep_gold_separate() -> None:
    slot = BenchmarkSlot(
        schema_version="reposcope.eval.slot.v1",
        slot_id="dev-01",
        split="development",
    )
    assert slot.schema_version == "reposcope.eval.slot.v1"
    assert slot.state == "unfilled"

    with pytest.raises(ValidationError):
        BenchmarkCase.model_validate({**_case().model_dump(), "gold_files": ["src/a.py"]})
    with pytest.raises(ValidationError):
        BenchmarkGold(
            schema_version="reposcope.eval.gold.v1",
            case_id="dev-01",
            gold_files=("../a.py",),
        )
    with pytest.raises(ValidationError):
        BenchmarkGold(
            schema_version="reposcope.eval.gold.v1",
            case_id="dev-01",
            gold_files=("tests/test_parser.py",),
        )
    with pytest.raises(ValidationError):
        _result(predicted_files=("src/a.py", "src/a.py"))


def test_pre_split_candidate_accepts_only_runner_safe_frozen_input() -> None:
    payload = {
        "schema_version": "reposcope.eval.candidate.v1",
        "candidate_id": "python-hyper-h11-issue-92",
        "state": "qualified_pending_dataset_lock",
        "repo_url": "https://github.com/python-hyper/h11",
        "issue_number": 92,
        "issue_title": "Handle repeated content lengths",
        "issue_body": "The parser rejects equal values.",
        "pre_fix_commit_sha": SHA,
        "snapshot_tree_digest": DIGEST,
    }

    candidate = CurationCandidate.model_validate(payload)

    assert candidate.candidate_id == "python-hyper-h11-issue-92"
    for forbidden in (
        "split",
        "slot_id",
        "case_id",
        "fix_pr_url",
        "fix_commit_sha",
        "changed_files",
        "gold_files",
    ):
        with pytest.raises(ValidationError):
            CurationCandidate.model_validate({**payload, forbidden: "must-not-enter"})


def test_jsonl_writer_is_canonical_and_reader_rejects_duplicate_keys(tmp_path: Path) -> None:
    target = tmp_path / "slots.jsonl"
    rows = (
        BenchmarkSlot(schema_version="reposcope.eval.slot.v1", slot_id="dev-02", split="development"),
        BenchmarkSlot(schema_version="reposcope.eval.slot.v1", slot_id="dev-01", split="development"),
    )

    write_jsonl(target, rows)

    lines = target.read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0])["slot_id"] == "dev-01"
    assert target.read_bytes().endswith(b"\n")
    assert read_jsonl(target, BenchmarkSlot) == tuple(sorted(rows, key=lambda row: row.slot_id))

    target.write_text(
        '{"schema_version":"reposcope.eval.slot.v1","slot_id":"dev-01",'
        '"slot_id":"dev-02","split":"development","state":"unfilled"}\n',
        encoding="utf-8",
    )
    with pytest.raises(JsonlContractError, match="duplicate JSON key"):
        read_jsonl(target, BenchmarkSlot)


def test_jsonl_row_limit_excludes_the_record_delimiter(tmp_path: Path) -> None:
    class SizedRow(BaseModel):
        case_id: str
        payload: str

    empty = SizedRow(case_id="row-1", payload="")
    empty_payload = json.dumps(
        empty.model_dump(), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    exact = SizedRow(
        case_id="row-1",
        payload="x" * (EVALUATION_JSONL_ROW_MAX_BYTES - len(empty_payload)),
    )
    target = tmp_path / "exact.jsonl"

    write_jsonl(target, (exact,))

    assert len(target.read_bytes()) == EVALUATION_JSONL_ROW_MAX_BYTES + 1
    assert read_jsonl(target, SizedRow) == (exact,)
    oversized = exact.model_copy(update={"payload": exact.payload + "x"})
    with pytest.raises(JsonlContractError, match="row is too large"):
        write_jsonl(target, (oversized,))


def test_paired_result_rows_round_trip_with_composite_identity(tmp_path: Path) -> None:
    target = tmp_path / "paired.jsonl"
    rows = (_result(system="reposcope"), _result(system="issue_only"))

    write_jsonl(target, rows)

    assert read_jsonl(target, BenchmarkResult) == tuple(
        sorted(rows, key=lambda item: (item.case_id, item.system))
    )


def test_external_rows_require_explicit_version_and_strict_issue_number() -> None:
    payload = _case().model_dump(exclude={"schema_version"})
    with pytest.raises(ValidationError):
        BenchmarkCase.model_validate(payload)
    with pytest.raises(ValidationError):
        BenchmarkCase.model_validate({**_case().model_dump(), "issue_number": "17"})


def test_result_contract_rejects_a_row_above_the_jsonl_limit() -> None:
    citations = tuple(
        EvidenceCitation(
            commit_sha=SHA,
            path="src/a.py",
            start_line=index,
            end_line=index,
            excerpt="界" * 8_000,
            explanation="x" * 1_500,
        )
        for index in range(1, 65)
    )
    with pytest.raises(ValidationError, match="JSONL row"):
        _result(citations=citations)


def test_metrics_use_macro_recall_mrr_and_exact_citation_validation(tmp_path: Path) -> None:
    index = _index(tmp_path)
    exact = EvidenceCitation(
        commit_sha=SHA,
        path="src/a.py",
        start_line=1,
        end_line=1,
        excerpt="first = 1\n",
        explanation="Exact pre-fix evidence.",
    )
    wrong_excerpt = exact.model_copy(update={"excerpt": "invented = True\n"})
    gold = BenchmarkGold(
        schema_version="reposcope.eval.gold.v1",
        case_id="dev-01",
        gold_files=("src/a.py", "src/b.py"),
    )

    summary = score_benchmark(
        cases=(_case(),),
        gold=(gold,),
        results=(
            _result(system="issue_only", predicted_files=("src/x.py", "src/a.py")),
            _result(
                predicted_files=("src/x.py", "src/a.py", "src/y.py", "src/b.py"),
                citations=(exact, wrong_excerpt),
            ),
        ),
        indexes={"dev-01": index},
    )

    reposcope = next(item for item in summary.systems if item.system == "reposcope")
    assert reposcope.file_recall_at_5 == 1.0
    assert reposcope.mrr == 0.5
    assert reposcope.citations_emitted == 2
    assert reposcope.citations_valid == 1
    assert reposcope.hallucinated_citations == 1
    assert reposcope.citation_validity == 0.5
    assert reposcope.citation_hallucination_rate == 0.5
    assert summary.file_recall_at_5_delta == 0.5


def test_rank_six_mrr_failed_cases_and_empty_citation_rates_are_truthful(tmp_path: Path) -> None:
    cases = (_case("dev-01"), _case("dev-02").model_copy(update={"case_id": "dev-02"}))
    gold = (
        BenchmarkGold(schema_version="reposcope.eval.gold.v1", case_id="dev-01", gold_files=("src/a.py",)),
        BenchmarkGold(schema_version="reposcope.eval.gold.v1", case_id="dev-02", gold_files=("src/a.py",)),
    )
    rank_six = ("x1.py", "x2.py", "x3.py", "x4.py", "x5.py", "src/a.py")
    failed = _result(status="failed").model_copy(update={"case_id": "dev-02"})
    index = _index(tmp_path)
    summary = score_benchmark(
        cases=cases,
        gold=gold,
        results=(_result(predicted_files=rank_six), failed),
        indexes={"dev-01": index, "dev-02": index},
    )

    system = summary.systems[0]
    assert system.file_recall_at_5 == 0.0
    assert system.mrr == 0.083333
    assert system.citation_validity is None
    assert system.citation_hallucination_rate is None
    assert system.citations_emitted == 0


def test_scoring_rejects_missing_rows_and_cost_stays_null_without_rate_card(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="exactly one result"):
        score_benchmark(
            cases=(_case(),),
            gold=(BenchmarkGold(schema_version="reposcope.eval.gold.v1", case_id="dev-01", gold_files=("src/a.py",)),),
            results=(),
            indexes={},
        )
    result = _result().model_copy(
        update={"usage": EvaluationUsage(input_tokens=10, output_tokens=20)}
    )
    assert result.usage.estimated_cost_usd is None


def test_scoring_rejects_gold_that_is_not_present_in_the_pre_fix_snapshot(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="gold file"):
        score_benchmark(
            cases=(_case(),),
            gold=(BenchmarkGold(schema_version="reposcope.eval.gold.v1", case_id="dev-01", gold_files=("src/missing.py",)),),
            results=(_result(predicted_files=("src/missing.py",)),),
            indexes={"dev-01": _index(tmp_path)},
        )


def test_usage_aggregates_are_null_until_every_case_has_observed_values(
    tmp_path: Path,
) -> None:
    cases = (_case("dev-01"), _case("dev-02").model_copy(update={"case_id": "dev-02"}))
    gold = tuple(
        BenchmarkGold(
            schema_version="reposcope.eval.gold.v1",
            case_id=case.case_id,
            gold_files=("src/a.py",),
        )
        for case in cases
    )
    complete = _result().model_copy(
        update={
            "usage": EvaluationUsage(
                latency_ms=100,
                input_tokens=10,
                output_tokens=5,
                estimated_cost_usd="0.001",
                rate_card_version="example-v1",
            )
        }
    )
    missing = _result().model_copy(update={"case_id": "dev-02"})

    index = _index(tmp_path)
    summary = score_benchmark(
        cases=cases,
        gold=gold,
        results=(complete, missing),
        indexes={"dev-01": index, "dev-02": index},
    )

    system = summary.systems[0]
    assert system.median_latency_ms is None
    assert system.total_input_tokens is None
    assert system.total_output_tokens is None
    assert system.estimated_cost_usd is None
