from __future__ import annotations

from collections import defaultdict, deque
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.agent import (
    AnalysisReport,
    CritiqueResult,
    EvidenceCitation,
    Hypothesis,
    IssueUnderstanding,
    ModelPhase,
    PermanentModelError,
    ToolRequest,
)
from app.evaluation.deepseek_provider import InMemoryCallLedger
from app.evaluation.predictors import (
    IssueOnlyModelOutput,
    RealIssueOnlyPredictor,
    RealRepoScopeAnalyzer,
    _model_id,
)
from app.evaluation.real_contracts import DeepSeekRunConfig, ProviderCallUsage
from app.evaluation.runners import IssueOnlyInput
from app.evaluation.contracts import BenchmarkCase
from app.evaluation.snapshot import snapshot_tree_digest


SHA = "a" * 40
SOURCE = "def parse(value):\n    return value\n"


@pytest.mark.parametrize("tool_name", ["get_related_issues", "get_recent_commits"])
@pytest.mark.anyio
async def test_frozen_analyzer_cannot_read_live_history(tmp_path: Path, tool_name: str) -> None:
    import json
    from types import SimpleNamespace

    class LiveHistorySpy:
        calls = 0

        async def fetch_related_issues(self, *args, **kwargs):
            self.calls += 1
            return (SimpleNamespace(number=99, title="POST_FIX_CANARY",
                                    body="POST_FIX_CANARY", state="closed",
                                    html_url="https://github.com/example/project/issues/99"),)

        async def fetch_recent_commits(self, *args, **kwargs):
            self.calls += 1
            return (SimpleNamespace(sha="b" * 40, message="POST_FIX_CANARY",
                                    html_url="https://github.com/example/project/commit/" + "b" * 40,
                                    committed_at=datetime(2026, 9, 3, tzinfo=UTC)),)

    snapshot = tmp_path / "snapshot"
    source = snapshot / "src" / "parser.py"
    source.parent.mkdir(parents=True)
    source.write_text(SOURCE, encoding="utf-8", newline="")
    gateway = _repo_gateway()
    gateway.responses[ModelPhase.TOOL_SELECTION].appendleft(ToolRequest(
        tool_name=tool_name,
        arguments={"query": "parse"} if tool_name == "get_related_issues" else {},
    ))
    live = LiveHistorySpy()
    analyzer = RealRepoScopeAnalyzer(
        model=gateway, configuration=DeepSeekRunConfig.approved(),
        ledger=InMemoryCallLedger(max_total_tokens=2_500_000), github=live,
    )
    prediction = await analyzer(_case(snapshot), snapshot)
    assert live.calls == 0
    history = gateway.contexts[ModelPhase.REPORT_COMPOSITION][0]["tool_history"]
    assert history[0]["tool_name"] == tool_name
    assert history[0]["succeeded"] is False
    assert history[0]["result_summary"] is None
    assert history[0]["safe_error"] == "Tool execution failed safely."
    assert "POST_FIX_CANARY" not in json.dumps(gateway.contexts)
    assert history[1]["succeeded"] is True
    assert prediction.citations == (_citation(),)
    assert prediction.usage.tool_calls == 2


class _GithubFake:
    async def fetch_recent_commits(self, *args, **kwargs):
        return ()

    async def fetch_related_issues(self, *args, **kwargs):
        return ()


class _ScriptedGateway:
    def __init__(self, responses: dict[ModelPhase, list[object]]) -> None:
        self.responses = {phase: deque(values) for phase, values in responses.items()}
        self.contexts: dict[ModelPhase, list[dict[str, object]]] = defaultdict(list)

    async def generate(self, *, phase, response_model, context, attempt=1):
        self.contexts[phase].append(context)
        value = self.responses[phase].popleft()
        if isinstance(value, Exception):
            raise value
        return value


def _case(snapshot: Path) -> BenchmarkCase:
    return BenchmarkCase(
        schema_version="reposcope.eval.case.v1",
        case_id="example-project-issue-17",
        split="development",
        repo_url="https://github.com/example/project",
        issue_number=17,
        issue_title="Parser accepts an invalid value",
        issue_body="parse() returns a value that should be rejected.",
        pre_fix_commit_sha=SHA,
        snapshot_tree_digest=snapshot_tree_digest(snapshot),
    )


