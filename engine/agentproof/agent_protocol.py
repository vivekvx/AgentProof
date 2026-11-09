"""Strict, transport-neutral validation for AgentProof external-agent protocol v1."""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ProtocolModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal["1.0"] = "1.0"


class ToolDescriptor(ProtocolModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    description: str = Field(max_length=300)
    input_schema: dict[str, Any] = Field(max_length=32)


class ExecutionLimits(ProtocolModel):
    max_tool_calls: int = Field(ge=1, le=32)
    max_message_bytes: int = Field(ge=256, le=65536)


class AgentTaskEnvelope(ProtocolModel):
    run_id: UUID
    trial_id: UUID
    scenario_id: str = Field(min_length=1, max_length=80)
    request: str = Field(min_length=1, max_length=2000)
    tools: list[ToolDescriptor] = Field(min_length=1, max_length=16)
    limits: ExecutionLimits
    deadline_unix_ms: int = Field(gt=0)
    resume_token: str | None = Field(max_length=256)

    @model_validator(mode="after")
    def unique_tools(self):
        names = [tool.name for tool in self.tools]
        if len(names) != len(set(names)):
            raise ValueError("duplicate tool name")
        return self


class AgentToolCall(ProtocolModel):
    run_id: UUID
    trial_id: UUID
    sequence: int = Field(ge=1, le=32)
    operation_id: UUID
    tool: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    arguments: dict[str, Any] = Field(max_length=32)


class AgentToolResult(ProtocolModel):
    run_id: UUID
    trial_id: UUID
    sequence: int = Field(ge=1, le=32)
    operation_id: UUID
    ok: bool
    result: dict[str, Any] | None = Field(default=None, max_length=64)
    error_code: (
        Literal[
            "unknown_tool",
            "invalid_arguments",
            "unauthorized",
            "budget_exhausted",
            "deadline_exceeded",
            "unavailable",
            "replay_mismatch",
        ]
        | None
    ) = None

    @model_validator(mode="after")
    def consistent_result(self):
        if self.ok and (self.result is None or self.error_code is not None):
            raise ValueError("successful tool result requires result only")
        if not self.ok and (self.error_code is None or self.result is not None):
            raise ValueError("failed tool result requires a bounded error code only")
        return self


class AgentOutcome(ProtocolModel):
    run_id: UUID
    trial_id: UUID
    last_sequence: int = Field(ge=0, le=32)
    resolution: Literal["completed", "refused", "blocked", "unknown"]
    message: str | None = Field(default=None, max_length=2000)
    error_code: (
        Literal[
            "deadline_exceeded",
            "cancelled",
            "unavailable",
            "malformed_request",
            "internal_error",
        ]
        | None
    ) = None

    @model_validator(mode="after")
    def no_arbitrary_error_text(self):
        if self.error_code is not None and self.message is not None:
            raise ValueError("error outcome cannot include arbitrary error text")
        return self


def validate_correlation(envelope: AgentTaskEnvelope, message: BaseModel) -> None:
    """Reject stale or cross-trial messages before broker authorization runs."""
    if message.run_id != envelope.run_id or message.trial_id != envelope.trial_id:
        raise ValueError("message identity does not match active trial")


def validate_tool_call(envelope: AgentTaskEnvelope, call: AgentToolCall) -> None:
    validate_correlation(envelope, call)
    if call.sequence > envelope.limits.max_tool_calls:
        raise ValueError("tool call limit exceeded")
    allowed = {tool.name for tool in envelope.tools}
    if call.tool not in allowed:
        raise ValueError("unknown tool")


def validate_outcome(envelope: AgentTaskEnvelope, outcome: AgentOutcome) -> None:
    validate_correlation(envelope, outcome)
    if outcome.last_sequence > envelope.limits.max_tool_calls:
        raise ValueError("outcome sequence exceeds tool call limit")
