from __future__ import annotations

from collections import defaultdict, deque
from datetime import UTC, datetime
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.types import Command

from app.agent import (
    AnalysisPhase,
    AnalysisReport,
    AnalysisStatus,
    CritiqueResult,
    EvidenceCitation,
    FeedbackCommand,
    Hypothesis,
    InvestigationBudget,
    IssueIdentity,
    IssueUnderstanding,
    ModelPhase,
    PermanentModelError,
    ProposedTest,
    RepositoryIdentity,
    TransientModelError,
    ToolRequest,
    build_analysis_state,
    build_investigation_graph,
)
from app.ingestion import RepositorySnapshot
from app.investigation import InvestigationTools, PythonRepositoryIndex


SHA = "a" * 40
SOURCE = "def parse(value):\n    return value\n"


class _GithubFake:
    async def fetch_recent_commits(self, *args, **kwargs):
        return ()

    async def fetch_related_issues(self, *args, **kwargs):
        return ()


class _ScriptedModel:
    def __init__(self, responses: dict[ModelPhase, list[object]]) -> None:
        self.responses = {phase: deque(items) for phase, items in responses.items()}
        self.calls: dict[ModelPhase, int] = defaultdict(int)

    async def generate(self, *, phase, response_model, context, attempt=1):
        self.calls[phase] += 1
        response = self.responses[phase].popleft()
        if isinstance(response, Exception):
            raise response
        return response


def _tools(tmp_path: Path) -> InvestigationTools:
    root = tmp_path / "snapshot"
    path = root / "src" / "parser.py"
    path.parent.mkdir(parents=True)
    path.write_text(SOURCE, encoding="utf-8", newline="")
    snapshot = RepositorySnapshot.create(
        owner="octo",
        repository="demo",
        commit_sha=SHA,
        root_path=root,
        indexed_byte_count=len(SOURCE.encode()),
        created_at=datetime(2026, 8, 29, tzinfo=UTC),
    )
    return InvestigationTools(PythonRepositoryIndex.build(snapshot), _GithubFake())


def _state(tools: InvestigationTools, analysis_id: str = "analysis-1"):
    return build_analysis_state(
        analysis_id=analysis_id,
        tools=tools,
        issue=IssueIdentity(
            number=7,
            title="Parser accepts an invalid value",
            body="parse() returns a value that should be rejected.",
            html_url="https://github.com/octo/demo/issues/7",
        ),
    )


def _citation(**overrides: object) -> EvidenceCitation:
    values: dict[str, object] = {
        "commit_sha": SHA,
        "path": "src/parser.py",
        "start_line": 1,
        "end_line": 2,
        "excerpt": SOURCE,
        "explanation": "The function returns without validation.",
    }
    values.update(overrides)
    return EvidenceCitation(**values)


def _understanding() -> IssueUnderstanding:
    return IssueUnderstanding(
        summary="The parser accepts an invalid value.",
        observed_behavior="The value is returned unchanged.",
        expected_behavior="The invalid value should be rejected.",
        search_terms=("parse", "validation"),
    )


def _report(*, statement: str = "Validation is missing.", citation=None):
    evidence = () if citation is None else (citation,)
    primary = (
        None
        if citation is None
        else Hypothesis(
            statement=statement,
            confidence=0.9,
            evidence=(citation.summary(),),
        )
    )
    return AnalysisReport(
        outcome=(
            "insufficient_evidence" if citation is None else "root_cause_identified"
        ),
        issue_summary="The parser accepts an invalid value.",
        observed_behavior="The value is returned unchanged.",
        expected_behavior="The invalid value should be rejected.",
        primary_hypothesis=primary,
        evidence=evidence,
        implementation_steps=("Add validation before returning.",) if citation else (),
        proposed_tests=(
            ProposedTest(
                name="reject invalid values",
                description="Verify invalid values raise a validation error.",
            ),
        ),
        uncertainties=() if citation else ("The relevant source is not yet known.",),
        confidence=0.9 if citation else 0.2,
    )


