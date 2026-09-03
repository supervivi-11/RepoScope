from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


REQUIRED_DEPENDENCIES = (
    "httpx",
    "langchain-openai",
    "langgraph",
    "openai",
    "pydantic",
    "pydantic-settings",
)


class RealEvaluationModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class DeepSeekRunConfig(RealEvaluationModel):
    schema_version: Literal["reposcope.eval.run-config.v1"]
    provider: Literal["deepseek_official"]
    base_url: Literal["https://api.deepseek.com"]
    requested_model: Literal["deepseek-v4-flash"]
    documented_model_version: Literal["DeepSeek-V4-Flash-0731"]
    api_surface: Literal["responses"]
    thinking: Literal["enabled"]
    reasoning_effort: Literal["low"]
    temperature: None = None
    max_output_tokens: Literal[16384]
    max_total_tokens: Literal[2_500_000]
    max_tool_calls: Literal[12]
    max_evidence_rounds: Literal[2]
    model_retries: Literal[2]
    request_timeout_seconds: Literal[120]
    split: Literal["development"]
    case_count: Literal[6]
    runner_version: Literal["deepseek-development-v1"]
    prompt_version: Literal["reposcope-eval-v1"]
    rate_card_version: Literal["deepseek-v4-2026-08-16-v1"]
    rate_card_source: Literal["https://api-docs.deepseek.com/quick_start/pricing/"]
    rate_card_retrieved_on: Literal["2026-09-01"]

    @classmethod
    def approved(cls) -> DeepSeekRunConfig:
        return cls(
            schema_version="reposcope.eval.run-config.v1",
            provider="deepseek_official",
            base_url="https://api.deepseek.com",
            requested_model="deepseek-v4-flash",
            documented_model_version="DeepSeek-V4-Flash-0731",
            api_surface="responses",
            thinking="enabled",
            reasoning_effort="low",
            max_output_tokens=16384,
            max_total_tokens=2_500_000,
            max_tool_calls=12,
            max_evidence_rounds=2,
            model_retries=2,
            request_timeout_seconds=120,
            split="development",
            case_count=6,
            runner_version="deepseek-development-v1",
            prompt_version="reposcope-eval-v1",
            rate_card_version="deepseek-v4-2026-08-16-v1",
            rate_card_source="https://api-docs.deepseek.com/quick_start/pricing/",
            rate_card_retrieved_on="2026-09-01",
        )


def configuration_digest(configuration: DeepSeekRunConfig) -> str:
    payload = json.dumps(
        configuration.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class ProviderCallUsage(RealEvaluationModel):
    schema_version: Literal["reposcope.eval.call-usage.v1"]
    call_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9-]*$")
    case_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9-]*$")
    system: Literal["issue_only", "reposcope", "preflight"]
    phase: str = Field(min_length=1, max_length=100, pattern=r"^[a-z][a-z0-9_]*$")
    attempt: int = Field(ge=1, le=3)
    requested_model: Literal["deepseek-v4-flash"]
    returned_model: str | None = Field(default=None, min_length=1, max_length=200)
    system_fingerprint: str | None = Field(default=None, max_length=500)
    started_at: datetime
    finished_at: datetime
    latency_ms: int = Field(ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)
    rate_period: Literal["peak", "off_peak"]
    cache_hit_usd_per_million: Decimal = Field(ge=0)
    cache_miss_usd_per_million: Decimal = Field(ge=0)
    output_usd_per_million: Decimal = Field(ge=0)
    estimated_cost_usd: Decimal | None = Field(default=None, ge=0)
    status: Literal["success", "failed"] = "success"
    safe_error_code: str | None = Field(
        default=None, pattern=r"^[a-z][a-z0-9_]{0,99}$"
    )

    @model_validator(mode="after")
    def _valid_timing_and_usage(self) -> ProviderCallUsage:
        if self.started_at.tzinfo is None or self.finished_at.tzinfo is None:
            raise ValueError("call timestamps must be timezone-aware")
        if self.finished_at < self.started_at:
            raise ValueError("call finish must not precede its start")
        if (self.input_tokens is None) != (self.output_tokens is None):
            raise ValueError("input and output token usage must be jointly present")
        if (
            self.cached_input_tokens is not None
            and self.input_tokens is not None
            and self.cached_input_tokens > self.input_tokens
        ):
            raise ValueError("cached input tokens cannot exceed input tokens")
        if self.estimated_cost_usd is not None and (
            self.input_tokens is None or self.cached_input_tokens is None
        ):
            raise ValueError("estimated cost requires complete provider token usage")
        if self.status == "failed" and self.safe_error_code is None:
            raise ValueError("failed calls require a safe error code")
        if self.status == "success" and self.safe_error_code is not None:
            raise ValueError("successful calls cannot have an error code")
        return self

    @property
    def uncached_input_tokens(self) -> int | None:
        if self.input_tokens is None or self.cached_input_tokens is None:
            return None
        return self.input_tokens - self.cached_input_tokens

    @property
    def total_tokens(self) -> int | None:
        if self.input_tokens is None or self.output_tokens is None:
            return None
        return self.input_tokens + self.output_tokens