def _citation() -> EvidenceCitation:
    return EvidenceCitation(
        commit_sha=SHA,
        path="src/parser.py",
        start_line=1,
        end_line=2,
        excerpt=SOURCE,
        explanation="The parser returns without validation.",
    )


def _repo_gateway() -> _ScriptedGateway:
    citation = _citation()
    report = AnalysisReport(
        outcome="root_cause_identified",
        issue_summary="The parser accepts an invalid value.",
        observed_behavior="The invalid value is returned.",
        expected_behavior="The invalid value is rejected.",
        primary_hypothesis=Hypothesis(
            statement="Validation is missing.",
            confidence=0.9,
            evidence=(citation.summary(),),
        ),
        evidence=(citation,),
        impacted_files=(
            {
                "path": "src/parser.py",
                "explanation": "The parser implementation must validate the value.",
            },
        ),
        implementation_steps=("Validate before returning.",),
        confidence=0.9,
    )
    return _ScriptedGateway(
        {
            ModelPhase.ISSUE_UNDERSTANDING: [
                IssueUnderstanding(
                    summary="The parser accepts an invalid value.",
                    observed_behavior="The invalid value is returned.",
                    expected_behavior="The invalid value is rejected.",
                    search_terms=("parse",),
                )
            ],
            ModelPhase.TOOL_SELECTION: [
                ToolRequest(
                    tool_name="read_code",
                    arguments={"path": "src/parser.py", "start_line": 1, "end_line": 2},
                ),
                ToolRequest(complete=True),
            ],
            ModelPhase.EVIDENCE_CRITIQUE: [
                CritiqueResult(
                    sufficient=True,
                    hypotheses=(report.primary_hypothesis,),
                    evidence=(citation,),
                )
            ],
            ModelPhase.REPORT_COMPOSITION: [report],
        }
    )


def test_model_id_includes_a_failed_schema_attempt_that_later_succeeds() -> None:
    records = (
        ProviderCallUsage.model_construct(
            status="failed", returned_model="deepseek-v4-flash"
        ),
        ProviderCallUsage.model_construct(
            status="success", returned_model="deepseek-v4-flash"
        ),
    )

    assert _model_id(records) == "deepseek-v4-flash"


@pytest.mark.anyio
async def test_issue_only_predictor_receives_only_frozen_issue_fields() -> None:
    gateway = _ScriptedGateway(
        {
            ModelPhase.ISSUE_ONLY_PREDICTION: [
                IssueOnlyModelOutput(
                    predicted_files=("src/parser.py", "src/validation.py"),
                    report_outcome="root_cause_identified",
                )
            ]
        }
    )
    predictor = RealIssueOnlyPredictor(
        model=gateway,
        configuration=DeepSeekRunConfig.approved(),
        ledger=InMemoryCallLedger(max_total_tokens=2_500_000),
    )
    value = IssueOnlyInput(
        repo_url="https://github.com/example/project",
        issue_number=17,
        issue_title="Parser accepts an invalid value",
        issue_body="Observed failure.",
    )

    prediction = await predictor(value)

    assert prediction.predicted_files == ("src/parser.py", "src/validation.py")
    assert prediction.citations == ()
    assert prediction.usage.tool_calls == 0
    assert prediction.usage.model_attempts == 1
    context = gateway.contexts[ModelPhase.ISSUE_ONLY_PREDICTION][0]
    assert context == {
        "repository_url": value.repo_url,
        "issue_number": 17,
        "issue_title": value.issue_title,
        "issue_body": value.issue_body,
        "instruction": "Rank at most 20 likely Python production files using only the issue.",
    }
    serialized = repr(context).casefold()
    assert "snapshot" not in serialized
    assert "gold" not in serialized
    assert "fix_pr" not in serialized


@pytest.mark.anyio
async def test_reposcope_analyzer_runs_static_graph_and_emits_validated_evidence(
    tmp_path: Path,
) -> None:
    snapshot = tmp_path / "snapshot"
    source = snapshot / "src" / "parser.py"
    source.parent.mkdir(parents=True)
    source.write_text(SOURCE, encoding="utf-8", newline="")
    case = _case(snapshot)
    gateway = _repo_gateway()
    analyzer = RealRepoScopeAnalyzer(
        model=gateway,
        configuration=DeepSeekRunConfig.approved(),
        ledger=InMemoryCallLedger(max_total_tokens=2_500_000),
        github=_GithubFake(),
        clock=lambda: datetime(2026, 9, 2, tzinfo=UTC),
    )

    prediction = await analyzer(case, snapshot)

    assert prediction.predicted_files[0] == "src/parser.py"
    assert prediction.citations == (_citation(),)
    assert prediction.report_outcome == "root_cause_identified"
    assert prediction.usage.tool_calls == 1
    assert prediction.usage.model_attempts == 5
    assert prediction.model_id is None
    for contexts in gateway.contexts.values():
        serialized = repr(contexts).casefold()
        assert "gold_files" not in serialized
        assert "fix_pr" not in serialized


