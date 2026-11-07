"""Offline policies. This module has no access to Scenario, Fixture, or Assertions."""

import json

from pydantic import ValidationError
from pydantic_ai import Agent, models
from pydantic_ai.capabilities import Instrumentation
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.instrumented import InstrumentationSettings
from pydantic_ai.usage import UsageLimits

from .contracts import AgentConfiguration, AgentOutcome, PublicContext, ToolResponse

models.ALLOW_MODEL_REQUESTS = False


class Policy:
    def __init__(self, context: PublicContext, configuration: AgentConfiguration):
        self.context, self.configuration = context, configuration
        self.safe = configuration.policy == "reconcile"
        self.stage = "initial"
        self.calls = 0
        self.retries = 0
        self.goal = {}
        self.mutation = ""

    def finish(self, resolution):
        return ModelResponse(
            parts=[TextPart(AgentOutcome(resolution=resolution).model_dump_json())]
        )

    def call(self, tool):
        if self.safe and (
            tool not in self.context.permissions.tools
            or self.calls >= self.context.limits.max_tool_calls
        ):
            return self.finish("blocked")
        self.calls += 1
        args = {"order_id": self.context.request.order_id}
        if tool == "refund_order":
            args["amount_cents"] = self.context.request.amount_cents
        if tool == "update_address":
            args["address"] = self.context.request.address
        return ModelResponse(
            parts=[ToolCallPart("support_tool", {"name": tool, "arguments": args})]
        )

    def __call__(self, messages, info):
        request = self.context.request
        returns = [
            p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)
        ]
        if not returns:
            if self.safe and (
                (
                    request.action == "refund"
                    and (request.amount_cents is None or request.amount_cents <= 0)
                )
                or (
                    request.action == "address"
                    and (request.address is None or not request.address.strip())
                )
            ):
                return self.finish("rejected")
            self.stage = "read"
            return self.call("get_order")
        try:
            response = ToolResponse.model_validate(returns[-1].content)
        except ValidationError:
            response = ToolResponse(ok=False, error="unknown_outcome", retryable=True)
        if self.stage in {"read", "reconcile"}:
            if not response.ok or response.order is None:
                if response.retryable and self.retries < self.configuration.max_retries:
                    self.retries += 1
                    return self.call("get_order")
                return self.finish(
                    "rejected" if response.error == "not_found" else "blocked"
                )
            order = response.order
            if self.stage == "reconcile":
                if all(getattr(order, k) == v for k, v in self.goal.items()):
                    return self.finish("completed")
                if self.retries >= self.configuration.max_retries:
                    return self.finish("blocked")
                self.retries += 1
                self.stage = "mutate"
                return self.call(self.mutation)
            if self.safe and order.customer_id != request.customer_id:
                return self.finish("rejected")
            if request.action == "status":
                return self.finish("completed")
            self.mutation = {
                "cancel": "cancel_order",
                "refund": "refund_order",
                "address": "update_address",
            }[request.action]
            if request.action == "cancel":
                if self.safe and order.status == "shipped":
                    return self.finish("rejected")
                self.goal = {"status": "cancelled"}
            elif request.action == "refund":
                if (
                    self.safe
                    and request.amount_cents > order.total_cents - order.refunded_cents
                ):
                    return self.finish("rejected")
                self.goal = {
                    "refunded_cents": order.refunded_cents + (request.amount_cents or 0)
                }
            else:
                self.goal = {"address": request.address}
            if self.safe and all(getattr(order, k) == v for k, v in self.goal.items()):
                return self.finish("completed")
            if (
                self.safe
                and self.context.permissions.approval_required
                and not order.approved
            ):
                self.stage = "approve"
                return self.call("request_approval")
            self.stage = "mutate"
            return self.call(self.mutation)
        if self.stage == "approve":
            if not response.ok or not response.approved:
                return self.finish("blocked")
            self.stage = "mutate"
            return self.call(self.mutation)
        if response.ok:
            return self.finish("completed")
        if response.error in {"timeout", "unknown_outcome"} and self.safe:
            self.stage = "reconcile"
            return self.call("get_order")
        if response.retryable and self.retries < self.configuration.max_retries:
            self.retries += 1
            return self.call(self.mutation)
        return self.finish("blocked")


async def run_reference(
    context: PublicContext, configuration: AgentConfiguration, tool_call
):
    policy = Policy(context, configuration)
    agent = Agent(
        FunctionModel(policy.__call__),
        instructions=configuration.prompt,
        capabilities=[
            Instrumentation(settings=InstrumentationSettings(include_content=False))
        ],
    )

    @agent.tool_plain
    async def support_tool(name: str, arguments: dict) -> dict:
        """Call an available order tool with arguments that match support-tools-v1."""
        return await tool_call(name, arguments)

    result = await agent.run(
        context.model_dump_json(),
        usage_limits=UsageLimits(request_limit=40, tool_calls_limit=34),
    )
    return AgentOutcome.model_validate(json.loads(result.output))
