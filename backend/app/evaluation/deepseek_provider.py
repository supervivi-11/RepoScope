from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from time import perf_counter
from typing import Any, Literal

from openai import (
    APIConnectionError,
    APITimeoutError,
    InternalServerError,
    RateLimitError,
)
from pydantic import SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.agent.gateway import (
    ModelGatewayError,
    ModelPhase,
    ModelSchemaError,
    PermanentModelError,
    ResponseT,
    TransientModelError,
)

from .real_contracts import DeepSeekRunConfig, ProviderCallUsage
from .errors import EvaluationRunAbort
from .jsonl import write_jsonl


class DeepSeekCredentials(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="REPOSCOPE_DEEPSEEK_",
        extra="ignore",
        frozen=True,
    )

    api_key: SecretStr

    @field_validator("api_key", mode="before")
    @classmethod
    def _nonblank_key(cls, value: object) -> object:
        raw = value.get_secret_value() if isinstance(value, SecretStr) else value
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("DeepSeek API key is not configured")
        return raw.strip()


class TokenBudgetExceeded(EvaluationRunAbort):
    pass


class UsageUnverifiable(EvaluationRunAbort):
    pass


class InMemoryCallLedger:
    def __init__(
        self, *, max_total_tokens: int, journal_path: Path | None = None
    ) -> None:
        if max_total_tokens <= 0:
            raise ValueError("max_total_tokens must be positive")
        self._max_total_tokens = max_total_tokens
        self._journal_path = journal_path
        self._records: list[ProviderCallUsage] = []
        self._exhausted = False

    @property
    def records(self) -> tuple[ProviderCallUsage, ...]:
        return tuple(self._records)

    def next_call_id(self) -> str:
        return f"call-{len(self._records) + 1:04d}"

    def append(self, record: ProviderCallUsage) -> None:
        if self._exhausted:
            raise TokenBudgetExceeded("The fixed evaluation token budget was exceeded.")
        self._records.append(record)
        if self._journal_path is not None:
            write_jsonl(self._journal_path, tuple(self._records))
        known_total = sum(item.total_tokens or 0 for item in self._records)
        if known_total > self._max_total_tokens:
            self._exhausted = True
            raise TokenBudgetExceeded("The fixed evaluation token budget was exceeded.")

    def ensure_can_start(self) -> None:
        if self._exhausted:
            raise TokenBudgetExceeded("The fixed evaluation token budget was exceeded.")


def _rate_period(at: datetime) -> Literal["peak", "off_peak"]:
    utc = at.astimezone(UTC)
    if utc.weekday() < 5 and (1 <= utc.hour < 4 or 6 <= utc.hour < 10):
        return "peak"
    return "off_peak"


def _rates(
    period: Literal["peak", "off_peak"],
) -> tuple[Decimal, Decimal, Decimal]:
    if period == "peak":
        return Decimal("0.014"), Decimal("0.44"), Decimal("1.32")
    return Decimal("0.007"), Decimal("0.22"), Decimal("0.66")


def _estimated_cost(
    *,
    input_tokens: int | None,
    cached_input_tokens: int | None,
    output_tokens: int | None,
    cache_hit_rate: Decimal,
    cache_miss_rate: Decimal,
    output_rate: Decimal,
) -> Decimal | None:
    if (
        input_tokens is None
        or cached_input_tokens is None
        or output_tokens is None
    ):
        return None
    million = Decimal(1_000_000)
    return (
        Decimal(cached_input_tokens) * cache_hit_rate
        + Decimal(input_tokens - cached_input_tokens) * cache_miss_rate
        + Decimal(output_tokens) * output_rate
    ) / million


