from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Tool = Literal[
    "get_order", "cancel_order", "refund_order", "update_address", "request_approval"
]
Effect = Literal["cancellation", "refund", "address_change", "approval"]
Verdict = Literal["PASS", "FAIL", "INDETERMINATE"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal["2.0"] = "2.0"


class Identity(Contract):
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,63}$")
    version: int = Field(ge=1)


class Order(Contract):
    order_id: str = Field(min_length=1, max_length=64)
    customer_id: str = Field(min_length=1, max_length=64)
    exists: bool = True
    status: Literal["active", "cancelled", "shipped"] = "active"
    total_cents: int = Field(ge=1, le=1000000)
    refunded_cents: int = Field(ge=0)
    address: str = Field(min_length=1, max_length=200)
    approved: bool = False

    @model_validator(mode="after")
    def balances(self):
        if self.refunded_cents > self.total_cents:
            raise ValueError("refund exceeds order total")
        return self


class Fixture(Identity):
    order: Order


class UserRequest(Contract):
    text: str = Field(min_length=1, max_length=1000)
    action: Literal["cancel", "refund", "address", "status"]
    order_id: str = Field(min_length=1, max_length=64)
    customer_id: str = Field(min_length=1, max_length=64)
    amount_cents: int | None = None
    address: str | None = Field(default=None, max_length=200)
    user_approved: bool = False


class Permissions(Contract):
    tools: list[Tool] = Field(min_length=0, max_length=5)
    approval_required: bool = False

    @model_validator(mode="after")
    def unique(self):
        if len(set(self.tools)) != len(self.tools):
            raise ValueError("duplicate permission")
        return self


class FaultProfile(Identity):
    kind: Literal[
        "none",
        "timeout_before_commit",
        "timeout_after_commit",
        "temporary_api_failure",
        "permanent_api_failure",
        "malformed_response",
        "duplicate_tool_request",
        "delayed_response",
        "permission_denial",
    ]
    tool: Tool
    trigger: int = Field(default=1, ge=1, le=16)
    count: int = Field(default=1, ge=1, le=3)
    delay_ms: int = Field(default=0, ge=0, le=2000)

    @model_validator(mode="after")
    def delay(self):
        if self.kind == "delayed_response" and not self.delay_ms:
            raise ValueError("delayed response needs a delay")
        return self


class Limits(Contract):
    max_tool_calls: int = Field(default=12, ge=1, le=32)
    wall_ms: int = Field(default=5000, ge=10, le=10000)


Evidence = Literal[
    "effect",
    "read",
    "rejection",
    "ownership_read",
    "invalid_request",
    "balance_read_or_rejection",
    "blocked_policy",
    "blocked_approval",
    "blocked_budget",
    "read_unavailable",
    "blocked_read",
    "blocked_tool",
]


class ExpectedOutcome(Contract):
    final_state: dict[str, Any]
    effect_counts: dict[Effect, int]
    resolution: Literal["completed", "rejected", "blocked"]
    evidence: Evidence

    @model_validator(mode="after")
    def valid_outcome(self):
        if any(v < 0 for v in self.effect_counts.values()):
            raise ValueError("negative alternative effect count")
        if set(self.final_state) - set(Order.model_fields):
            raise ValueError("unknown alternative order field")
        return self


class Assertions(Identity):
    final_state: dict[str, Any]
    effect_counts: dict[Effect, int]
    forbidden_effects: list[Effect]
    resolution: Literal["completed", "rejected", "blocked"]
    evidence: Evidence | None = None
    alternatives: list[ExpectedOutcome] = Field(default_factory=list, max_length=2)
    effect_count_bounds: dict[Literal["refund"], list[int]] = Field(
        default_factory=dict
    )
    recovery_required: bool = False
    fault_observed: bool = False

    @model_validator(mode="after")
    def valid(self):
        for kind, bounds in self.effect_count_bounds.items():
            if (
                len(bounds) != 2
                or not 1 <= bounds[0] <= bounds[1] <= 32
                or kind in self.forbidden_effects
                or self.effect_counts.get(kind, 0) != bounds[0]
            ):
                raise ValueError("invalid positive refund installment bounds")
        if any(v < 0 for v in self.effect_counts.values()):
            raise ValueError("negative effect count")
        if any(self.effect_counts.get(k, 0) for k in self.forbidden_effects):
            raise ValueError("forbidden effect is expected")
        if set(self.final_state) - set(Order.model_fields):
            raise ValueError("unknown asserted order field")
        if any(
            o.effect_counts.get(k, 0)
            for o in self.alternatives
            for k in self.forbidden_effects
        ):
            raise ValueError("alternative expects a forbidden effect")
        return self


