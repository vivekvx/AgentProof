"""Trial-scoped order capabilities with atomic response and effect ledgers."""

import asyncio
import copy
import json
import math
import re
from datetime import UTC, datetime
from typing import Any

from opentelemetry import trace
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from ..reliability import EvaluatorFailure
from ..telemetry import tracer
from . import evidence
from .contracts import (
    AddressArguments,
    Order,
    RefundArguments,
    Scenario,
    ToolArguments,
    ToolResponse,
)


class AgentLimit(RuntimeError):
    pass


class TrialStopped(RuntimeError):
    pass


class ReplayMismatch(EvaluatorFailure):
    pass


ARGUMENTS = {
    "get_order": ToolArguments,
    "cancel_order": ToolArguments,
    "refund_order": RefundArguments,
    "update_address": AddressArguments,
    "request_approval": ToolArguments,
}
EFFECTS = {
    "cancel_order": "cancellation",
    "refund_order": "refund",
    "update_address": "address_change",
    "request_approval": "approval",
}


def snapshot_input(value):
    """Bound work and retained evidence before schema validation or serialization."""
    remaining = 128

    def visit(item, depth=0):
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > 5:
            raise ValueError("input bounds")
        kind = type(item)
        if item is None or kind is bool:
            return item
        if kind is str and len(item) <= 2048:
            return item
        if kind is int and -(2**63) <= item < 2**63:
            return item
        if kind is float and math.isfinite(item):
            return item
        if kind is list and len(item) <= 32:
            return [visit(v, depth + 1) for v in item]
        if (
            kind is dict
            and len(item) <= 32
            and all(type(k) is str and len(k) <= 64 for k in item)
        ):
            return {k: visit(v, depth + 1) for k, v in item.items()}
        raise ValueError("input type or size")

    try:
        result = visit(value)
        if len(json.dumps(result).encode()) > 4096:
            raise ValueError("input bytes")
        return result, True
    except ValueError:
        return {"_rejected_input": "non_json_or_input_bounds"}, False