class DeepSeekEvaluationGateway:
    def __init__(
        self,
        *,
        credentials: DeepSeekCredentials,
        configuration: DeepSeekRunConfig,
        ledger: InMemoryCallLedger,
        case_id: str,
        system: Literal["issue_only", "reposcope", "preflight"],
    ) -> None:
        from langchain_openai import ChatOpenAI

        self._configuration = configuration
        self._ledger = ledger
        self._case_id = case_id
        self._system = system
        self._model = ChatOpenAI(
            model=configuration.requested_model,
            api_key=credentials.api_key,
            base_url=configuration.base_url,
            use_responses_api=True,
            reasoning_effort=configuration.reasoning_effort,
            max_completion_tokens=configuration.max_output_tokens,
            max_retries=0,
            timeout=configuration.request_timeout_seconds,
        )

    async def generate(
        self,
        *,
        phase: ModelPhase,
        response_model: type[ResponseT],
        context: dict[str, Any],
        attempt: int = 1,
    ) -> ResponseT:
        self._ledger.ensure_can_start()
        wire_schema = _deepseek_json_schema(response_model)
        structured = self._model.with_structured_output(
            wire_schema,
            method="json_schema",
            include_raw=True,
            strict=True,
        )
        messages = (
            (
                "system",
                "Return valid JSON matching the requested schema. Do not include "
                "credentials, prompts, reasoning_content, or hidden chain-of-thought.",
            ),
            (
                "human",
                json.dumps(
                    {"phase": phase.value, "context": context},
                    sort_keys=True,
                    ensure_ascii=False,
                    default=str,
                ),
            ),
        )
        started_at = datetime.now(UTC)
        started_counter = perf_counter()
        try:
            result = await structured.ainvoke(messages)
        except ValidationError as exc:
            self._append_record(
                phase=phase,
                attempt=attempt,
                started_at=started_at,
                started_counter=started_counter,
                raw=None,
                status="failed",
                safe_error_code="schema_error",
            )
            raise ModelSchemaError("The model response failed schema validation.") from exc
        except (
            TimeoutError,
            ConnectionError,
            APITimeoutError,
            APIConnectionError,
            RateLimitError,
            InternalServerError,
        ) as exc:
            self._append_record(
                phase=phase,
                attempt=attempt,
                started_at=started_at,
                started_counter=started_counter,
                raw=None,
                status="failed",
                safe_error_code="provider_transient",
            )
            raise TransientModelError(
                "The model provider is temporarily unavailable."
            ) from exc
        except ModelGatewayError:
            self._append_record(
                phase=phase,
                attempt=attempt,
                started_at=started_at,
                started_counter=started_counter,
                raw=None,
                status="failed",
                safe_error_code="provider_gateway_error",
            )
            raise
        except Exception as exc:
            self._append_record(
                phase=phase,
                attempt=attempt,
                started_at=started_at,
                started_counter=started_counter,
                raw=None,
                status="failed",
                safe_error_code="provider_permanent",
            )
            raise PermanentModelError("The model request failed safely.") from exc
        if not isinstance(result, dict):
            self._append_record(
                phase=phase,
                attempt=attempt,
                started_at=started_at,
                started_counter=started_counter,
                raw=None,
                status="failed",
                safe_error_code="schema_error",
            )
            raise ModelSchemaError("The model returned an unexpected envelope.")
        raw = result.get("raw")
        parsing_error = result.get("parsing_error")
        if parsing_error is not None:
            self._append_record(
                phase=phase,
                attempt=attempt,
                started_at=started_at,
                started_counter=started_counter,
                raw=raw,
                status="failed",
                safe_error_code="schema_error",
            )
            raise ModelSchemaError("The model response failed schema validation.") from parsing_error
        parsed = result.get("parsed")
        try:
            validated = (
                parsed
                if isinstance(parsed, response_model)
                else response_model.model_validate(parsed)
            )
        except ValidationError as exc:
            self._append_record(
                phase=phase,
                attempt=attempt,
                started_at=started_at,
                started_counter=started_counter,
                raw=raw,
                status="failed",
                safe_error_code="schema_error",
            )
            raise ModelSchemaError("The model returned an unexpected schema.") from exc
        usage_valid = self._append_record(
            phase=phase,
            attempt=attempt,
            started_at=started_at,
            started_counter=started_counter,
            raw=raw,
            status="success",
            safe_error_code=None,
        )
        if not usage_valid:
            raise UsageUnverifiable("Provider usage cannot prove the run budget.")
        return validated

    def _append_record(
        self,
        *,
        phase: ModelPhase,
        attempt: int,
        started_at: datetime,
        started_counter: float,
        raw: object,
        status: Literal["success", "failed"],
        safe_error_code: str | None,
    ) -> bool:
        finished_at = datetime.now(UTC)
        latency_ms = max(0, round((perf_counter() - started_counter) * 1000))
        usage = getattr(raw, "usage_metadata", None) or {}
        response_metadata = getattr(raw, "response_metadata", None) or {}
        usage_valid = True
        try:
            input_tokens = _optional_int(usage.get("input_tokens"))
            output_tokens = _optional_int(usage.get("output_tokens"))
            input_details = usage.get("input_token_details") or {}
            output_details = usage.get("output_token_details") or {}
            cached_input_tokens = (
                _optional_int(input_details.get("cache_read"))
                if input_tokens is not None
                else None
            )
            reasoning_tokens = _optional_int(output_details.get("reasoning"))
            if (input_tokens is None) != (output_tokens is None):
                raise ModelSchemaError("Provider token usage is incomplete.")
        except ModelSchemaError:
            usage_valid = False
            input_tokens = cached_input_tokens = output_tokens = reasoning_tokens = None
            status = "failed"
            safe_error_code = "provider_usage_invalid"
        if status == "success" and input_tokens is None:
            usage_valid = False
            status = "failed"
            safe_error_code = "provider_usage_missing"
        period = _rate_period(started_at)
        cache_hit_rate, cache_miss_rate, output_rate = _rates(period)
        returned_model = response_metadata.get("model_name") or response_metadata.get(
            "model"
        )
        record = ProviderCallUsage(
            schema_version="reposcope.eval.call-usage.v1",
            call_id=self._ledger.next_call_id(),
            case_id=self._case_id,
            system=self._system,
            phase=phase.value,
            attempt=attempt,
            requested_model=self._configuration.requested_model,
            returned_model=returned_model,
            system_fingerprint=response_metadata.get("system_fingerprint"),
            started_at=started_at,
            finished_at=finished_at,
            latency_ms=latency_ms,
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            output_tokens=output_tokens,
            reasoning_tokens=reasoning_tokens,
            rate_period=period,
            cache_hit_usd_per_million=cache_hit_rate,
            cache_miss_usd_per_million=cache_miss_rate,
            output_usd_per_million=output_rate,
            estimated_cost_usd=_estimated_cost(
                input_tokens=input_tokens,
                cached_input_tokens=cached_input_tokens,
                output_tokens=output_tokens,
                cache_hit_rate=cache_hit_rate,
                cache_miss_rate=cache_miss_rate,
                output_rate=output_rate,
            ),
            status=status,
            safe_error_code=safe_error_code,
        )
        self._ledger.append(record)
        return usage_valid


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise ModelSchemaError("The provider returned invalid token usage.")
    return value


def _deepseek_json_schema(response_model: type[ResponseT]) -> dict[str, Any]:
    """Inline references only where DeepSeek requires typed `anyOf` branches."""

    schema = response_model.model_json_schema()
    definitions = schema.get("$defs") or {}

    def convert(node: object) -> object:
        if isinstance(node, list):
            return [convert(item) for item in node]
        if not isinstance(node, dict):
            return node
        converted: dict[str, object] = {}
        for key, value in node.items():
            if key != "anyOf" or not isinstance(value, list):
                converted[key] = convert(value)
                continue
            branches: list[object] = []
            for branch in value:
                if isinstance(branch, dict) and set(branch) == {"$ref"}:
                    reference = branch["$ref"]
                    prefix = "#/$defs/"
                    if isinstance(reference, str) and reference.startswith(prefix):
                        target = definitions.get(reference[len(prefix) :])
                        if isinstance(target, dict):
                            branches.append(convert(target))
                            continue
                branches.append(convert(branch))
            converted[key] = branches
        return converted

    converted_schema = convert(schema)
    if not isinstance(converted_schema, dict):
        raise ValueError("structured response schema must be an object")
    return converted_schema