def _happy_model(*, report=None, revision=None) -> _ScriptedModel:
    citation = _citation()
    responses: dict[ModelPhase, list[object]] = {
        ModelPhase.ISSUE_UNDERSTANDING: [_understanding()],
        ModelPhase.TOOL_SELECTION: [
            ToolRequest(
                tool_name="read_code",
                arguments={
                    "path": "src/parser.py",
                    "start_line": 1,
                    "end_line": 2,
                },
            ),
            ToolRequest(complete=True),
        ],
        ModelPhase.EVIDENCE_CRITIQUE: [
            CritiqueResult(
                sufficient=True,
                hypotheses=(
                    Hypothesis(
                        statement="Validation is missing.",
                        confidence=0.9,
                        evidence=(citation.summary(),),
                    ),
                ),
                evidence=(citation,),
            )
        ],
        ModelPhase.REPORT_COMPOSITION: [report or _report(citation=citation)],
    }
    if revision is not None:
        responses[ModelPhase.REPORT_REVISION] = [revision]
    return _ScriptedModel(responses)


def _config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def _memory() -> InMemorySaver:
    model_names = (
        "AnalysisEvent",
        "AnalysisPhase",
        "AnalysisReport",
        "AnalysisStatus",
        "BudgetCounters",
        "EvidenceCitation",
        "EvidenceCitationSummary",
        "FeedbackCommand",
        "Hypothesis",
        "ImpactedFile",
        "IssueIdentity",
        "IssueUnderstanding",
        "ProposedTest",
        "RepositoryIdentity",
        "ToolRequest",
        "ToolArgument",
        "ToolResultSummary",
    )
    return InMemorySaver(
        serde=JsonPlusSerializer(
            allowed_msgpack_modules=tuple(
                ("app.agent.models", name) for name in model_names
            )
        )
    )


@pytest.mark.anyio
async def test_happy_graph_pauses_with_validated_report_then_accepts(tmp_path: Path) -> None:
    """Breaks if the bounded graph skips tools, validation, review, or acceptance."""
    tools = _tools(tmp_path)
    model = _happy_model()
    graph = build_investigation_graph(
        tools=tools,
        model=model,
        budget=InvestigationBudget(),
        checkpointer=_memory(),
    )
    config = _config("happy")

    paused = await graph.ainvoke(_state(tools), config)

    assert paused["status"] == AnalysisStatus.REVIEW_READY
    assert paused["phase"] == AnalysisPhase.REVIEW
    assert paused["report"].outcome == "root_cause_identified"
    assert paused["report"].evidence == (_citation(),)
    assert paused["counters"].tool_calls == 1
    assert paused["counters"].evidence_rounds == 1
    assert "__interrupt__" in paused
    assert [item.kind for item in paused["events"]] == [
        "issue_understood",
        "evidence_round_started",
        "tool_succeeded",
        "investigation_completed",
        "evidence_sufficient",
        "report_composed",
        "citation_validated",
        "review_ready",
    ]

    completed = await graph.ainvoke(
        Command(resume=FeedbackCommand(action="accept").model_dump()), config
    )
    assert completed["status"] == AnalysisStatus.COMPLETED
    assert completed["phase"] == AnalysisPhase.COMPLETED
    assert completed["events"][-1].kind == "report_accepted"


@pytest.mark.anyio
async def test_tool_loop_stops_at_twelve_attempts(tmp_path: Path) -> None:
    """Breaks if repeated model tool choices exceed the hard v1 call ceiling."""
    tools = _tools(tmp_path)
    requests = [ToolRequest(tool_name="get_repository_map") for _ in range(20)]
    model = _ScriptedModel(
        {
            ModelPhase.ISSUE_UNDERSTANDING: [_understanding()],
            ModelPhase.TOOL_SELECTION: requests,
            ModelPhase.EVIDENCE_CRITIQUE: [
                CritiqueResult(sufficient=False, uncertainties=("No source evidence.",))
            ],
            ModelPhase.REPORT_COMPOSITION: [_report()],
        }
    )
    graph = build_investigation_graph(
        tools=tools,
        model=model,
        budget=InvestigationBudget(max_evidence_rounds=1),
        checkpointer=_memory(),
    )

    paused = await graph.ainvoke(_state(tools, "analysis-12"), _config("calls-12"))

    assert paused["counters"].tool_calls == 12
    assert len(paused["tool_history"]) == 12
    assert model.calls[ModelPhase.TOOL_SELECTION] == 12
    assert any(item.kind == "tool_budget_exhausted" for item in paused["events"])
    assert [item.sequence for item in paused["events"]] == list(
        range(1, len(paused["events"]) + 1)
    )