class OrderSimulator:
    def __init__(
        self,
        conn,
        trial_id: str,
        scenario: Scenario,
        attempt: int = 1,
        record_receipts=False,
    ):
        row = conn.execute(
            "SELECT t.scenario,e.manifest FROM benchmark_trials t JOIN benchmark_evaluations e ON e.id=t.evaluation_id WHERE t.id=%s",
            (trial_id,),
        ).fetchone()
        if row is None:
            raise EvaluatorFailure("unknown broker trial")
        authority = Scenario.model_validate(row[0])
        supplied = scenario.model_copy(deep=True)
        supplied.limits = authority.limits.model_copy(deep=True)
        if supplied != authority:
            raise EvaluatorFailure("broker authority differs from stored trial")
        authority.limits.max_tool_calls = min(
            authority.limits.max_tool_calls, scenario.limits.max_tool_calls
        )
        authority.limits.wall_ms = min(
            authority.limits.wall_ms, scenario.limits.wall_ms
        )
        self.conn, self.trial_id, self.scenario, self.attempt = (
            conn,
            trial_id,
            authority,
            attempt,
        )
        self.sequence = 0
        # A caller cannot disable the witness required by the stored publication.
        self.record_receipts = record_receipts or row[1]["evaluator"]["version"] == 4

    async def call(
        self,
        tool: str,
        arguments: dict[str, Any],
        *,
        sequence: int | None = None,
        operation_id: str | None = None,
        record_requested_tool: bool = False,
    ) -> dict:
        if sequence is not None:
            if type(sequence) is not int or sequence < 1:
                raise ValueError("invalid operation sequence")
            self.sequence = sequence - 1
        requested_tool = tool
        tool = (
            tool
            if type(tool) is str and re.fullmatch(r"[a-z_][a-z0-9_]{0,63}", tool)
            else "invalid_tool"
        )
        arguments, bounded = snapshot_input(arguments)
        valid = bounded
        try:
            if tool not in ARGUMENTS:
                raise ValueError("unknown tool")
            parsed = ARGUMENTS[tool].model_validate(arguments)
            if parsed.order_id != self.scenario.request.order_id:
                raise ValueError("tool order outside trial")
        except (ValidationError, ValueError):
            valid = False
        if not valid:
            arguments = {
                "_rejected_arguments": {
                    "sha256": evidence.digest(arguments),
                    "reason": "invalid_tool_arguments",
                }
            }
        try:
            return await self._dispatch(
                tool,
                arguments,
                valid,
                operation_id,
                requested_tool if record_requested_tool else None,
            )
        except (TrialStopped, ReplayMismatch, EvaluatorFailure) as error:
            reason = (
                "terminal"
                if isinstance(error, TrialStopped)
                else "replay_mismatch"
                if isinstance(error, ReplayMismatch)
                else "evidence_integrity"
            )
            self.conn.execute(
                "INSERT INTO benchmark_broker_rejections(trial_id,sequence,attempt,reason,tool,arguments_sha256) VALUES(%s,%s,%s,%s,%s,%s)",
                (
                    self.trial_id,
                    self.sequence,
                    self.attempt,
                    reason,
                    tool,
                    evidence.digest(arguments),
                ),
            )
            with tracer.start_as_current_span(
                "benchmark.broker.rejected",
                attributes={
                    "agentproof.trial_id": self.trial_id,
                    "agentproof.rejection_reason": reason,
                },
            ):
                pass
            raise

    async def _dispatch(
        self, tool, arguments, valid, operation_id=None, requested_tool=None
    ):
        self.sequence += 1
        operation_key = f"{self.trial_id}:support-tools-v1:{self.sequence}"
        with tracer.start_as_current_span(
            "benchmark.tool." + tool,
            attributes={
                "agentproof.trial_id": self.trial_id,
                "agentproof.scenario.id": self.scenario.id,
                "agentproof.operation_key": operation_key,
                "temporal.activity.attempt": self.attempt,
            },
        ) as span:
            with self.conn.transaction():
                parent = self.conn.execute(
                    "SELECT e.status FROM benchmark_evaluations e JOIN benchmark_trials t ON t.evaluation_id=e.id WHERE t.id=%s FOR SHARE OF e",
                    (self.trial_id,),
                ).fetchone()[0]
                row = self.conn.execute(
                    "SELECT state,result FROM benchmark_trials WHERE id=%s FOR UPDATE",
                    (self.trial_id,),
                ).fetchone()
                if parent in {"completed", "cancelled"} or row[1] is not None:
                    raise TrialStopped("trial already terminal")
                if self.record_receipts:
                    evidence.require_intact(
                        self.conn,
                        self.trial_id,
                        self.scenario.fixture.order.model_dump(),
                    )
                old = self.conn.execute(
                    "SELECT payload FROM benchmark_events WHERE trial_id=%s AND sequence=%s",
                    (self.trial_id, self.sequence),
                ).fetchone()
                if old:
                    event = old[0]
                    if (
                        event["tool"] != tool
                        or event["arguments"] != arguments
                        or event.get("operation_id") != operation_id
                    ):
                        raise ReplayMismatch("benchmark tool replay mismatch")
                    span.set_attribute("agentproof.replayed", True)
                else:
                    state = Order.model_validate(row[0]).model_dump()
                    authorized = tool in self.scenario.permissions.tools
                    if tool in EFFECTS:
                        authorized = (
                            authorized
                            and self.scenario.request.customer_id
                            == state["customer_id"]
                        )
                    if tool in {"cancel_order", "refund_order", "update_address"}:
                        authorized = authorized and (
                            not self.scenario.permissions.approval_required
                            or state["approved"]
                        )
                    limit = self.sequence > self.scenario.limits.max_tool_calls
                    invocation = (
                        self.conn.execute(
                            "SELECT count(*) FROM benchmark_events WHERE trial_id=%s AND payload->>'tool'=%s",
                            (self.trial_id, tool),
                        ).fetchone()[0]
                        + 1
                    )
                    f = self.scenario.fault
                    fault = (
                        f.kind
                        if f.kind != "none"
                        and f.tool == tool
                        and invocation >= f.trigger
                        and (
                            f.kind == "permanent_api_failure"
                            or invocation < f.trigger + f.count
                        )
                        else "none"
                    )
                    response = ToolResponse(
                        ok=False, error="invalid_arguments"
                    ).model_dump()
                    effect = None
                    if limit:
                        response = ToolResponse(
                            ok=False, error="execution_limit"
                        ).model_dump()
                        fault = "none"
                    elif not valid:
                        fault = "none"
                    elif not authorized:
                        response = ToolResponse(
                            ok=False, error="permission_denied"
                        ).model_dump()
                        fault = "none"
                    elif fault in {
                        "timeout_before_commit",
                        "temporary_api_failure",
                        "permanent_api_failure",
                        "permission_denial",
                    }:
                        error = {
                            "timeout_before_commit": "timeout",
                            "temporary_api_failure": "unavailable",
                            "permanent_api_failure": "unavailable",
                            "permission_denial": "permission_denied",
                        }[fault]
                        response = ToolResponse(
                            ok=False,
                            error=error,
                            retryable=fault
                            in {"timeout_before_commit", "temporary_api_failure"},
                        ).model_dump()
                    elif not state["exists"]:
                        response = ToolResponse(
                            ok=False, error="not_found"
                        ).model_dump()
                    else:
                        response, effect = self._apply(
                            tool, arguments, state, operation_key
                        )
                        # A duplicated transport delivery has the same operation key.
                        # The second delivery must not apply a second state mutation.
                        if fault == "duplicate_tool_request":
                            self._apply(tool, arguments, state, operation_key)
                        if fault == "timeout_after_commit":
                            response = ToolResponse(
                                ok=False, error="timeout", retryable=True
                            ).model_dump()
                        elif fault == "malformed_response":
                            response = {"malformed": True}
                    ctx = trace.get_current_span().get_span_context()
                    event = {
                        "schema_version": "2.0",
                        "sequence": self.sequence,
                        "tool": tool,
                        "arguments": arguments,
                        "arguments_valid": valid,
                        "authorized": authorized,
                        "limit_exceeded": limit,
                        "response": response,
                        "fault": fault,
                        "effect": effect,
                        "operation_key": operation_key,
                        "transport_requests": 2
                        if fault == "duplicate_tool_request"
                        else 1,
                        "delay_ms": f.delay_ms if fault == "delayed_response" else 0,
                        "timestamp": datetime.now(UTC).isoformat(),
                        "trace_id": f"{ctx.trace_id:032x}",
                        "span_id": f"{ctx.span_id:016x}",
                        "attempt": self.attempt,
                    }
                    if operation_id is not None:
                        event["operation_id"] = operation_id
                    if requested_tool is not None:
                        event["requested_tool"] = requested_tool
                    self.conn.execute(
                        "INSERT INTO benchmark_events VALUES(%s,%s,%s,%s)",
                        (self.trial_id, self.sequence, operation_key, Jsonb(event)),
                    )
                    if self.record_receipts:
                        evidence.append(self.conn, self.trial_id, event, state)
            span.set_attribute("agentproof.fault", event["fault"])
            if event["limit_exceeded"]:
                raise AgentLimit("tool-call budget exceeded")
            if event["delay_ms"]:
                await asyncio.sleep(event["delay_ms"] / 1000)
            # No private assertions, fault identity, commit evidence, or ledger metadata reach the agent.
            return copy.deepcopy(event["response"])

    def _apply(self, tool, args, state, key):
        if tool == "get_order":
            return ToolResponse(
                ok=True, order=Order.model_validate(state)
            ).model_dump(), None
        if tool == "cancel_order" and state["status"] == "shipped":
            return ToolResponse(ok=False, error="invalid_request").model_dump(), None
        if (
            tool == "refund_order"
            and args["amount_cents"] > state["total_cents"] - state["refunded_cents"]
        ):
            return ToolResponse(ok=False, error="invalid_request").model_dump(), None
        if tool == "request_approval" and not self.scenario.request.user_approved:
            return ToolResponse(
                ok=False, error="approval_denied", approved=False
            ).model_dump(), None
        effect = EFFECTS[tool]
        inserted = self.conn.execute(
            "INSERT INTO benchmark_effects VALUES(%s,%s,%s,%s) ON CONFLICT(operation_key) DO NOTHING RETURNING operation_key",
            (self.trial_id, key, effect, Jsonb({"tool": tool, "arguments": args})),
        ).fetchone()
        if inserted:
            if tool == "cancel_order":
                state["status"] = "cancelled"
            if tool == "refund_order":
                state["refunded_cents"] += args["amount_cents"]
            if tool == "update_address":
                state["address"] = args["address"]
            if tool == "request_approval":
                state["approved"] = True
            self.conn.execute(
                "UPDATE benchmark_trials SET state=%s WHERE id=%s",
                (Jsonb(state), self.trial_id),
            )
        return ToolResponse(
            ok=True,
            order=Order.model_validate(state),
            approved=state["approved"] if tool == "request_approval" else None,
        ).model_dump(), effect