@pytest.mark.anyio
async def test_reposcope_analyzer_does_not_score_model_fallback_as_success(
    tmp_path: Path,
) -> None:
    snapshot = tmp_path / "snapshot"
    source = snapshot / "src" / "parser.py"
    source.parent.mkdir(parents=True)
    source.write_text(SOURCE, encoding="utf-8", newline="")
    case = _case(snapshot)
    gateway = _repo_gateway()
    gateway.responses[ModelPhase.REPORT_COMPOSITION] = deque(
        (PermanentModelError("provider failed"),)
    )
    analyzer = RealRepoScopeAnalyzer(
        model=gateway,
        configuration=DeepSeekRunConfig.approved(),
        ledger=InMemoryCallLedger(max_total_tokens=2_500_000),
        github=_GithubFake(),
    )

    with pytest.raises(RuntimeError, match="model failure"):
        await analyzer(case, snapshot)


@pytest.mark.anyio
@pytest.mark.parametrize("scenario", ["bound", "no_primary", "bad_excerpt", "bad_range", "valid_nonprimary", "critique_only", "model_insufficient"])
async def test_diagnostic_replay_distinguishes_report_loss_without_changing_prediction(
    tmp_path: Path, scenario: str,
) -> None:
    from app.evaluation.diagnostics import DiagnosticJournal, CaseDiagnostic
    from app.evaluation.jsonl import read_jsonl

    snapshot = tmp_path / "snapshot"
    source = snapshot / "src" / "parser.py"
    source.parent.mkdir(parents=True)
    source.write_text(SOURCE, encoding="utf-8", newline="")
    case = _case(snapshot)

    def gateway_for_scenario():
        gateway = _repo_gateway()
        report = gateway.responses[ModelPhase.REPORT_COMPOSITION][0]
        if scenario == "bound":
            report = report.model_copy(update={"evidence": ()})
        elif scenario == "no_primary":
            report = report.model_copy(update={"primary_hypothesis": None})
        elif scenario == "bad_excerpt":
            report = report.model_copy(update={"evidence": (_citation().model_copy(update={"excerpt": "UNTRUSTED_SECRET_CANARY"}),)})
        elif scenario in {"bad_range", "valid_nonprimary"}:
            primary = report.primary_hypothesis.model_copy(update={"evidence": (_citation().summary().model_copy(update={"start_line": 20, "end_line": 21}),)})
            report = report.model_copy(update={"evidence": report.evidence if scenario == "valid_nonprimary" else (), "primary_hypothesis": primary})
        elif scenario == "critique_only":
            gateway.responses[ModelPhase.TOOL_SELECTION] = deque([ToolRequest(complete=True)])
            report = report.model_copy(update={"evidence": ()})
        else:
            report = report.model_copy(update={"outcome": "insufficient_evidence", "primary_hypothesis": None, "evidence": ()})
        gateway.responses[ModelPhase.REPORT_COMPOSITION] = deque([report])
        return gateway

    common = dict(configuration=DeepSeekRunConfig.approved(), github=_GithubFake())
    baseline_gateway = gateway_for_scenario()
    baseline = await RealRepoScopeAnalyzer(model=baseline_gateway, ledger=InMemoryCallLedger(max_total_tokens=2_500_000), **common)(case, snapshot)
    journal = DiagnosticJournal(tmp_path / "diagnostics.v1.jsonl", dataset_digest="b" * 64)
    gateway = gateway_for_scenario()
    diagnosed = await RealRepoScopeAnalyzer(model=gateway, ledger=InMemoryCallLedger(max_total_tokens=2_500_000), diagnostics=journal, **common)(case, snapshot)
    assert diagnosed.model_dump(exclude={"usage"}) == baseline.model_dump(exclude={"usage"})
    assert diagnosed.usage.tool_calls == baseline.usage.tool_calls
    assert diagnosed.usage.model_attempts == baseline.usage.model_attempts
    assert gateway.contexts == baseline_gateway.contexts
    row, = read_jsonl(journal.path, CaseDiagnostic)
    assert row.status == "complete"
    assert [step.sequence for step in row.steps] == list(range(1, len(row.steps) + 1))
    assert row.steps[-1].node == "prepare_review"
    validated = next(step for step in row.steps if step.node == "validate_report")
    expected = {"bound": "supported_primary", "no_primary": "primary_absent", "bad_excerpt": "primary_unvalidated", "bad_range": "primary_unvalidated", "valid_nonprimary": "primary_unvalidated", "critique_only": "primary_unvalidated", "model_insufficient": "model_report_insufficient"}
    assert validated.report_reason == expected[scenario]
    if scenario == "bound":
        assert "report_evidence_bound" in validated.events
        assert len(validated.report_evidence_ids) == 1
    if scenario == "bad_excerpt":
        assert validated.rejection_codes == ("excerpt_mismatch",)
    if scenario == "valid_nonprimary":
        assert validated.valid_count == 1 and validated.report_evidence_ids == ()
        assert validated.evidence_after == 1  # valid evidence pool != retained report evidence
    if scenario == "critique_only":
        critique = next(step for step in row.steps if step.node == "critique_evidence")
        assert critique.evidence_before == 0 and critique.evidence_after == 1
        assert critique.hypothesis_tool_matches == 0 and validated.bound_count == 0
    assert "UNTRUSTED_SECRET_CANARY" not in journal.path.read_text(encoding="utf-8")
    # No diagnostic file or answer is ever fed back into the model context.
    assert "diagnostic" not in repr(gateway.contexts).lower()


