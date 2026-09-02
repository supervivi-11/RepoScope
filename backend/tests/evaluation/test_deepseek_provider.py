from __future__ import annotations

from types import SimpleNamespace
import json
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from pydantic import ValidationError
from openai import InternalServerError

from app.agent import AnalysisReport, IssueUnderstanding, ModelPhase, ToolRequest, invoke_structured
from app.evaluation.deepseek_provider import (
    DeepSeekCredentials,
    DeepSeekEvaluationGateway,
    InMemoryCallLedger,
    TokenBudgetExceeded,
    UsageUnverifiable,
    SchemaUnverifiable,
    _phase_instruction,
    _estimated_cost,
    _deepseek_json_schema,
    _rate_period,
)
from app.evaluation.errors import EvaluationRunAbort
from app.evaluation.real_contracts import DeepSeekRunConfig


def _understanding() -> IssueUnderstanding:
    return IssueUnderstanding(
        summary="Parser regression.",
        observed_behavior="Parsing fails.",
        expected_behavior="Parsing succeeds.",
        search_terms=("parse",),
    )


def test_deepseek_schema_types_nullable_reference_without_weakening_contract() -> None:
    schema = _deepseek_json_schema(AnalysisReport)
    primary = schema["properties"]["primary_hypothesis"]

    assert len(primary["anyOf"]) == 2
    assert primary["anyOf"][1] == {"type": "null"}
    assert primary["anyOf"][0]["type"] == "object"
    assert "statement" in primary["anyOf"][0]["properties"]
    assert "$ref" not in primary["anyOf"][0]
    assert schema["additionalProperties"] is False


def test_tool_selection_instruction_declares_read_only_tools_and_completion_rule() -> None:
    instruction = _phase_instruction(ModelPhase.TOOL_SELECTION)

    assert "get_repository_map" in instruction
    assert "search_code" in instruction
    assert "read_code" in instruction
    assert "complete=true" in instruction
    assert "tool_name=null" in instruction


@pytest.mark.anyio
async def test_semantically_invalid_structured_result_aborts_entire_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = SimpleNamespace(
        usage_metadata={
            "input_tokens": 10,
            "output_tokens": 5,
            "input_token_details": {"cache_read": 0},
        },
        response_metadata={"model_name": "deepseek-v4-flash"},
    )

    class _Structured:
        async def ainvoke(self, messages):
            return {
                "raw": raw,
                "parsed": {"complete": False, "tool_name": None, "arguments": []},
                "parsing_error": None,
            }

    class _ChatOpenAI:
        def __init__(self, **kwargs):
            pass

        def with_structured_output(self, response_model, **kwargs):
            return _Structured()

    monkeypatch.setattr("langchain_openai.ChatOpenAI", _ChatOpenAI)
    ledger = InMemoryCallLedger(max_total_tokens=2_500_000)
    gateway = DeepSeekEvaluationGateway(
        credentials=DeepSeekCredentials(api_key="test-key"),
        configuration=DeepSeekRunConfig.approved(),
        ledger=ledger,
        case_id="example-issue-1",
        system="reposcope",
    )

    with pytest.raises(SchemaUnverifiable):
        await gateway.generate(
            phase=ModelPhase.TOOL_SELECTION,
            response_model=ToolRequest,
            context={},
        )
    assert ledger.records[0].status == "failed"
    assert ledger.records[0].safe_error_code == "schema_error"