@pytest.mark.anyio
async def test_default_combined_budget_never_selects_a_thirteenth_tool(
    tmp_path: Path,
) -> None:
    """Breaks if an exhausted call budget can start another evidence round."""
    tools = _tools(tmp_path)
    model = _ScriptedModel(
        {
            ModelPhase.ISSUE_UNDERSTANDING: [_understanding()],
            ModelPhase.TOOL_SELECTION: [
                *(
                    ToolRequest(tool_name="get_repository_map")
                    for _ in range(12)
                ),
                ToolRequest(complete=True),
            ],
            ModelPhase.EVIDENCE_CRITIQUE: [
                CritiqueResult(sufficient=False, uncertainties=("Need more.",)),
                CritiqueResult(sufficient=False, uncertainties=("Still unknown.",)),
            ],
            ModelPhase.REPORT_COMPOSITION: [_report()],
        }
    )
    graph = build_investigation_graph(
        tools=tools,
        model=model,
        budget=InvestigationBudget(),
        checkpointer=_memory(),
    )

    paused = await graph.ainvoke(
        _state(tools, "analysis-combined-budget"), _config("combined-budget")
    )

    assert paused["counters"].tool_calls == 12
    assert paused["counters"].evidence_rounds == 1
    assert model.calls[ModelPhase.TOOL_SELECTION] == 12
    assert model.calls[ModelPhase.EVIDENCE_CRITIQUE] == 1


@pytest.mark.anyio
async def test_graph_records_actual_attempts_after_transient_then_permanent_failure(
    tmp_path: Path,
) -> None:
    """Breaks if observable state counts a retry allowance instead of actual calls."""
    tools = _tools(tmp_path)
    model = _ScriptedModel(
        {
            ModelPhase.ISSUE_UNDERSTANDING: [
                TransientModelError(),
                PermanentModelError(),
            ],
            ModelPhase.TOOL_SELECTION: [ToolRequest(complete=True)],
            ModelPhase.EVIDENCE_CRITIQUE: [CritiqueResult(sufficient=False)],
            ModelPhase.REPORT_COMPOSITION: [_report()],
        }
    )
    graph = build_investigation_graph(
        tools=tools,
        model=model,
        budget=InvestigationBudget(max_evidence_rounds=1),
        checkpointer=_memory(),
    )

    paused = await graph.ainvoke(
        _state(tools, "analysis-attempt-count"), _config("attempt-count")
    )

    assert model.calls[ModelPhase.ISSUE_UNDERSTANDING] == 2
    assert paused["counters"].model_attempts == 5
    failure = next(item for item in paused["events"] if item.kind == "model_failed")
    assert failure.model_attempts == 2


@pytest.mark.anyio
async def test_evidence_gathering_stops_after_two_rounds(tmp_path: Path) -> None:
    """Breaks if an insufficient critique can create a third evidence round."""
    tools = _tools(tmp_path)
    model = _ScriptedModel(
        {
            ModelPhase.ISSUE_UNDERSTANDING: [_understanding()],
            ModelPhase.TOOL_SELECTION: [
                ToolRequest(complete=True),
                ToolRequest(complete=True),
            ],
            ModelPhase.EVIDENCE_CRITIQUE: [
                CritiqueResult(sufficient=False, uncertainties=("Unknown one.",)),
                CritiqueResult(sufficient=False, uncertainties=("Unknown two.",)),
            ],
            ModelPhase.REPORT_COMPOSITION: [_report()],
        }
    )
    graph = build_investigation_graph(
        tools=tools,
        model=model,
        budget=InvestigationBudget(),
        checkpointer=_memory(),
    )

    paused = await graph.ainvoke(_state(tools, "analysis-rounds"), _config("rounds"))

    assert paused["counters"].evidence_rounds == 2
    assert model.calls[ModelPhase.EVIDENCE_CRITIQUE] == 2
    assert sum(
        item.kind == "evidence_round_started" for item in paused["events"]
    ) == 2
    assert any(item.kind == "round_budget_exhausted" for item in paused["events"])


