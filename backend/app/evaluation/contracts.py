from __future__ import annotations

from decimal import Decimal
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.agent import EvidenceCitation
from app.ingestion import RepositoryCoordinates, validate_issue_number
from app.investigation.index import _normalize_relative_path


class EvaluationModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


EVALUATION_JSONL_ROW_MAX_BYTES = 1_000_000


class BenchmarkSlot(EvaluationModel):
    schema_version: Literal["reposcope.eval.slot.v1"]
    slot_id: str = Field(pattern=r"^(dev|hidden)-0[1-6]$")
    split: Literal["development", "hidden"]
    state: Literal["unfilled"] = "unfilled"

    @model_validator(mode="after")
    def _matching_split(self) -> BenchmarkSlot:
        expected = "development" if self.slot_id.startswith("dev-") else "hidden"
        if self.split != expected:
            raise ValueError("slot prefix and split must agree")
        return self


class LockedBenchmarkSlot(EvaluationModel):
    schema_version: Literal["reposcope.eval.slot.v2"]
    slot_id: str = Field(pattern=r"^(dev|hidden)-0[1-6]$")
    split: Literal["development", "hidden"]
    state: Literal["locked"] = "locked"
    case_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9-]*$")
    split_key_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _matching_split(self) -> LockedBenchmarkSlot:
        expected = "development" if self.slot_id.startswith("dev-") else "hidden"
        if self.split != expected:
            raise ValueError("slot prefix and split must agree")
        return self


class BenchmarkCase(EvaluationModel):
    schema_version: Literal["reposcope.eval.case.v1"]
    case_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9-]*$")
    split: Literal["development", "hidden"]
    repo_url: str
    issue_number: int = Field(strict=True)
    issue_title: str = Field(min_length=1, max_length=10_000)
    issue_body: str | None = Field(default=None, max_length=100_000)
    pre_fix_commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    snapshot_tree_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("repo_url")
    @classmethod
    def _repository_url(cls, value: str) -> str:
        return RepositoryCoordinates.parse(value).canonical_url

    @field_validator("issue_number")
    @classmethod
    def _issue_number(cls, value: int) -> int:
        return validate_issue_number(value)


class CurationCandidate(EvaluationModel):
    schema_version: Literal["reposcope.eval.candidate.v1"]
    candidate_id: str = Field(
        min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9-]*$"
    )
    state: Literal["qualified_pending_dataset_lock"]
    repo_url: str
    issue_number: int = Field(strict=True)
    issue_title: str = Field(min_length=1, max_length=10_000)
    issue_body: str | None = Field(default=None, max_length=100_000)
    pre_fix_commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    snapshot_tree_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("repo_url")
    @classmethod
    def _repository_url(cls, value: str) -> str:
        return RepositoryCoordinates.parse(value).canonical_url

    @field_validator("issue_number")
    @classmethod
    def _issue_number(cls, value: int) -> int:
        return validate_issue_number(value)


class BenchmarkGold(EvaluationModel):
    schema_version: Literal["reposcope.eval.gold.v1"]
    case_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9-]*$")
    gold_files: tuple[str, ...] = Field(min_length=1, max_length=5)

    @field_validator("gold_files")
    @classmethod
    def _gold_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(_normalize_relative_path(item) for item in value)
        if normalized != value or len(set(normalized)) != len(normalized):
            raise ValueError("gold files must be normalized and unique")
        if any(not item.casefold().endswith((".py", ".pyi")) for item in normalized):
            raise ValueError("gold files must be Python production source paths")
        for item in normalized:
            path = PurePosixPath(item)
            folded_parts = {part.casefold() for part in path.parts}
            if folded_parts.intersection({"test", "tests", "vendor", "vendors", "third_party"}) or (
                path.name.casefold().startswith("test_")
                or path.stem.casefold().endswith("_test")
            ):
                raise ValueError("gold files must exclude tests and vendored source")
        return normalized


