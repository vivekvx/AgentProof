"""Version 3 interaction cases. Historical scenario contracts stay unchanged."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .contracts import (
    AgentConfiguration,
    AgentOutcome,
    EvaluationConfiguration,
    EvaluationManifest,
    EvaluationResult,
    Fixture,
    Limits,
)
from .coverage import Obligation


class InteractionContract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal["3.0"] = "3.0"


class InteractionScenario(InteractionContract):
    id: str = Field(pattern=r"^t07-[0-9]{3}$")
    version: Literal[1]
    title: str = Field(min_length=1, max_length=120)
    split: Literal["development", "held-out"]
    category: Literal["interaction"]
    family: str = Field(pattern=r"^[a-z][a-z0-9-]{1,63}$")
    fixture: Fixture
    initial_user: str = Field(min_length=10, max_length=500)
    requester_id: str = Field(min_length=1, max_length=64)
    scripted_reply: str | None = Field(default=None, max_length=300)
    clarification_field: Literal["amount_cents"] | None = None
    permissions: list[
        Literal[
            "get_order",
            "cancel_order",
            "refund_order",
            "update_address",
            "request_approval",
        ]
    ] = Field(min_length=1, max_length=5)
    approval_required: bool
    user_approved: bool
    must_read: bool
    untrusted_tool_note: str | None = Field(default=None, max_length=500)
    obligations: list[Obligation] = Field(min_length=1, max_length=3)
    limits: Limits
    fault: Literal["none"] = "none"
    review: str = Field(min_length=20, max_length=1000)
    reference_outcome: Literal["PASS"] = "PASS"

    @property
    def order(self):
        return self.fixture.order

    @property
    def max_tool_calls(self):
        return self.limits.max_tool_calls

    @model_validator(mode="after")
    def consistent(self):
        if self.requester_id != self.order.customer_id:
            raise ValueError("requester and fixture customer differ")
        if len(set(self.permissions)) != len(self.permissions):
            raise ValueError("duplicate permission")
        if len({o.id for o in self.obligations}) != len(self.obligations):
            raise ValueError("duplicate obligation")
        signatures = [(o.effect, o.amount_cents, o.address) for o in self.obligations]
        if len(set(signatures)) != len(signatures):
            raise ValueError("ambiguous obligation identity")
        if (self.scripted_reply is None) != (self.clarification_field is None):
            raise ValueError("reply and clarification field must be paired")
        required = {
            "cancellation": "cancel_order",
            "refund": "refund_order",
            "address_change": "update_address",
        }
        if any(required[o.effect] not in self.permissions for o in self.obligations):
            raise ValueError("obligation needs an available tool")
        if self.must_read and "get_order" not in self.permissions:
            raise ValueError("required read tool is unavailable")
        if self.approval_required and "request_approval" not in self.permissions:
            raise ValueError("required approval tool is unavailable")
        return self


class InteractionDataset(InteractionContract):
    id: Literal["customer-support"]
    version: Literal[6]
    title: str
    publication: Literal["published"]
    review_method: str
    source_dataset_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_evaluator_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    authoring_fixture_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    authoring_oracle_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    review_status: Literal["automated-review-development-only"]
    split_policy: str = Field(min_length=20)
    family_links: dict[str, list[str]]
    scenarios: list[InteractionScenario] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def unique(self):
        if len({s.id for s in self.scenarios}) != len(self.scenarios):
            raise ValueError("duplicate scenario identity")
        if len({s.family for s in self.scenarios}) != len(self.scenarios):
            raise ValueError("duplicate template family")
        if set(self.family_links) != {s.id for s in self.scenarios}:
            raise ValueError("missing family link disposition")
        if any(not links for links in self.family_links.values()):
            raise ValueError("family links require a prior-case review")
        return self


class InteractionAgentConfiguration(AgentConfiguration):
    schema_version: Literal["3.0"] = "3.0"
    policy: Literal["interaction-reference"]
    tool_schema_version: Literal["support-tools-v2"]


class InteractionOutcome(AgentOutcome):
    schema_version: Literal["3.0"] = "3.0"
    resolution: Literal[
        "completed", "rejected", "blocked", "limit", "invalid", "clarification_required"
    ]


class InteractionConfiguration(EvaluationConfiguration):
    schema_version: Literal["3.0"] = "3.0"
    dataset_version: Literal[6]


class InteractionManifest(EvaluationManifest):
    schema_version: Literal["3.0"] = "3.0"
    tool_schema_version: Literal["support-tools-v2"]


class InteractionResult(EvaluationResult):
    schema_version: Literal["3.0"] = "3.0"
    evaluator_version: Literal["support-evaluator-v5"]

    @model_validator(mode="after")
    def validated_outcome(self):
        if self.assessment is None or self.provenance is None:
            raise ValueError("interaction result needs assessment and provenance")
        if self.assessment.overall != self.verdict:
            raise ValueError("assessment and verdict disagree")
        if self.verdict == "PASS" and (
            self.assessment.task != "PASS"
            or self.assessment.policy != "PASS"
            or self.assessment.harness != "complete"
            or self.assessment.execution != "completed"
            or not self.provenance.references_valid
        ):
            raise ValueError("PASS requires complete trusted evidence")
        return self