class Scenario(Identity):
    title: str = Field(min_length=1, max_length=120)
    split: Literal["development", "held-out"]
    category: Literal[
        "success",
        "invalid-request",
        "permission",
        "approval",
        "fault",
        "recovery",
        "limits",
    ]
    fixture: Fixture
    request: UserRequest
    permissions: Permissions
    fault: FaultProfile
    assertions: Assertions
    limits: Limits
    review: str = Field(min_length=20, max_length=1000)
    reference_outcome: Literal["PASS"] = "PASS"

    @model_validator(mode="after")
    def consistent(self):
        if self.request.order_id != self.fixture.order.order_id:
            raise ValueError("request and fixture order identity differ")
        if self.fault.kind != "none" and self.fault.tool not in self.permissions.tools:
            raise ValueError("fault target must be an available tool")
        if self.assertions.recovery_required and self.fault.kind not in {
            "timeout_before_commit",
            "timeout_after_commit",
            "temporary_api_failure",
            "malformed_response",
            "delayed_response",
        }:
            raise ValueError("recovery assertion requires a recoverable fault")
        return self


class Dataset(Identity):
    title: str
    publication: Literal["published"]
    review_method: str
    scenarios: list[Scenario] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def unique(self):
        ids = [s.id for s in self.scenarios]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate scenario identity")
        return self


class ModelConfiguration(Contract):
    provider: Literal["openai-compatible"]
    model: str = Field(min_length=1, max_length=120)
    base_url: str = Field(pattern=r"^https?://[^?#@]+$")
    api_key_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,100}$")
    tool_calling: Literal[True]
    timeout_seconds: float = Field(default=10.0, gt=0, le=30)
    temperature: float = Field(default=0.0, ge=0, le=2)
    top_p: float = Field(default=1.0, gt=0, le=1)
    max_output_tokens: int = Field(default=256, ge=16, le=4096)
    max_requests: int = Field(default=4, ge=1, le=20)
    max_tool_calls: int = Field(default=10, ge=1, le=32)
    max_tokens: int = Field(default=64000, ge=128, le=100000)
    max_estimated_cost_usd: float = Field(default=0.01, gt=0, le=5)
    input_usd_per_million: float = Field(gt=0, le=1000)
    output_usd_per_million: float = Field(gt=0, le=10000)
    pricing_source: str = Field(pattern=r"^https://", max_length=500)
    pricing_checked_at: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


class AgentConfiguration(Identity):
    title: str
    policy: Literal["reconcile", "blind-retry", "llm"]
    model_configuration: ModelConfiguration | dict[str, Any]
    prompt: str = Field(min_length=20, max_length=2000)
    prompt_version: str = Field(pattern=r"^[a-z][a-z0-9-]{1,63}$")
    tool_schema_version: Literal["support-tools-v1"]
    max_retries: int = Field(ge=0, le=3)

    @model_validator(mode="after")
    def offline(self):
        if self.policy == "llm":
            self.model_configuration = ModelConfiguration.model_validate(
                self.model_configuration
            )
            return self
        if self.model_configuration != {
            "provider": "offline",
            "model": "pydantic-function-model",
            "external_calls": False,
        }:
            raise ValueError("only the offline reference model is supported")
        return self


class PublicContext(Contract):
    """The only task data supplied to tested agent code."""

    tool_schema: dict[Tool, dict[str, Any]] = Field(default_factory=dict)
    request: UserRequest
    permissions: Permissions
    limits: Limits


class ToolArguments(Contract):
    order_id: str = Field(min_length=1, max_length=64)


class RefundArguments(ToolArguments):
    amount_cents: int = Field(gt=0, le=1000000)


class AddressArguments(ToolArguments):
    address: str = Field(min_length=1, max_length=200, pattern=r".*\S.*")


class ToolResponse(Contract):
    ok: bool
    order: Order | None = None
    error: str | None = None
    retryable: bool = False
    approved: bool | None = None