class ArtifactDigest(RealEvaluationModel):
    path: str = Field(min_length=1, max_length=300)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("path")
    @classmethod
    def _safe_relative_path(cls, value: str) -> str:
        if "\\" in value:
            raise ValueError("artifact paths must use POSIX separators")
        path = PurePosixPath(value)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise ValueError("artifact paths must be normalized relative paths")
        return value


class DependencyVersion(RealEvaluationModel):
    name: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    version: str = Field(min_length=1, max_length=200)


class RunArtifactManifest(RealEvaluationModel):
    schema_version: Literal["reposcope.eval.manifest.v1"]
    completion_status: Literal["complete"]
    run_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    dataset_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    configuration_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    reposcope_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    working_tree_clean: Literal[True]
    python_version: str = Field(min_length=1, max_length=100)
    dependency_versions: tuple[DependencyVersion, ...] = Field(
        min_length=1, max_length=32
    )
    execution_order: tuple[str, ...] = Field(min_length=12, max_length=12)
    started_at: datetime
    finished_at: datetime
    systems: tuple[Literal["issue_only", "reposcope"], Literal["issue_only", "reposcope"]]
    case_count: Literal[6]
    artifacts: tuple[ArtifactDigest, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def _complete_unique_manifest(self) -> RunArtifactManifest:
        if self.systems != ("issue_only", "reposcope"):
            raise ValueError("manifest must contain both systems in fixed order")
        paths = tuple(item.path for item in self.artifacts)
        if len(paths) != len(set(paths)):
            raise ValueError("manifest artifact paths must be unique")
        dependency_names = tuple(item.name for item in self.dependency_versions)
        if dependency_names != REQUIRED_DEPENDENCIES:
            raise ValueError("manifest dependencies must match the fixed ordered set")
        if self.started_at.tzinfo is None or self.finished_at.tzinfo is None:
            raise ValueError("manifest timestamps must be timezone-aware")
        if self.finished_at < self.started_at:
            raise ValueError("manifest finish must not precede start")
        parsed_order: list[tuple[str, str]] = []
        for step in self.execution_order:
            try:
                case_id, system = step.rsplit(":", 1)
            except ValueError as exc:
                raise ValueError("manifest execution order is malformed") from exc
            if not case_id or system not in {"issue_only", "reposcope"}:
                raise ValueError("manifest execution order is malformed")
            parsed_order.append((case_id, system))
        if any(
            parsed_order[index : index + 2]
            != [(parsed_order[index][0], "issue_only"), (parsed_order[index][0], "reposcope")]
            for index in range(0, 12, 2)
        ) or len({case_id for case_id, _ in parsed_order}) != 6:
            raise ValueError("manifest must record six paired case executions")
        return self
