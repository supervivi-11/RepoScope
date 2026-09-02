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
)
from app.evaluation.real_contracts import DeepSeekRunConfig
from app.evaluation.runners import IssueOnlyInput
from app.evaluation.contracts import BenchmarkCase
from app.evaluation.snapshot import snapshot_tree_digest


SHA = "a" * 40
SOURCE = "def parse(value):\n    return value\n"


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