class EvaluationUsage(EvaluationModel):
    latency_ms: int | None = Field(default=None, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    estimated_cost_usd: Decimal | None = Field(default=None, ge=0)
    tool_calls: int | None = Field(default=None, ge=0, le=12)
    model_attempts: int | None = Field(default=None, ge=0)
    rate_card_version: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def _cost_has_rate_card(self) -> EvaluationUsage:
        if self.estimated_cost_usd is not None and (
            self.rate_card_version is None
            or self.input_tokens is None
            or self.output_tokens is None
        ):
            raise ValueError("estimated cost requires token usage and a versioned rate card")
        return self


class EvaluationPrediction(EvaluationModel):
    predicted_files: tuple[str, ...] = Field(default=(), max_length=20)
    citations: tuple[EvidenceCitation, ...] = Field(default=(), max_length=64)
    report_outcome: Literal["root_cause_identified", "insufficient_evidence"] | None = None
    model_id: str | None = Field(default=None, max_length=200)
    usage: EvaluationUsage = Field(default_factory=EvaluationUsage)

    @field_validator("predicted_files")
    @classmethod
    def _predicted_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(_normalize_relative_path(item) for item in value)
        if normalized != value or len(set(normalized)) != len(normalized):
            raise ValueError("predicted files must be normalized and unique")
        return normalized

    @field_validator("citations")
    @classmethod
    def _unique_citations(
        cls, value: tuple[EvidenceCitation, ...]
    ) -> tuple[EvidenceCitation, ...]:
        keys = {
            (item.commit_sha, item.path, item.start_line, item.end_line, item.excerpt)
            for item in value
        }
        if len(keys) != len(value):
            raise ValueError("citations must be unique")
        return value

    @model_validator(mode="after")
    def _bounded_jsonl_row(self) -> EvaluationPrediction:
        if len(self.model_dump_json().encode("utf-8")) > EVALUATION_JSONL_ROW_MAX_BYTES:
            raise ValueError("evaluation JSONL row is too large")
        return self


class ScriptedPrediction(EvaluationPrediction):
    schema_version: Literal["reposcope.eval.script.v1"]
    case_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9-]*$")
    system: Literal["issue_only", "reposcope"]


class BenchmarkResult(EvaluationPrediction):
    schema_version: Literal["reposcope.eval.result.v1"]
    case_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9-]*$")
    system: Literal["issue_only", "reposcope"]
    status: Literal["ok", "failed"]
    runner_id: str = Field(min_length=1, max_length=200)
    dataset_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    safe_error_code: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,99}$")

    @model_validator(mode="after")
    def _status_payload(self) -> BenchmarkResult:
        if self.status == "failed":
            if self.safe_error_code is None:
                raise ValueError("failed results require a safe error code")
            if self.predicted_files or self.citations or self.report_outcome is not None:
                raise ValueError("failed results cannot contain predictions")
        elif self.safe_error_code is not None:
            raise ValueError("successful results cannot contain an error code")
        return self


class SystemMetrics(EvaluationModel):
    system: Literal["issue_only", "reposcope"]
    case_count: int = Field(ge=1)
    successful_cases: int = Field(ge=0)
    file_recall_at_5: float = Field(ge=0, le=1)
    mrr: float = Field(ge=0, le=1)
    citations_emitted: int = Field(ge=0)
    citations_valid: int = Field(ge=0)
    hallucinated_citations: int = Field(ge=0)
    citation_validity: float | None = Field(default=None, ge=0, le=1)
    citation_hallucination_rate: float | None = Field(default=None, ge=0, le=1)
    median_latency_ms: float | None = Field(default=None, ge=0)
    total_input_tokens: int | None = Field(default=None, ge=0)
    total_output_tokens: int | None = Field(default=None, ge=0)
    estimated_cost_usd: Decimal | None = Field(default=None, ge=0)


class EvaluationSummary(EvaluationModel):
    schema_version: Literal["reposcope.eval.summary.v1"]
    case_count: int = Field(ge=1)
    systems: tuple[SystemMetrics, ...] = Field(min_length=1, max_length=2)
    file_recall_at_5_delta: float | None = None
    mrr_delta: float | None = None


class EvaluationSummaryV2(EvaluationModel):
    schema_version: Literal["reposcope.eval.summary.v2"]
    split: Literal["development", "hidden", "all"]
    dataset_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    case_count: int = Field(ge=1)
    systems: tuple[SystemMetrics, ...] = Field(min_length=1, max_length=2)
    file_recall_at_5_delta: float | None = None
    mrr_delta: float | None = None
    run_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    configuration_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    prediction_manifest_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    provider: str | None = Field(default=None, min_length=1, max_length=100)
    requested_model: str | None = Field(default=None, min_length=1, max_length=200)
    documented_model_version: str | None = Field(
        default=None, min_length=1, max_length=200
    )
    provider_backend_drift: bool | None = None
    preflight_usage: EvaluationUsage | None = None
    total_provider_usage: EvaluationUsage | None = None
