from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.evaluation.real_contracts import (
    ArtifactDigest,
    DependencyVersion,
    DeepSeekRunConfig,
    ProviderCallUsage,
    RunArtifactManifest,
    REQUIRED_DEPENDENCIES,
    configuration_digest,
)
from app.evaluation.schemas import export_schemas
from app.evaluation.real_validation import provider_backend_drift


def test_approved_deepseek_configuration_is_fixed_redacted_and_deterministic() -> None:
    config = DeepSeekRunConfig.approved()

    assert config.schema_version == "reposcope.eval.run-config.v1"
    assert config.provider == "deepseek_official"
    assert config.base_url == "https://api.deepseek.com"
    assert config.requested_model == "deepseek-v4-flash"
    assert config.documented_model_version == "DeepSeek-V4-Flash-0731"
    assert config.api_surface == "responses"
    assert config.thinking == "enabled"
    assert config.reasoning_effort == "low"
    assert config.temperature is None
    assert config.max_output_tokens == 16384
    assert config.max_total_tokens == 2_500_000
    assert config.max_tool_calls == 12
    assert config.max_evidence_rounds == 2
    assert config.model_retries == 2
    assert config.request_timeout_seconds == 120
    assert config.split == "development"
    assert config.case_count == 6
    assert config.rate_card_version == "deepseek-v4-2026-08-16-v1"
    assert config.rate_card_retrieved_on == "2026-09-01"
    assert "key" not in type(config).model_fields
    assert configuration_digest(config) == configuration_digest(
        DeepSeekRunConfig.approved()
    )
    assert len(configuration_digest(config)) == 64

    serialized = config.model_dump_json().casefold()
    assert "api_key" not in serialized
    assert "secret" not in serialized


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("base_url", "https://provider.example"),
        ("requested_model", "deepseek-v4-pro"),
        ("temperature", 0),
        ("split", "hidden"),
        ("case_count", 12),
        ("max_tool_calls", 11),
    ),
)
def test_fixed_configuration_rejects_experiment_drift(field: str, value: object) -> None:
    payload = DeepSeekRunConfig.approved().model_dump()
    payload[field] = value

    with pytest.raises(ValidationError):
        DeepSeekRunConfig.model_validate(payload)


def test_provider_call_usage_preserves_auditable_token_breakdown() -> None:
    started = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    call = ProviderCallUsage(
        schema_version="reposcope.eval.call-usage.v1",
        call_id="call-0001",
        case_id="dateutil-dateutil-issue-926",
        system="reposcope",
        phase="tool_selection",
        attempt=1,
        requested_model="deepseek-v4-flash",
        returned_model="deepseek-v4-flash",
        system_fingerprint="fp_test",
        started_at=started,
        finished_at=started + timedelta(milliseconds=125),
        latency_ms=125,
        input_tokens=1200,
        cached_input_tokens=200,
        output_tokens=300,
        reasoning_tokens=100,
        rate_period="off_peak",
        cache_hit_usd_per_million=Decimal("0.007"),
        cache_miss_usd_per_million=Decimal("0.22"),
        output_usd_per_million=Decimal("0.66"),
        estimated_cost_usd=Decimal("0.0004194"),
    )

    assert call.uncached_input_tokens == 1000
    assert call.total_tokens == 1500
    assert call.finished_at > call.started_at
    assert "reasoning_content" not in type(call).model_fields

    with pytest.raises(ValidationError):
        ProviderCallUsage.model_validate(
            {**call.model_dump(), "cached_input_tokens": 1201}
        )

    missing = ProviderCallUsage.model_validate(
        {
            **call.model_dump(
                exclude={
                    "uncached_input_tokens",
                    "total_tokens",
                    "input_tokens",
                    "cached_input_tokens",
                    "output_tokens",
                    "estimated_cost_usd",
                }
            ),
            "input_tokens": None,
            "cached_input_tokens": None,
            "output_tokens": None,
            "estimated_cost_usd": None,
        }
    )
    assert missing.total_tokens is None
    assert missing.estimated_cost_usd is None

    no_cache_breakdown = ProviderCallUsage.model_validate(
        {
            **call.model_dump(),
            "cached_input_tokens": None,
            "estimated_cost_usd": None,
        }
    )
    assert no_cache_breakdown.total_tokens == 1500
    assert no_cache_breakdown.estimated_cost_usd is None