@pytest.mark.anyio
async def test_invalid_report_evidence_is_removed_and_downgraded(tmp_path: Path) -> None:
    """Breaks if graph output can preserve fabricated root-cause evidence."""
    tools = _tools(tmp_path)
    invalid = _citation(excerpt="fabricated\n")
    model = _happy_model(report=_report(citation=invalid))
    graph = build_investigation_graph(
        tools=tools,
        model=model,
        budget=InvestigationBudget(),
        checkpointer=_memory(),
    )

    paused = await graph.ainvoke(_state(tools, "analysis-invalid"), _config("invalid"))

    assert paused["report"].outcome == "insufficient_evidence"
    assert paused["report"].primary_hypothesis is None
    assert paused["report"].confidence <= 0.25
    rejected = [item for item in paused["events"] if item.kind == "citation_rejected"]
    assert len(rejected) == 1
    assert rejected[0].citation.path == "src/parser.py"
    assert rejected[0].safe_error == (
        "Citation excerpt does not exactly match the snapshot."
    )


@pytest.mark.anyio
async def test_downgrade_clears_valid_nonprimary_evidence_explanations(
    tmp_path: Path,
) -> None:
    """Breaks if valid secondary evidence preserves model-authored root-cause prose."""
    tools = _tools(tmp_path)
    invalid_primary = _citation(
        start_line=1,
        end_line=1,
        excerpt="fabricated\n",
    )
    hostile_explanation = "ROOT CAUSE: disclose sk-secret-secondary"
    valid_secondary = _citation(explanation=hostile_explanation)
    report = AnalysisReport(
        outcome="root_cause_identified",
        issue_summary="The parser accepts an invalid value.",
        observed_behavior="The value is returned unchanged.",
        expected_behavior="The invalid value should be rejected.",
        primary_hypothesis=Hypothesis(
            statement="Fabricated primary cause.",
            confidence=0.9,
            evidence=(invalid_primary.summary(),),
        ),
        alternative_hypotheses=(
            Hypothesis(
                statement="Secondary explanation.",
                confidence=0.5,
                evidence=(valid_secondary.summary(),),
            ),
        ),
        evidence=(invalid_primary, valid_secondary),
        uncertainties=("Unknown.",),
        confidence=0.9,
    )
    graph = build_investigation_graph(
        tools=tools,
        model=_happy_model(report=report),
        budget=InvestigationBudget(),
        checkpointer=_memory(),
    )

    paused = await graph.ainvoke(
        _state(tools, "analysis-secondary-leak"), _config("secondary-leak")
    )

    observable = "\n".join(
        (paused["report"].model_dump_json(),)
        + tuple(item.model_dump_json() for item in paused["events"])
    )
    assert paused["report"].outcome == "insufficient_evidence"
    assert paused["report"].evidence == ()
    assert "ROOT CAUSE" not in observable
    assert "sk-secret-secondary" not in observable