def test_credentials_are_required_from_the_deepseek_environment_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("REPOSCOPE_DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(ValidationError):
        DeepSeekCredentials(_env_file=None)

    monkeypatch.setenv("REPOSCOPE_DEEPSEEK_API_KEY", "  local-test-key  ")
    credentials = DeepSeekCredentials(_env_file=None)

    assert credentials.api_key.get_secret_value() == "local-test-key"
    assert set(type(credentials).model_fields) == {"api_key"}
    assert "local-test-key" not in repr(credentials)


@pytest.mark.anyio
async def test_gateway_uses_fixed_responses_configuration_and_records_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    raw = SimpleNamespace(
        usage_metadata={
            "input_tokens": 1200,
            "output_tokens": 300,
            "input_token_details": {"cache_read": 200},
            "output_token_details": {"reasoning": 100},
        },
        response_metadata={
            "model_name": "deepseek-v4-flash",
            "system_fingerprint": "fp_test",
        },
    )

    class _Structured:
        async def ainvoke(self, messages):
            captured["messages"] = messages
            return {"raw": raw, "parsed": _understanding(), "parsing_error": None}

    class _ChatOpenAI:
        def __init__(self, **kwargs):
            captured["constructor"] = kwargs

        def with_structured_output(self, response_model, **kwargs):
            captured["response_model"] = response_model
            captured["structured"] = kwargs
            return _Structured()

    monkeypatch.setattr("langchain_openai.ChatOpenAI", _ChatOpenAI)
    ledger = InMemoryCallLedger(max_total_tokens=2_500_000)
    gateway = DeepSeekEvaluationGateway(
        credentials=DeepSeekCredentials(api_key="test-key"),
        configuration=DeepSeekRunConfig.approved(),
        ledger=ledger,
        case_id="dateutil-dateutil-issue-926",
        system="reposcope",
    )

    result = await gateway.generate(
        phase=ModelPhase.ISSUE_UNDERSTANDING,
        response_model=IssueUnderstanding,
        context={"issue": {"title": "Failure"}},
        attempt=1,
    )

    assert result == _understanding()
    constructor = captured["constructor"]
    assert constructor["model"] == "deepseek-v4-flash"
    assert constructor["base_url"] == "https://api.deepseek.com"
    assert constructor["use_responses_api"] is True
    assert constructor["reasoning_effort"] == "high"
    assert "extra_body" not in constructor
    assert constructor["max_completion_tokens"] == 8192
    assert constructor["max_retries"] == 0
    assert constructor["timeout"] == 120
    assert "temperature" not in constructor
    assert captured["structured"] == {
        "method": "json_schema",
        "include_raw": True,
        "strict": True,
    }
    messages = captured["messages"]
    assert "JSON" in messages[0][1]
    assert "hidden chain-of-thought" in messages[0][1]
    assert len(ledger.records) == 1
    call = ledger.records[0]
    assert call.case_id == "dateutil-dateutil-issue-926"
    assert call.system == "reposcope"
    assert call.phase == "issue_understanding"
    assert call.attempt == 1
    assert call.input_tokens == 1200
    assert call.cached_input_tokens == 200
    assert call.output_tokens == 300
    assert call.reasoning_tokens == 100
    assert call.returned_model == "deepseek-v4-flash"
    assert call.system_fingerprint == "fp_test"
    assert call.estimated_cost_usd is not None
    assert call.estimated_cost_usd > 0


@pytest.mark.anyio
async def test_real_langchain_client_sends_fixed_responses_wire_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from langchain_openai import ChatOpenAI as RealChatOpenAI

    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "resp_test",
                "object": "response",
                "created_at": 1788310800,
                "status": "completed",
                "model": "deepseek-v4-flash",
                "output": [
                    {
                        "id": "msg_test",
                        "type": "message",
                        "status": "completed",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": _understanding().model_dump_json(),
                                "annotations": [],
                            }
                        ],
                    }
                ],
                "usage": {
                    "input_tokens": 10,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens": 5,
                    "output_tokens_details": {"reasoning_tokens": 1},
                    "total_tokens": 15,
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    def build_real_client(**kwargs):
        return RealChatOpenAI(**kwargs, http_async_client=client)

    monkeypatch.setattr("langchain_openai.ChatOpenAI", build_real_client)
    gateway = DeepSeekEvaluationGateway(
        credentials=DeepSeekCredentials(api_key="test-key"),
        configuration=DeepSeekRunConfig.approved(),
        ledger=InMemoryCallLedger(max_total_tokens=2_500_000),
        case_id="schema-preflight",
        system="preflight",
    )
    try:
        result = await gateway.generate(
            phase=ModelPhase.ISSUE_UNDERSTANDING,
            response_model=IssueUnderstanding,
            context={"issue": "synthetic"},
        )
    finally:
        await client.aclose()

    assert result == _understanding()
    assert captured["url"] == "https://api.deepseek.com/responses"
    payload = captured["payload"]
    assert payload["model"] == "deepseek-v4-flash"
    assert payload["reasoning"] == {"effort": "high"}
    assert payload["max_output_tokens"] == 8192
    assert payload["text"]["format"]["type"] == "json_schema"
    assert payload["text"]["format"]["strict"] is True
    assert "temperature" not in payload
    assert "thinking" not in payload


@pytest.mark.anyio
async def test_gateway_records_missing_usage_without_estimating_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = SimpleNamespace(
        usage_metadata=None,
        response_metadata={"model_name": "deepseek-v4-flash"},
    )

    class _Structured:
        async def ainvoke(self, messages):
            return {"raw": raw, "parsed": _understanding(), "parsing_error": None}

    class _ChatOpenAI:
        def __init__(self, **kwargs):
            pass

        def with_structured_output(self, response_model, **kwargs):
            return _Structured()

    monkeypatch.setattr("langchain_openai.ChatOpenAI", _ChatOpenAI)
    ledger = InMemoryCallLedger(max_total_tokens=2_500_000)
    gateway = DeepSeekEvaluationGateway(
        credentials=DeepSeekCredentials(api_key="test-key"),
        configuration=DeepSeekRunConfig.approved(),
        ledger=ledger,
        case_id="dateutil-dateutil-issue-926",
        system="issue_only",
    )

    with pytest.raises(UsageUnverifiable):
        await gateway.generate(
            phase=ModelPhase.ISSUE_UNDERSTANDING,
            response_model=IssueUnderstanding,
            context={},
            attempt=1,
        )

    assert ledger.records[0].total_tokens is None
    assert ledger.records[0].estimated_cost_usd is None
    assert ledger.records[0].status == "failed"
    assert isinstance(UsageUnverifiable(), EvaluationRunAbort)


@pytest.mark.anyio
async def test_missing_cache_breakdown_is_null_and_never_guessed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = SimpleNamespace(
        usage_metadata={"input_tokens": 1200, "output_tokens": 300},
        response_metadata={"model_name": "deepseek-v4-flash"},
    )

    class _Structured:
        async def ainvoke(self, messages):
            return {"raw": raw, "parsed": _understanding(), "parsing_error": None}

    class _ChatOpenAI:
        def __init__(self, **kwargs):
            pass

        def with_structured_output(self, response_model, **kwargs):
            return _Structured()

    monkeypatch.setattr("langchain_openai.ChatOpenAI", _ChatOpenAI)
    ledger = InMemoryCallLedger(max_total_tokens=2_500_000)
    gateway = DeepSeekEvaluationGateway(
        credentials=DeepSeekCredentials(api_key="test-key"),
        configuration=DeepSeekRunConfig.approved(),
        ledger=ledger,
        case_id="dateutil-dateutil-issue-926",
        system="issue_only",
    )

    await gateway.generate(
        phase=ModelPhase.ISSUE_UNDERSTANDING,
        response_model=IssueUnderstanding,
        context={},
    )

    assert ledger.records[0].cached_input_tokens is None
    assert ledger.records[0].estimated_cost_usd is None


def test_token_budget_excess_is_fatal_seals_ledger_and_persists_record(
    tmp_path,
) -> None:
    started = datetime(2026, 9, 2, 1, 0, tzinfo=UTC)
    from app.evaluation.real_contracts import ProviderCallUsage

    ledger = InMemoryCallLedger(
        max_total_tokens=10,
        journal_path=tmp_path / "call-usage.v1.jsonl",
    )
    record = ProviderCallUsage(
        schema_version="reposcope.eval.call-usage.v1",
        call_id="call-0001",
        case_id="schema-preflight",
        system="preflight",
        phase="issue_understanding",
        attempt=1,
        requested_model="deepseek-v4-flash",
        returned_model="deepseek-v4-flash",
        started_at=started,
        finished_at=started,
        latency_ms=0,
        input_tokens=8,
        cached_input_tokens=0,
        output_tokens=3,
        rate_period="peak",
        cache_hit_usd_per_million=Decimal("0.014"),
        cache_miss_usd_per_million=Decimal("0.44"),
        output_usd_per_million=Decimal("1.32"),
        estimated_cost_usd=Decimal("0.00000748"),
    )

    with pytest.raises(TokenBudgetExceeded):
        ledger.append(record)
    assert len(ledger.records) == 1
    assert (tmp_path / "call-usage.v1.jsonl").read_text(encoding="utf-8")
    with pytest.raises(TokenBudgetExceeded):
        ledger.ensure_can_start()


@pytest.mark.anyio
async def test_gateway_never_sends_a_second_request_after_observed_budget_excess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_count = 0
    raw = SimpleNamespace(
        usage_metadata={
            "input_tokens": 8,
            "output_tokens": 3,
            "input_token_details": {"cache_read": 0},
        },
        response_metadata={"model_name": "deepseek-v4-flash"},
    )

    class _Structured:
        async def ainvoke(self, messages):
            nonlocal request_count
            request_count += 1
            return {"raw": raw, "parsed": _understanding(), "parsing_error": None}

    class _ChatOpenAI:
        def __init__(self, **kwargs):
            pass

        def with_structured_output(self, response_model, **kwargs):
            return _Structured()

    monkeypatch.setattr("langchain_openai.ChatOpenAI", _ChatOpenAI)
    gateway = DeepSeekEvaluationGateway(
        credentials=DeepSeekCredentials(api_key="test-key"),
        configuration=DeepSeekRunConfig.approved(),
        ledger=InMemoryCallLedger(max_total_tokens=10),
        case_id="schema-preflight",
        system="preflight",
    )

    for _ in range(2):
        with pytest.raises(TokenBudgetExceeded):
            await gateway.generate(
                phase=ModelPhase.ISSUE_UNDERSTANDING,
                response_model=IssueUnderstanding,
                context={},
            )
    assert request_count == 1


@pytest.mark.anyio
async def test_internal_server_error_is_transient_and_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Structured:
        async def ainvoke(self, messages):
            request = httpx.Request("POST", "https://api.deepseek.com/responses")
            response = httpx.Response(503, request=request)
            raise InternalServerError("service unavailable", response=response, body={})

    class _ChatOpenAI:
        def __init__(self, **kwargs):
            pass

        def with_structured_output(self, response_model, **kwargs):
            return _Structured()

    monkeypatch.setattr("langchain_openai.ChatOpenAI", _ChatOpenAI)
    gateway = DeepSeekEvaluationGateway(
        credentials=DeepSeekCredentials(api_key="test-key"),
        configuration=DeepSeekRunConfig.approved(),
        ledger=InMemoryCallLedger(max_total_tokens=2_500_000),
        case_id="schema-preflight",
        system="preflight",
    )

    from app.agent import TransientModelError

    with pytest.raises(TransientModelError):
        await gateway.generate(
            phase=ModelPhase.ISSUE_UNDERSTANDING,
            response_model=IssueUnderstanding,
            context={},
        )


@pytest.mark.anyio
async def test_invoke_structured_passes_actual_retry_attempt_to_gateway() -> None:
    attempts: list[int] = []

    class _Gateway:
        async def generate(self, *, phase, response_model, context, attempt):
            attempts.append(attempt)
            if attempt < 3:
                from app.agent import TransientModelError

                raise TransientModelError()
            return _understanding()

    result = await invoke_structured(
        _Gateway(),
        phase=ModelPhase.ISSUE_UNDERSTANDING,
        response_model=IssueUnderstanding,
        context={},
        retries=2,
    )

    assert result.attempts == 3
    assert attempts == [1, 2, 3]


@pytest.mark.parametrize(
    ("timestamp", "expected"),
    (
        (datetime(2026, 9, 7, 0, 59, tzinfo=UTC), "off_peak"),
        (datetime(2026, 9, 7, 1, 0, tzinfo=UTC), "peak"),
        (datetime(2026, 9, 7, 4, 0, tzinfo=UTC), "off_peak"),
        (datetime(2026, 9, 7, 6, 0, tzinfo=UTC), "peak"),
        (datetime(2026, 9, 7, 10, 0, tzinfo=UTC), "off_peak"),
        (datetime(2026, 9, 6, 2, 0, tzinfo=UTC), "off_peak"),
    ),
)
def test_deepseek_rate_period_uses_fixed_official_utc_windows(
    timestamp: datetime, expected: str
) -> None:
    assert _rate_period(timestamp) == expected


def test_deepseek_cost_uses_cache_hit_miss_and_output_rates() -> None:
    assert _estimated_cost(
        input_tokens=1200,
        cached_input_tokens=200,
        output_tokens=300,
        cache_hit_rate=Decimal("0.007"),
        cache_miss_rate=Decimal("0.22"),
        output_rate=Decimal("0.66"),
    ) == Decimal("0.0004194")


@pytest.mark.anyio
async def test_failed_provider_attempt_is_recorded_without_secret_or_fake_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Structured:
        async def ainvoke(self, messages):
            raise __import__("openai").APITimeoutError(
                httpx.Request("POST", "https://api.deepseek.com/responses")
            )

    class _ChatOpenAI:
        def __init__(self, **kwargs):
            pass

        def with_structured_output(self, response_model, **kwargs):
            return _Structured()

    monkeypatch.setattr("langchain_openai.ChatOpenAI", _ChatOpenAI)
    ledger = InMemoryCallLedger(max_total_tokens=2_500_000)
    gateway = DeepSeekEvaluationGateway(
        credentials=DeepSeekCredentials(api_key="top-secret-test-key"),
        configuration=DeepSeekRunConfig.approved(),
        ledger=ledger,
        case_id="dateutil-dateutil-issue-926",
        system="reposcope",
    )

    from app.agent import TransientModelError

    with pytest.raises(TransientModelError):
        await gateway.generate(
            phase=ModelPhase.ISSUE_UNDERSTANDING,
            response_model=IssueUnderstanding,
            context={},
            attempt=2,
        )

    assert len(ledger.records) == 1
    record = ledger.records[0]
    assert record.status == "failed"
    assert record.safe_error_code == "provider_transient"
    assert record.attempt == 2
    assert record.total_tokens is None
    assert record.estimated_cost_usd is None
    assert "top-secret-test-key" not in record.model_dump_json()
