"""T12 protocol validation tests; transport and business authorization stay external."""

from uuid import uuid4

import pytest
from agentproof.agent_protocol import (
    AgentOutcome,
    AgentTaskEnvelope,
    AgentToolCall,
    AgentToolResult,
    validate_correlation,
    validate_outcome,
    validate_tool_call,
)
from pydantic import ValidationError


def envelope():
    return AgentTaskEnvelope.model_validate(
        {
            "run_id": uuid4(),
            "trial_id": uuid4(),
            "scenario_id": "cs-001",
            "request": "Cancel this order if it is eligible.",
            "tools": [
                {
                    "name": "get_order",
                    "description": "Read the assigned synthetic order.",
                    "input_schema": {"type": "object", "additionalProperties": False},
                }
            ],
            "limits": {"max_tool_calls": 2, "max_message_bytes": 4096},
            "deadline_unix_ms": 1_800_000_000_000,
            "resume_token": None,
        }
    )


def test_task_envelope_exposes_only_public_bounded_fields():
    task = envelope()
    assert task.limits.max_tool_calls == 2
    assert "fixture" not in task.model_dump()
    assert "assertions" not in task.model_dump()


@pytest.mark.parametrize(
    "patch",
    [
        {"private_assertions": {"status": "cancelled"}},
        {"resume_token": "x" * 257},
        {"request": ""},
        {"limits": {"max_tool_calls": 33, "max_message_bytes": 4096}},
    ],
)
def test_envelope_rejects_private_or_unbounded_fields(patch):
    raw = envelope().model_dump()
    raw.update(patch)
    with pytest.raises(ValidationError):
        AgentTaskEnvelope.model_validate(raw)


def test_duplicate_tool_names_are_rejected():
    raw = envelope().model_dump()
    raw["tools"].append(raw["tools"][0])
    with pytest.raises(ValidationError, match="duplicate tool"):
        AgentTaskEnvelope.model_validate(raw)


def test_valid_call_is_correlated_and_sequence_bounded():
    task = envelope()
    call = AgentToolCall(
        run_id=task.run_id,
        trial_id=task.trial_id,
        sequence=1,
        operation_id=uuid4(),
        tool="get_order",
        arguments={},
    )
    validate_tool_call(task, call)
    with pytest.raises(ValueError, match="limit"):
        validate_tool_call(task, call.model_copy(update={"sequence": 3}))


@pytest.mark.parametrize("field", ["run_id", "trial_id"])
def test_stale_or_cross_trial_call_is_rejected(field):
    task = envelope()
    call = AgentToolCall(
        run_id=task.run_id,
        trial_id=task.trial_id,
        sequence=1,
        operation_id=uuid4(),
        tool="get_order",
        arguments={},
    ).model_copy(update={field: uuid4()})
    with pytest.raises(ValueError, match="identity"):
        validate_correlation(task, call)


def test_unknown_tool_and_malformed_call_fail_closed():
    task = envelope()
    call = AgentToolCall(
        run_id=task.run_id,
        trial_id=task.trial_id,
        sequence=1,
        operation_id=uuid4(),
        tool="cancel_order",
        arguments={},
    )
    with pytest.raises(ValueError, match="unknown tool"):
        validate_tool_call(task, call)
    raw = call.model_dump()
    raw["unexpected"] = "private evidence"
    with pytest.raises(ValidationError):
        AgentToolCall.model_validate(raw)


def test_tool_result_requires_success_payload_or_fixed_error_code():
    common = {
        "run_id": uuid4(),
        "trial_id": uuid4(),
        "sequence": 1,
        "operation_id": uuid4(),
    }
    assert AgentToolResult(**common, ok=False, error_code="unauthorized").ok is False
    with pytest.raises(ValidationError):
        AgentToolResult(**common, ok=False, error_code="raw provider stack trace")
    with pytest.raises(ValidationError):
        AgentToolResult(**common, ok=True)


def test_outcomes_cover_refusal_failure_and_cancellation_without_arbitrary_errors():
    task = envelope()
    refusal = AgentOutcome(
        run_id=task.run_id,
        trial_id=task.trial_id,
        last_sequence=0,
        resolution="refused",
        message="The order is not eligible for cancellation.",
    )
    validate_outcome(task, refusal)
    cancelled = AgentOutcome(
        run_id=task.run_id,
        trial_id=task.trial_id,
        last_sequence=0,
        resolution="unknown",
        error_code="cancelled",
    )
    validate_outcome(task, cancelled)
    with pytest.raises(ValidationError):
        AgentOutcome(
            run_id=task.run_id,
            trial_id=task.trial_id,
            last_sequence=0,
            resolution="unknown",
            error_code="internal_error",
            message="sensitive provider response",
        )


def test_outcome_cannot_claim_unobserved_sequence():
    task = envelope()
    outcome = AgentOutcome(
        run_id=task.run_id,
        trial_id=task.trial_id,
        last_sequence=3,
        resolution="completed",
    )
    with pytest.raises(ValueError, match="limit"):
        validate_outcome(task, outcome)
