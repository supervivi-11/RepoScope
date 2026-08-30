from __future__ import annotations

from enum import StrEnum
from math import isfinite
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.investigation.index import _normalize_relative_path


type ToolArgumentValue = str | int | float | bool | None


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class AnalysisPhase(StrEnum):
    QUEUED = "queued"
    UNDERSTANDING = "understanding"
    INVESTIGATING = "investigating"
    CRITIQUING = "critiquing"
    COMPOSING = "composing"
    VALIDATING = "validating"
    REVIEW = "review"
    REVISING = "revising"
    COMPLETED = "completed"
    FAILED = "failed"


class AnalysisStatus(StrEnum):
    QUEUED = "QUEUED"
    INVESTIGATING = "INVESTIGATING"
    REVIEW_READY = "REVIEW_READY"
    REVISING = "REVISING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class InvestigationBudget(FrozenModel):
    max_tool_calls: int = Field(default=12, ge=1, le=12)
    max_evidence_rounds: int = Field(default=2, ge=1, le=2)
    model_retries: int = Field(default=2, ge=0, le=2)
    max_user_revisions: int = Field(default=1, ge=0, le=1)


def _nonblank(value: str, *, label: str, maximum: int = 10_000) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be nonblank")
    if len(value) > maximum:
        raise ValueError(f"{label} is too long")
    return value


def _normalized_path(value: str) -> str:
    _nonblank(value, label="path", maximum=1_000)
    return _normalize_relative_path(value)


class EvidenceCitation(FrozenModel):
    commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    excerpt: str
    explanation: str

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        return _normalized_path(value)

    @field_validator("explanation")
    @classmethod
    def _validate_explanation(cls, value: str) -> str:
        return _nonblank(value, label="explanation", maximum=4_000)

    @model_validator(mode="after")
    def _ordered_range(self) -> EvidenceCitation:
        if self.end_line < self.start_line:
            raise ValueError("end_line must not precede start_line")
        if self.end_line - self.start_line + 1 > 200:
            raise ValueError("citation cannot exceed 200 lines")
        return self

    def summary(self) -> EvidenceCitationSummary:
        return EvidenceCitationSummary(
            commit_sha=self.commit_sha,
            path=self.path,
            start_line=self.start_line,
            end_line=self.end_line,
        )


class EvidenceCitationSummary(FrozenModel):
    commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        return _normalized_path(value)


class Hypothesis(FrozenModel):
    statement: str
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: tuple[EvidenceCitation, ...] = Field(default=(), max_length=32)

    @field_validator("statement")
    @classmethod
    def _validate_statement(cls, value: str) -> str:
        return _nonblank(value, label="hypothesis", maximum=8_000)


class ImpactedFile(FrozenModel):
    path: str
    explanation: str

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        return _normalized_path(value)

    @field_validator("explanation")
    @classmethod
    def _validate_explanation(cls, value: str) -> str:
        return _nonblank(value, label="explanation", maximum=4_000)


class ProposedTest(FrozenModel):
    name: str
    description: str

    @field_validator("name", "description")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        return _nonblank(value, label="test text", maximum=4_000)


