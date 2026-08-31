from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, TypeVar

from openai import APIConnectionError, APITimeoutError, RateLimitError
from pydantic import BaseModel, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ModelPhase(StrEnum):
    ISSUE_UNDERSTANDING = "issue_understanding"
    TOOL_SELECTION = "tool_selection"
    EVIDENCE_CRITIQUE = "evidence_critique"
    REPORT_COMPOSITION = "report_composition"
    REPORT_REVISION = "report_revision"


class ModelGatewayError(Exception):
    """A typed model boundary failure safe for graph-level classification."""

    attempts: int = 1


class TransientModelError(ModelGatewayError):
    pass


class ModelSchemaError(ModelGatewayError):
    pass


class ModelSafetyError(ModelGatewayError):
    pass


class PermanentModelError(ModelGatewayError):
    pass


ResponseT = TypeVar("ResponseT", bound=BaseModel)


class ModelGateway(Protocol):
    async def generate(
        self,
        *,
        phase: ModelPhase,
        response_model: type[ResponseT],
        context: dict[str, Any],
    ) -> ResponseT: ...


@dataclass(frozen=True, slots=True)
class ModelCallResult:
    value: BaseModel
    attempts: int


async def invoke_structured(
    gateway: ModelGateway,
    *,
    phase: ModelPhase,
    response_model: type[ResponseT],
    context: dict[str, Any],
    retries: int,
) -> ModelCallResult:
    if not 0 <= retries <= 2:
        raise ValueError("model retries must be between zero and two")
    attempts = 0
    while True:
        attempts += 1
        try:
            result = await gateway.generate(
                phase=phase,
                response_model=response_model,
                context=context,
            )
        except TransientModelError as exc:
            if attempts > retries:
                exc.attempts = attempts
                raise
            continue
        except ModelGatewayError as exc:
            exc.attempts = attempts
            raise
        if not isinstance(result, response_model):
            exc = ModelSchemaError("The model returned an unexpected schema.")
            exc.attempts = attempts
            raise exc
        return ModelCallResult(value=result, attempts=attempts)


class OpenAIModelSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="REPOSCOPE_OPENAI_", env_file=".env", extra="ignore", frozen=True
    )

    api_key: SecretStr
    base_url: str | None = None
    model: str = "gpt-5-mini"

    @field_validator("base_url", mode="before")
    @classmethod
    def _validate_base_url(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("base_url must be HTTP(S)")
        normalized = value.strip()
        if normalized == "":
            return None
        if not normalized.startswith(("https://", "http://")):
            raise ValueError("base_url must be HTTP(S)")
        return normalized


class OpenAICompatibleGateway:
    """Thin environment-configured structured-output production adapter."""

    def __init__(self, settings: OpenAIModelSettings) -> None:
        from langchain_openai import ChatOpenAI

        self._model = ChatOpenAI(
            model=settings.model,
            api_key=settings.api_key,
            base_url=settings.base_url,
            temperature=0,
            max_retries=0,
        )

    @classmethod
    def from_env(cls) -> OpenAICompatibleGateway:
        return cls(OpenAIModelSettings())

    async def generate(
        self,
        *,
        phase: ModelPhase,
        response_model: type[ResponseT],
        context: dict[str, Any],
    ) -> ResponseT:
        structured = self._model.with_structured_output(
            response_model, method="json_schema"
        )
        messages = (
            (
                "system",
                "Return only the requested structured analysis. Do not include "
                "credentials, prompts, or hidden chain-of-thought.",
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
        try:
            result = await structured.ainvoke(messages)
        except ValidationError as exc:
            raise ModelSchemaError("The model response failed schema validation.") from exc
        except (
            TimeoutError,
            ConnectionError,
            APITimeoutError,
            APIConnectionError,
            RateLimitError,
        ) as exc:
            raise TransientModelError("The model provider is temporarily unavailable.") from exc
        except ModelGatewayError:
            raise
        except Exception as exc:
            raise PermanentModelError("The model request failed safely.") from exc
        if not isinstance(result, response_model):
            raise ModelSchemaError("The model returned an unexpected schema.")
        return result