def test_provider_backend_drift_uses_observed_models_without_alias_fallback() -> None:
    started = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)

    def usage(call_id: str, returned_model: str | None) -> ProviderCallUsage:
        return ProviderCallUsage(
            schema_version="reposcope.eval.call-usage.v1",
            call_id=call_id,
            case_id="schema-preflight",
            system="preflight",
            phase="issue_understanding",
            attempt=1,
            requested_model="deepseek-v4-flash",
            returned_model=returned_model,
            started_at=started,
            finished_at=started,
            latency_ms=0,
            input_tokens=1,
            cached_input_tokens=0,
            output_tokens=1,
            rate_period="off_peak",
            cache_hit_usd_per_million=Decimal("0.007"),
            cache_miss_usd_per_million=Decimal("0.22"),
            output_usd_per_million=Decimal("0.66"),
            estimated_cost_usd=Decimal("0.00000088"),
        )

    flash = usage("call-0001", "deepseek-v4-flash")
    pro = usage("call-0001", "deepseek-v4-pro")
    unknown = usage("call-0001", None)
    assert provider_backend_drift((flash,), "deepseek-v4-flash") is False
    assert provider_backend_drift((pro,), "deepseek-v4-flash") is True
    assert provider_backend_drift((unknown,), "deepseek-v4-flash") is None
    assert provider_backend_drift(
        (flash, pro.model_copy(update={"call_id": "call-0002"})),
        "deepseek-v4-flash",
    ) is True
    failed_pro = pro.model_copy(
        update={
            "call_id": "call-0002",
            "status": "failed",
            "safe_error_code": "schema_error",
        }
    )
    assert provider_backend_drift(
        (flash, failed_pro), "deepseek-v4-flash"
    ) is True


def test_artifact_manifest_accepts_only_relative_unique_hashed_outputs() -> None:
    manifest = RunArtifactManifest(
        schema_version="reposcope.eval.manifest.v1",
        completion_status="complete",
        run_id="20260901T120000Z-deepseek-v4-flash",
        dataset_digest="a" * 64,
        configuration_digest="b" * 64,
        reposcope_commit="e" * 40,
        working_tree_clean=True,
        python_version="3.12.10",
        dependency_versions=tuple(
            DependencyVersion(name=name, version="test")
            for name in REQUIRED_DEPENDENCIES
        ),
        execution_order=tuple(
            f"development-case-{number}:{system}"
            for number in range(1, 7)
            for system in ("issue_only", "reposcope")
        ),
        started_at=datetime(2026, 9, 2, 1, 0, tzinfo=UTC),
        finished_at=datetime(2026, 9, 2, 1, 5, tzinfo=UTC),
        systems=("issue_only", "reposcope"),
        case_count=6,
        artifacts=(
            ArtifactDigest(path="issue-only.results.v1.jsonl", sha256="c" * 64),
            ArtifactDigest(path="reposcope.results.v1.jsonl", sha256="d" * 64),
        ),
    )

    assert manifest.systems == ("issue_only", "reposcope")
    assert len(manifest.execution_order) == 12
    with pytest.raises(ValidationError):
        ArtifactDigest(path="../gold.jsonl", sha256="c" * 64)
    with pytest.raises(ValidationError):
        RunArtifactManifest.model_validate(
            {
                **manifest.model_dump(),
                "artifacts": (
                    manifest.artifacts[0].model_dump(),
                    manifest.artifacts[0].model_dump(),
                ),
            }
        )


def test_real_run_schemas_are_exported(tmp_path: Path) -> None:
    export_schemas(tmp_path)

    run_config = json.loads(
        (tmp_path / "run-config.v1.schema.json").read_text(encoding="utf-8")
    )
    call_usage = json.loads(
        (tmp_path / "call-usage.v1.schema.json").read_text(encoding="utf-8")
    )
    manifest = json.loads(
        (tmp_path / "manifest.v1.schema.json").read_text(encoding="utf-8")
    )

    assert run_config["properties"]["requested_model"]["const"] == (
        "deepseek-v4-flash"
    )
    assert "cached_input_tokens" in call_usage["properties"]
    assert manifest["properties"]["case_count"]["const"] == 6