@pytest.mark.anyio
async def test_diagnostics_survive_model_abort_and_io_failure_stops_calls(tmp_path: Path, monkeypatch) -> None:
    import app.evaluation.diagnostics as module
    from app.evaluation.jsonl import read_jsonl

    snapshot = tmp_path / "snapshot"
    source = snapshot / "src" / "parser.py"
    source.parent.mkdir(parents=True)
    source.write_text(SOURCE, encoding="utf-8", newline="")
    gateway = _repo_gateway()
    gateway.responses[ModelPhase.REPORT_COMPOSITION] = deque([PermanentModelError("SECRET_PROVIDER_ERROR")])
    journal = module.DiagnosticJournal(tmp_path / "failed.jsonl", dataset_digest="b" * 64)
    analyzer = RealRepoScopeAnalyzer(model=gateway, configuration=DeepSeekRunConfig.approved(), ledger=InMemoryCallLedger(max_total_tokens=2_500_000), github=_GithubFake(), diagnostics=journal)
    with pytest.raises(RuntimeError, match="model failure"):
        await analyzer(_case(snapshot), snapshot)
    row, = read_jsonl(journal.path, module.CaseDiagnostic)
    assert row.status == "aborted"
    assert any("model_failed" in step.events for step in row.steps)
    assert "SECRET_PROVIDER_ERROR" not in journal.path.read_text(encoding="utf-8")

    gateway = _repo_gateway()
    journal = module.DiagnosticJournal(tmp_path / "io-failed.jsonl", dataset_digest="b" * 64)
    analyzer = RealRepoScopeAnalyzer(model=gateway, configuration=DeepSeekRunConfig.approved(), ledger=InMemoryCallLedger(max_total_tokens=2_500_000), github=_GithubFake(), diagnostics=journal)
    def no_write(*args, **kwargs):
        raise OSError("private path canary")
    monkeypatch.setattr(module, "write_jsonl", no_write)
    with pytest.raises(module.DiagnosticWriteAbort):
        await analyzer(_case(snapshot), snapshot)
    assert not gateway.contexts