class AgentOutcome(Contract):
    resolution: Literal["completed", "rejected", "blocked", "limit", "invalid"]


class AssertionEvidence(Contract):
    id: str
    verdict: Verdict
    expected: Any
    actual: Any
    event_sequences: list[int]


class Assessment(Contract):
    task: Verdict
    policy: Verdict
    provider: Literal["not_used", "ok", "error", "unknown"]
    harness: Literal["complete", "incomplete", "conflicting"]
    execution: Literal[
        "completed", "provider", "evaluator", "infrastructure", "cancelled"
    ]
    overall: Verdict
    issues: list[str]


class Provenance(Contract):
    scenario_id: str
    scenario_version: int
    scenario_sha256: str
    dataset: dict[str, Any]
    evaluator: dict[str, Any]
    agent: dict[str, Any]
    attempt: dict[str, Any]
    evidence_sha256: str
    receipt_count: int
    references_valid: bool


class EvaluationResult(Contract):
    trial_id: str
    scenario_id: str
    verdict: Verdict
    failure_category: (
        Literal["agent", "provider", "evaluator", "infrastructure", "cancelled"] | None
    )
    evaluator_version: Literal[
        "support-evaluator-v1",
        "support-evaluator-v2",
        "support-evaluator-v3",
        "support-evaluator-v4",
    ]
    evaluator_sha256: str
    assertions: list[AssertionEvidence]
    assessment: Assessment | None = None
    provenance: Provenance | None = None
    final_state: dict[str, Any]
    effect_counts: dict[str, int]
    unsafe_actions: int
    valid_tool_calls: int
    tool_calls: int
    recovery_eligible: bool
    recovery_succeeded: bool | None
    duplicate_side_effects: bool
    latency_ms: float | None = Field(ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    inference_cost_usd: float | None = Field(default=None, ge=0)
    model_requests: list[dict[str, Any]] = Field(default_factory=list)
    estimated_cost_usd: float | None = Field(default=None, ge=0)
    reported_cost_usd: float | None = Field(default=None, ge=0)
    provider_error: str | None = None
    trace_id: str
    error: str | None = None

    @model_validator(mode="after")
    def coherent_result(self):
        if self.evaluator_version == "support-evaluator-v4":
            if self.assessment is None or self.provenance is None:
                raise ValueError("validated results require dimensions and provenance")
            if self.assessment.overall != self.verdict or (
                self.assessment.policy == "FAIL" and self.verdict != "FAIL"
            ):
                raise ValueError("assessment precedence disagrees with verdict")
            if self.verdict == "PASS" and (
                self.assessment.harness != "complete"
                or self.assessment.execution != "completed"
                or self.assessment.task != "PASS"
                or self.assessment.policy != "PASS"
                or not self.provenance.references_valid
            ):
                raise ValueError(
                    "PASS requires complete evidence and accepted outcomes"
                )
        allowed = {
            "PASS": {None},
            "FAIL": {"agent"},
            "INDETERMINATE": {"provider", "evaluator", "infrastructure", "cancelled"},
        }
        if self.failure_category not in allowed[self.verdict]:
            raise ValueError("verdict and failure category disagree")
        if (
            not 0 <= self.valid_tool_calls <= self.tool_calls
            or not 0 <= self.unsafe_actions <= self.tool_calls
        ):
            raise ValueError("invalid tool call counts")
        if any(v < 0 for v in self.effect_counts.values()):
            raise ValueError("negative effect count")
        return self


class EvaluationConfiguration(Contract):
    dataset_id: Literal["customer-support"]
    dataset_version: Literal[2, 3, 4, 5]
    agent_id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,63}$")
    agent_version: Literal[1]
    split: Literal["development", "held-out", "all"] = "development"
    trials: int = Field(default=1, ge=1, le=5)
    scenario_ids: list[str] = Field(default_factory=list, max_length=50)


class VersionReference(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str
    version: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    content: dict[str, Any]


class EvaluationManifest(Contract):
    id: str
    dataset: VersionReference
    agent: VersionReference
    evaluator: VersionReference
    tool_schema: VersionReference
    tool_schema_version: Literal["support-tools-v1"]
    split: Literal["development", "held-out", "all"]
    trials: int = Field(ge=1, le=5)
    scenario_ids: list[str] = Field(min_length=1, max_length=50)
    traceparent: str
