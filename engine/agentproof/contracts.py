from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1.0"] = "1.0"


class Fault(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["timeout_after_commit"]
    tool: Literal["cancel_order"]
    invocation: Literal[1]


class Assertions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    final_status: Literal["cancelled"]
    side_effect_count: Literal[1]


class Scenario(Contract):
    id: Literal["cancel-order"]
    version: Literal[1]
    order_id: str
    initial_status: Literal["active"]
    fault: Fault
    assertions: Assertions


class Reference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    version: int
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    content: dict[str, Any]


class Manifest(Contract):
    run_id: str
    agent: Literal["safe", "duplicate"]
    agent_version: Literal["reference-v1"]
    scenario: Reference
    evaluator: Reference
    traceparent: str


class ToolEvent(Contract):
    sequence: int
    tool: Literal["cancel_order", "get_order"]
    arguments: dict[str, str]
    outcome: Literal["ok", "timeout_after_commit"]
    output: dict[str, Any]
    side_effect_committed: bool
    timestamp: str
    trace_id: str
    span_id: str


class Check(BaseModel):
    name: str
    passed: bool
    expected: str | int | bool
    actual: str | int | bool


class EvaluationResult(Contract):
    run_id: str
    status: Literal["passed", "failed", "infrastructure_failed"]
    failure_kind: Literal["agent", "infrastructure"] | None = None
    checks: list[Check]
    final_state: dict[str, Any]
    side_effect_count: int
    scenario_sha256: str
    evaluator_sha256: str
    trace_id: str
    error: str | None = None