@pytest.mark.anyio
@pytest.mark.parametrize("scenario", ["unknown", "empty", "two_rounds", "tool_cap"])
async def test_diagnostics_capture_navigation_failure_and_budgets(tmp_path: Path, scenario: str) -> None:
    from app.evaluation.diagnostics import DiagnosticJournal, CaseDiagnostic
    from app.evaluation.jsonl import read_jsonl
    snapshot = tmp_path / "snapshot"
    source = snapshot / "src" / "parser.py"
    source.parent.mkdir(parents=True)
    source.write_text(SOURCE, encoding="utf-8", newline="")
    gateway = _repo_gateway()
    read = gateway.responses[ModelPhase.TOOL_SELECTION][0]
    if scenario == "unknown":
        gateway.responses[ModelPhase.TOOL_SELECTION].appendleft(ToolRequest(tool_name="SECRET_TOOL_CANARY", arguments={"secret": "SECRET_ARG_CANARY"}))
    elif scenario == "empty":
        gateway.responses[ModelPhase.TOOL_SELECTION].appendleft(ToolRequest(tool_name="search_code", arguments={"query": "NOT_FOUND_CANARY"}))
    elif scenario == "two_rounds":
        gateway.responses[ModelPhase.TOOL_SELECTION] = deque([read, ToolRequest(complete=True), read, ToolRequest(complete=True)])
        gateway.responses[ModelPhase.EVIDENCE_CRITIQUE].appendleft(CritiqueResult(sufficient=False, uncertainties=("SECRET_UNCERTAINTY_CANARY",)))
    else:
        gateway.responses[ModelPhase.TOOL_SELECTION] = deque([read] * 12)
    report = gateway.responses[ModelPhase.REPORT_COMPOSITION][0]
    gateway.responses[ModelPhase.REPORT_COMPOSITION][0] = report.model_copy(update={
        "issue_summary": "SECRET_SUMMARY_CANARY", "uncertainties": ("SECRET_REPORT_CANARY",),
        "primary_hypothesis": report.primary_hypothesis.model_copy(update={"statement": "SECRET_STATEMENT_CANARY"}),
    })
    journal = DiagnosticJournal(tmp_path / "diagnostics.jsonl", dataset_digest="b" * 64)
    await RealRepoScopeAnalyzer(model=gateway, configuration=DeepSeekRunConfig.approved(), ledger=InMemoryCallLedger(max_total_tokens=2_500_000), github=_GithubFake(), diagnostics=journal)(_case(snapshot), snapshot)
    row, = read_jsonl(journal.path, CaseDiagnostic)
    tools = [step for step in row.steps if step.node == "execute_tool"]
    if scenario == "unknown":
        assert tools[0].tool == "unknown" and tools[0].tool_succeeded is False
    elif scenario == "empty":
        assert tools[0].tool_succeeded is True and tools[0].tool_citation_count == 0
    elif scenario == "two_rounds":
        assert row.steps[-1].evidence_rounds == 2
        assert tools[-1].evidence_before == tools[-1].evidence_after == 1  # exact dedup
    else:
        assert len(tools) == row.steps[-1].tool_calls == 12
        assert "tool_budget_exhausted" in tools[-1].events
    assert "CANARY" not in journal.path.read_text(encoding="utf-8")


@pytest.mark.anyio
async def test_midrun_diagnostic_write_failure_preserves_prefix(tmp_path: Path, monkeypatch) -> None:
    import app.evaluation.diagnostics as module
    from app.evaluation.jsonl import read_jsonl
    snapshot = tmp_path / "snapshot"
    source = snapshot / "src" / "parser.py"
    source.parent.mkdir(parents=True)
    source.write_text(SOURCE, encoding="utf-8", newline="")
    journal = module.DiagnosticJournal(tmp_path / "diagnostics.jsonl", dataset_digest="b" * 64)
    real_write = module.write_jsonl
    def fail_third_step(path, rows):
        if rows and len(rows[0].steps) == 3:
            raise OSError("SECRET_DISK_PATH_CANARY")
        real_write(path, rows)
    monkeypatch.setattr(module, "write_jsonl", fail_third_step)
    gateway = _repo_gateway()
    with pytest.raises(module.DiagnosticWriteAbort):
        await RealRepoScopeAnalyzer(model=gateway, configuration=DeepSeekRunConfig.approved(), ledger=InMemoryCallLedger(max_total_tokens=2_500_000), github=_GithubFake(), diagnostics=journal)(_case(snapshot), snapshot)
    row, = read_jsonl(journal.path, module.CaseDiagnostic)
    assert row.status == "in_progress" and len(row.steps) == 2
    assert sum(len(values) for values in gateway.contexts.values()) == 2
    assert "CANARY" not in journal.path.read_text(encoding="utf-8")