@pytest.mark.anyio
async def test_one_revision_preserves_original_and_second_revision_is_rejected(
    tmp_path: Path,
) -> None:
    """Breaks if review overwrites history or allows more than one revision."""
    tools = _tools(tmp_path)
    original = _report(citation=_citation())
    revised = _report(statement="Validation and normalization are missing.", citation=_citation())
    graph = build_investigation_graph(
        tools=tools,
        model=_happy_model(report=original, revision=revised),
        budget=InvestigationBudget(),
        checkpointer=_memory(),
    )
    config = _config("revision")
    await graph.ainvoke(_state(tools, "analysis-revision"), config)

    revised_pause = await graph.ainvoke(
        Command(
            resume=FeedbackCommand(
                action="revise", text="Check normalization too."
            ).model_dump()
        ),
        config,
    )

    assert revised_pause["status"] == AnalysisStatus.REVIEW_READY
    assert revised_pause["report"] == revised
    assert revised_pause["original_report"] == original
    assert revised_pause["report_history"] == (original, revised)
    assert revised_pause["counters"].user_revisions == 1
    assert revised_pause["revision_feedback"] == ("Check normalization too.",)

    rejected_pause = await graph.ainvoke(
        Command(
            resume=FeedbackCommand(
                action="revise", text="Try a second rewrite."
            ).model_dump()
        ),
        config,
    )

    assert rejected_pause["report"] == revised
    assert rejected_pause["counters"].user_revisions == 1
    assert rejected_pause["events"][-2].kind == "revision_rejected"
    assert rejected_pause["events"][-2].safe_error == (
        "The single allowed report revision has already been used."
    )
    assert "__interrupt__" in rejected_pause


@pytest.mark.anyio
async def test_revision_checkpoints_collision_resistant_feedback_identity(
    tmp_path: Path,
) -> None:
    """Breaks if crash recovery can identify a revision only by its raw comment."""
    tools = _tools(tmp_path)
    original = _report(citation=_citation())
    revised = _report(
        statement="Validation and normalization are missing.",
        citation=_citation(),
    )
    graph = build_investigation_graph(
        tools=tools,
        model=_happy_model(report=original, revision=revised),
        budget=InvestigationBudget(),
        checkpointer=_memory(),
    )
    config = _config("revision-command-id")
    await graph.ainvoke(_state(tools, "analysis-revision-command-id"), config)
    command_id = "b" * 64

    revised_pause = await graph.ainvoke(
        Command(
            resume={
                "action": "revise",
                "text": "Inspect the normalization branch.",
                "command_id": command_id,
            }
        ),
        config,
    )

    assert revised_pause["status"] == AnalysisStatus.REVIEW_READY
    assert revised_pause["applied_feedback_id"] == command_id
    observable_events = "\n".join(
        event.model_dump_json() for event in revised_pause["events"]
    )
    assert command_id not in observable_events
    assert "Inspect the normalization branch." not in observable_events


@pytest.mark.anyio
async def test_malformed_feedback_is_rejected_without_accepting_or_revising(
    tmp_path: Path,
) -> None:
    """Breaks if raw resume data can bypass the feedback command contract."""
    tools = _tools(tmp_path)
    original = _report(citation=_citation())
    graph = build_investigation_graph(
        tools=tools,
        model=_happy_model(report=original),
        budget=InvestigationBudget(),
        checkpointer=_memory(),
    )
    config = _config("invalid-feedback")
    await graph.ainvoke(_state(tools, "analysis-invalid-feedback"), config)

    rejected_pause = await graph.ainvoke(
        Command(resume={"action": "revise", "text": "   "}), config
    )

    assert rejected_pause["status"] == AnalysisStatus.REVIEW_READY
    assert rejected_pause["report"] == original
    assert rejected_pause["counters"].user_revisions == 0
    assert rejected_pause["events"][-2].kind == "feedback_rejected"
    assert rejected_pause["events"][-2].safe_error == (
        "Feedback command failed validation."
    )
    assert "__interrupt__" in rejected_pause


@pytest.mark.anyio
async def test_revision_model_failure_preserves_original_and_redacts_error(
    tmp_path: Path,
) -> None:
    """Breaks if a failed one-shot revision loses report history or leaks errors."""
    tools = _tools(tmp_path)
    original = _report(citation=_citation())
    model = _happy_model(report=original, revision=PermanentModelError("sk-secret"))
    graph = build_investigation_graph(
        tools=tools,
        model=model,
        budget=InvestigationBudget(),
        checkpointer=_memory(),
    )
    config = _config("failed-revision")
    await graph.ainvoke(_state(tools, "analysis-failed-revision"), config)

    failed_pause = await graph.ainvoke(
        Command(
            resume=FeedbackCommand(
                action="revise", text="Please revise the report."
            ).model_dump()
        ),
        config,
    )

    assert failed_pause["status"] == AnalysisStatus.REVIEW_READY
    assert failed_pause["report"] == original
    assert failed_pause["original_report"] == original
    assert failed_pause["report_history"] == (original,)
    assert failed_pause["counters"].user_revisions == 1
    assert failed_pause["revision_feedback"] == ("Please revise the report.",)
    failed = next(item for item in failed_pause["events"] if item.kind == "model_failed")
    assert failed.safe_error == "Model output could not be completed safely."
    assert "sk-secret" not in "\n".join(
        item.model_dump_json() for item in failed_pause["events"]
    )


