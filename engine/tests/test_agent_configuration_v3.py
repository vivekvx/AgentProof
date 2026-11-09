import json

import pytest
from agentproof.benchmark.contracts import (
    AgentConfiguration,
    AgentConfigurationV3,
    parse_agent_configuration,
)
from pydantic import ValidationError


def config(policy="external"):
    return {
        "schema_version": "3.0",
        "id": "external-agent",
        "version": 1,
        "title": "External agent",
        "policy": policy,
        "model_configuration": {
            "provider": "offline",
            "model": "external-agent"
            if policy == "external"
            else "pydantic-function-model",
            "external_calls": False,
        },
        "prompt": "Use the public request and permitted tools only.",
        "prompt_version": "support-prompt-v1",
        "tool_schema_version": "support-tools-v1",
        "max_retries": 0,
        "external_session": {
            "execution_mode": "external",
            "transport": "http-json",
            "session_ttl_seconds": 300,
            "credential_scope": "single-trial",
        }
        if policy == "external"
        else None,
    }


def test_external_v3_serializes_and_round_trips_with_external_dispatch():
    from agentproof.benchmark.contracts import AgentConfigurationV3

    parsed = AgentConfigurationV3.model_validate(config())
    assert parsed.execution_mode == "external"
    dumped = json.loads(parsed.model_dump_json())
    assert AgentConfigurationV3.model_validate(dumped) == parsed
    dispatched = parse_agent_configuration(dumped)
    assert isinstance(dispatched, AgentConfigurationV3)
    assert dispatched.execution_mode == "external"


@pytest.mark.parametrize("policy", ["reconcile", "blind-retry", "llm"])
def test_v2_keeps_existing_modes(policy):
    legacy = {
        "schema_version": "2.0",
        "id": "support-agent",
        "version": 1,
        "title": "Existing agent",
        "policy": policy,
        "model_configuration": {
            "provider": "offline",
            "model": "pydantic-function-model",
            "external_calls": False,
        },
        "prompt": "Use the public request and permitted tools only.",
        "prompt_version": "support-prompt-v1",
        "tool_schema_version": "support-tools-v1",
        "max_retries": 0,
    }
    if policy == "llm":
        legacy["model_configuration"] = {
            "provider": "openai-compatible",
            "model": "test-model",
            "base_url": "https://example.test/v1",
            "api_key_env": "TEST_API_KEY",
            "tool_calling": True,
            "input_usd_per_million": 1,
            "output_usd_per_million": 1,
            "pricing_source": "https://example.test/pricing",
            "pricing_checked_at": "2026-11-09",
        }
    assert parse_agent_configuration(legacy).policy == policy
    assert AgentConfiguration.model_validate(legacy).policy == policy


def test_external_rejects_llm_settings_missing_session_and_invalid_policy():
    bad = config()
    bad["model_configuration"] = {"provider": "openai-compatible"}
    with pytest.raises(ValidationError):
        AgentConfigurationV3.model_validate(bad)
    bad = config()
    bad.pop("external_session")
    with pytest.raises(ValidationError):
        AgentConfigurationV3.model_validate(bad)
    bad = config("nope")
    with pytest.raises(ValidationError):
        AgentConfigurationV3.model_validate(bad)