class AnalysisReport(FrozenModel):
    outcome: Literal["root_cause_identified", "insufficient_evidence"]
    issue_summary: str
    observed_behavior: str
    expected_behavior: str
    primary_hypothesis: Hypothesis | None = None
    alternative_hypotheses: tuple[Hypothesis, ...] = Field(default=(), max_length=8)
    evidence: tuple[EvidenceCitation, ...] = Field(default=(), max_length=64)
    impacted_files: tuple[ImpactedFile, ...] = Field(default=(), max_length=32)
    implementation_steps: tuple[str, ...] = Field(default=(), max_length=32)
    proposed_tests: tuple[ProposedTest, ...] = Field(default=(), max_length=32)
    uncertainties: tuple[str, ...] = Field(default=(), max_length=32)
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator(
        "issue_summary", "observed_behavior", "expected_behavior"
    )
    @classmethod
    def _validate_required_text(cls, value: str) -> str:
        return _nonblank(value, label="report text", maximum=10_000)

    @field_validator("implementation_steps", "uncertainties")
    @classmethod
    def _validate_string_tuple(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            _nonblank(item, label="report item", maximum=4_000)
        return value


class IssueUnderstanding(FrozenModel):
    summary: str
    observed_behavior: str
    expected_behavior: str
    search_terms: tuple[str, ...] = Field(default=(), max_length=16)
    uncertainties: tuple[str, ...] = Field(default=(), max_length=16)

    @field_validator("summary", "observed_behavior", "expected_behavior")
    @classmethod
    def _validate_required_text(cls, value: str) -> str:
        return _nonblank(value, label="issue understanding", maximum=8_000)

    @field_validator("search_terms", "uncertainties")
    @classmethod
    def _validate_items(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            _nonblank(item, label="issue item", maximum=500)
        return value


class ToolArgument(FrozenModel):
    key: str = Field(min_length=1, max_length=100)
    value: ToolArgumentValue

    @field_validator("key")
    @classmethod
    def _validate_key(cls, value: str) -> str:
        return _nonblank(value, label="tool argument key", maximum=100)

    @field_validator("value", mode="before")
    @classmethod
    def _validate_json_scalar(cls, value: Any) -> Any:
        if type(value) is float and not isfinite(value):
            raise ValueError("tool argument values must be JSON-safe")
        if value is None or type(value) in {str, int, float, bool}:
            return value
        raise ValueError("tool argument values must be JSON-safe scalars")


class ToolRequest(FrozenModel):
    tool_name: str | None = None
    arguments: tuple[ToolArgument, ...] = Field(default_factory=tuple)
    complete: bool = False

    @field_validator("arguments", mode="before")
    @classmethod
    def _normalize_arguments(cls, value: Any) -> Any:
        if isinstance(value, dict):
            if not all(type(key) is str for key in value):
                raise ValueError("tool argument keys must be strings")
            return tuple(
                {"key": key, "value": value[key]} for key in sorted(value)
            )
        if isinstance(value, (tuple, list)):
            return value
        raise ValueError("tool arguments must be a flat key/value collection")

    @model_validator(mode="after")
    def _valid_action(self) -> ToolRequest:
        if self.complete:
            if self.tool_name is not None or self.arguments:
                raise ValueError("completed requests cannot invoke a tool")
        elif self.tool_name is None:
            raise ValueError("a tool name is required")
        if len({item.key for item in self.arguments}) != len(self.arguments):
            raise ValueError("tool argument keys must be unique")
        return self

    def arguments_dict(self) -> dict[str, Any]:
        return {item.key: item.value for item in self.arguments}


class ToolResultSummary(FrozenModel):
    tool_name: str
    arguments_summary: str
    result_summary: str | None = None
    citations: tuple[EvidenceCitation, ...] = Field(default=(), max_length=32)
    succeeded: bool
    safe_error: str | None = None


class CritiqueResult(FrozenModel):
    sufficient: bool
    hypotheses: tuple[Hypothesis, ...] = Field(default=(), max_length=8)
    evidence: tuple[EvidenceCitation, ...] = Field(default=(), max_length=64)
    uncertainties: tuple[str, ...] = Field(default=(), max_length=32)


class FeedbackCommand(FrozenModel):
    action: Literal["accept", "revise"]
    text: str | None = None
    command_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _validate_feedback(self) -> FeedbackCommand:
        if self.action == "revise":
            if self.text is None:
                raise ValueError("revision text is required")
            _nonblank(self.text, label="revision text", maximum=4_000)
        elif self.text is not None:
            raise ValueError("accept feedback cannot include text")
        return self


class AnalysisEvent(FrozenModel):
    sequence: int = Field(ge=1)
    phase: AnalysisPhase
    status: AnalysisStatus
    kind: str
    tool_name: str | None = None
    citation: EvidenceCitationSummary | None = None
    safe_error: str | None = None
    tool_calls: int = Field(default=0, ge=0, le=12)
    evidence_rounds: int = Field(default=0, ge=0, le=2)
    model_attempts: int = Field(default=0, ge=0)

    @field_validator("kind")
    @classmethod
    def _validate_kind(cls, value: str) -> str:
        return _nonblank(value, label="event kind", maximum=100)


class RepositoryIdentity(FrozenModel):
    owner: str
    repository: str
    commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")


class IssueIdentity(FrozenModel):
    number: int = Field(ge=1)
    title: str
    body: str | None = None
    html_url: str


class BudgetCounters(FrozenModel):
    tool_calls: int = Field(default=0, ge=0, le=12)
    evidence_rounds: int = Field(default=0, ge=0, le=2)
    model_attempts: int = Field(default=0, ge=0)
    user_revisions: int = Field(default=0, ge=0, le=1)


class AnalysisState(FrozenModel):
    analysis_id: str
    repository: RepositoryIdentity
    issue: IssueIdentity
    phase: AnalysisPhase = AnalysisPhase.QUEUED
    status: AnalysisStatus = AnalysisStatus.QUEUED
    issue_understanding: IssueUnderstanding | None = None
    hypotheses: tuple[Hypothesis, ...] = Field(default=(), max_length=8)
    evidence: tuple[EvidenceCitation, ...] = Field(default=(), max_length=64)
    tool_history: tuple[ToolResultSummary, ...] = Field(default=(), max_length=12)
    report: AnalysisReport | None = None
    original_report: AnalysisReport | None = None
    report_history: tuple[AnalysisReport, ...] = Field(default=(), max_length=2)
    counters: BudgetCounters = Field(default_factory=BudgetCounters)
    safe_errors: tuple[str, ...] = Field(default=(), max_length=32)
    events: tuple[AnalysisEvent, ...] = Field(default=(), max_length=256)
    revision_feedback: tuple[str, ...] = Field(default=(), max_length=1)
    applied_feedback_id: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    pending_feedback: FeedbackCommand | None = None
    pending_tool_request: ToolRequest | None = None
    critique_sufficient: bool = False
    feedback_invalid: bool = False