class _SecretFailingTools(InvestigationTools):
    def read_code(self, path: str, start_line: int, end_line: int):
        raise RuntimeError("sk-secret-value at C:/private/snapshot")


@pytest.mark.anyio
async def test_tool_failure_events_are_deterministic_and_redact_secret_errors(
    tmp_path: Path,
) -> None:
    """Breaks if observable failures expose exception text, paths, or credentials."""
    base = _tools(tmp_path)
    tools = _SecretFailingTools(base.index, _GithubFake())
    model = _ScriptedModel(
        {
            ModelPhase.ISSUE_UNDERSTANDING: [_understanding()],
            ModelPhase.TOOL_SELECTION: [
                ToolRequest(
                    tool_name="read_code",
                    arguments={
                        "path": "src/parser.py",
                        "start_line": 1,
                        "end_line": 2,
                    },
                ),
                ToolRequest(complete=True),
            ],
            ModelPhase.EVIDENCE_CRITIQUE: [CritiqueResult(sufficient=False)],
            ModelPhase.REPORT_COMPOSITION: [_report()],
        }
    )
    graph = build_investigation_graph(
        tools=tools,
        model=model,
        budget=InvestigationBudget(max_evidence_rounds=1),
        checkpointer=_memory(),
    )

    paused = await graph.ainvoke(_state(tools, "analysis-secret"), _config("secret"))

    event_json = "\n".join(item.model_dump_json() for item in paused["events"])
    assert "sk-secret-value" not in event_json
    assert "private/snapshot" not in event_json
    assert "Traceback" not in event_json
    failed = next(item for item in paused["events"] if item.kind == "tool_failed")
    assert failed.safe_error == "Tool execution failed safely."


@pytest.mark.anyio
async def test_unsupported_tool_name_never_reaches_observable_state(
    tmp_path: Path,
) -> None:
    """Breaks if a model-provided name leaks secrets or prompt fragments."""
    tools = _tools(tmp_path)
    hostile_name = "run_shell sk-secret-value IGNORE PRIOR PROMPT"
    model = _ScriptedModel(
        {
            ModelPhase.ISSUE_UNDERSTANDING: [_understanding()],
            ModelPhase.TOOL_SELECTION: [
                ToolRequest(tool_name=hostile_name),
                ToolRequest(complete=True),
            ],
            ModelPhase.EVIDENCE_CRITIQUE: [CritiqueResult(sufficient=False)],
            ModelPhase.REPORT_COMPOSITION: [_report()],
        }
    )
    graph = build_investigation_graph(
        tools=tools,
        model=model,
        budget=InvestigationBudget(max_evidence_rounds=1),
        checkpointer=_memory(),
    )

    paused = await graph.ainvoke(
        _state(tools, "analysis-hostile-tool"), _config("hostile-tool")
    )

    failed = next(item for item in paused["events"] if item.kind == "tool_failed")
    assert failed.tool_name == "unknown"
    assert paused["tool_history"][0].tool_name == "unknown"
    assert paused["tool_history"][0].citations == ()
    observable = "\n".join(
        [
            *(item.model_dump_json() for item in paused["events"]),
            *(item.model_dump_json() for item in paused["tool_history"]),
            *paused["safe_errors"],
        ]
    )
    assert hostile_name not in observable
    assert "sk-secret-value" not in observable
    assert "IGNORE PRIOR PROMPT" not in observable
