from __future__ import annotations

from collections import deque

import httpx
import pytest

from app.agent import (
    IssueUnderstanding,
    ModelPhase,
    ModelSafetyError,
    ModelSchemaError,
    PermanentModelError,
    TransientModelError,
    OpenAICompatibleGateway,
    OpenAIModelSettings,
    invoke_structured,
)


class _ScriptedGateway:
    def __init__(self, *responses: object) -> None:
        self.responses = deque(responses)
        self.calls = 0

    async def generate(self, *, phase, response_model, context, attempt=1):
        self.calls += 1
        response = self.responses.popleft()
        if isinstance(response, Exception):
            raise response
        return response


def _understanding() -> IssueUnderstanding:
    return IssueUnderstanding(
        summary="Parser regression.",
        observed_behavior="A value is returned unchanged.",
        expected_behavior="The value should be validated.",
        search_terms=("parse", "validation"),
    )


@pytest.mark.anyio
async def test_transient_model_failures_retry_twice_after_first_attempt() -> None:
    """Breaks if transient calls exceed three attempts or stop retrying too early."""
    gateway = _ScriptedGateway(
        TransientModelError(), TransientModelError(), _understanding()
    )

    result = await invoke_structured(
        gateway,
        phase=ModelPhase.ISSUE_UNDERSTANDING,
        response_model=IssueUnderstanding,
        context={"issue": {"number": 7}},
        retries=2,
    )

    assert result.value == _understanding()
    assert result.attempts == 3
    assert gateway.calls == 3


@pytest.mark.anyio
async def test_transient_failure_stops_after_configured_retry_ceiling() -> None:
    """Breaks if a failing provider can create an unbounded retry loop."""
    gateway = _ScriptedGateway(
        TransientModelError(), TransientModelError(), TransientModelError()
    )

    with pytest.raises(TransientModelError):
        await invoke_structured(
            gateway,
            phase=ModelPhase.ISSUE_UNDERSTANDING,
            response_model=IssueUnderstanding,
            context={},
            retries=2,
        )
    assert gateway.calls == 3


@pytest.mark.anyio
@pytest.mark.parametrize(
    "failure",
    [ModelSchemaError(), ModelSafetyError(), PermanentModelError()],
)
async def test_schema_safety_and_permanent_failures_are_never_retried(
    failure: Exception,
) -> None:
    """Breaks if malformed or unsafe model responses are repeatedly submitted."""
    gateway = _ScriptedGateway(failure, _understanding())

    with pytest.raises(type(failure)):
        await invoke_structured(
            gateway,
            phase=ModelPhase.ISSUE_UNDERSTANDING,
            response_model=IssueUnderstanding,
            context={},
            retries=2,
        )
    assert gateway.calls == 1


@pytest.mark.anyio
async def test_wrong_structured_type_is_a_nonretryable_schema_failure() -> None:
    """Breaks if an untyped model object can enter graph state."""
    gateway = _ScriptedGateway({"summary": "not validated"}, _understanding())

    with pytest.raises(ModelSchemaError):
        await invoke_structured(
            gateway,
            phase=ModelPhase.ISSUE_UNDERSTANDING,
            response_model=IssueUnderstanding,
            context={},
            retries=2,
        )
    assert gateway.calls == 1


@pytest.mark.anyio
async def test_failed_attempts_preserve_actual_transient_then_permanent_count() -> None:
    """Breaks if graph counters report a retry budget instead of real provider calls."""
    gateway = _ScriptedGateway(TransientModelError(), PermanentModelError())

    with pytest.raises(PermanentModelError) as raised:
        await invoke_structured(
            gateway,
            phase=ModelPhase.ISSUE_UNDERSTANDING,
            response_model=IssueUnderstanding,
            context={},
            retries=2,
        )

    assert raised.value.attempts == 2
    assert gateway.calls == 2


@pytest.mark.anyio
async def test_zero_retry_budget_records_one_failed_attempt() -> None:
    """Breaks if disabling graph retries still reports the default retry budget."""
    gateway = _ScriptedGateway(TransientModelError())

    with pytest.raises(TransientModelError) as raised:
        await invoke_structured(
            gateway,
            phase=ModelPhase.ISSUE_UNDERSTANDING,
            response_model=IssueUnderstanding,
            context={},
            retries=0,
        )

    assert raised.value.attempts == 1
    assert gateway.calls == 1


@pytest.mark.anyio
async def test_openai_adapter_disables_sdk_retries_and_maps_timeout_transient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Breaks if a provider timeout is retried outside the bounded graph loop."""
    captured: dict[str, object] = {}

    class _Structured:
        async def ainvoke(self, messages):
            raise __import__("openai").APITimeoutError(
                httpx.Request("POST", "https://provider.example/v1")
            )

    class _ChatOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def with_structured_output(self, response_model, method):
            return _Structured()

    monkeypatch.setattr("langchain_openai.ChatOpenAI", _ChatOpenAI)
    gateway = OpenAICompatibleGateway(OpenAIModelSettings(api_key="test-key"))

    with pytest.raises(TransientModelError):
        await gateway.generate(
            phase=ModelPhase.ISSUE_UNDERSTANDING,
            response_model=IssueUnderstanding,
            context={},
        )

    assert captured["max_retries"] == 0
