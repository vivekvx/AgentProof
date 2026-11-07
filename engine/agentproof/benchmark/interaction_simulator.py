"""Version 3 trial broker. Only this module produces trusted simulated replies."""

import copy
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from opentelemetry import trace
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from ..reliability import EvaluatorFailure
from ..telemetry import tracer
from . import evidence
from .contracts import AddressArguments, Order, RefundArguments, ToolArguments
from .interaction_contracts import InteractionScenario
from .simulator import AgentLimit, ReplayMismatch, TrialStopped, snapshot_input

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


@dataclass(frozen=True)
class TrustedUserReply:
    """A harness reply delivered on a separate channel, never as a tool result."""

    text: str
    turn_id: str
    source: str = "harness"


class InteractionSimulator:
    def __init__(self, conn, trial_id: str, scenario: InteractionScenario, attempt=1):
        row = conn.execute(
            "SELECT t.scenario,e.manifest FROM benchmark_trials t JOIN benchmark_evaluations e ON e.id=t.evaluation_id WHERE t.id=%s",
            (trial_id,),
        ).fetchone()
        if row is None:
            raise EvaluatorFailure("unknown interaction trial")
        authority = InteractionScenario.model_validate(row[0])
        if authority != scenario:
            raise EvaluatorFailure(
                "interaction broker authority differs from stored trial"
            )
        if row[1]["evaluator"]["version"] != 5:
            raise EvaluatorFailure("interaction evaluator reference mismatch")
        self.conn, self.trial_id, self.scenario, self.attempt = (
            conn,
            trial_id,
            authority,
            attempt,
        )
        self.sequence = 0
        self.read = False
        self.asked = False
        self.replied = False

    def _key(self, sequence):
        return f"{self.trial_id}:support-tools-v2:{sequence}"

    def _record(
        self, kind, tool, arguments, response, effect, state, *, source, **extra
    ):
        try:
            return self._record_inner(
                kind, tool, arguments, response, effect, state, source=source, **extra
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
                    tool[:64],
                    evidence.digest(arguments),
                ),
            )
            raise

    def _record_inner(
        self, kind, tool, arguments, response, effect, state, *, source, **extra
    ):
        self.sequence += 1
        key = self._key(self.sequence)
        with tracer.start_as_current_span(
            "benchmark.interaction." + kind,
            attributes={
                "agentproof.trial_id": self.trial_id,
                "agentproof.scenario.id": self.scenario.id,
                "agentproof.operation_key": key,
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
                require_interaction_intact(
                    self.conn, self.trial_id, self.scenario.order.model_dump()
                )
                old = self.conn.execute(
                    "SELECT payload FROM benchmark_events WHERE trial_id=%s AND sequence=%s",
                    (self.trial_id, self.sequence),
                ).fetchone()
                if old:
                    event = old[0]
                    expected = (kind, tool, arguments, source, extra)
                    actual = (
                        event["kind"],
                        event["tool"],
                        event["arguments"],
                        event["source"],
                        event["metadata"],
                    )
                    if expected != actual:
                        raise ReplayMismatch("interaction decision replay mismatch")
                    span.set_attribute("agentproof.replayed", True)
                    return copy.deepcopy(event["response"]), event
                if effect:
                    effect_payload = {"tool": tool, "arguments": arguments}
                    self.conn.execute(
                        "INSERT INTO benchmark_effects VALUES(%s,%s,%s,%s)",
                        (self.trial_id, key, effect, Jsonb(effect_payload)),
                    )
                    self.conn.execute(
                        "UPDATE benchmark_trials SET state=%s WHERE id=%s",
                        (Jsonb(state), self.trial_id),
                    )
                ctx = trace.get_current_span().get_span_context()
                event = {
                    "schema_version": "3.0",
                    "sequence": self.sequence,
                    "operation_key": key,
                    "kind": kind,
                    "tool": tool,
                    "arguments": arguments,
                    "response": response,
                    "effect": effect,
                    "source": source,
                    "metadata": extra,
                    "attempt": self.attempt,
                    "timestamp": datetime.now(UTC).isoformat(),
                    "trace_id": f"{ctx.trace_id:032x}",
                    "span_id": f"{ctx.span_id:016x}",
                }
                self.conn.execute(
                    "INSERT INTO benchmark_events VALUES(%s,%s,%s,%s)",
                    (self.trial_id, self.sequence, key, Jsonb(event)),
                )
                evidence.append(self.conn, self.trial_id, event, state)
            return copy.deepcopy(response), event

    async def ask(self, field: str):
        if field != "amount_cents" or self.scenario.clarification_field != field:
            raise ValueError("unsupported clarification")
        state = self._state()
        response, _ = self._record(
            "ask",
            "ask_user",
            {"field": field},
            {"pending": True},
            None,
            state,
            source="agent",
        )
        self.asked = True
        return response

    async def receive_user_reply(self):
        if not self.asked or self.replied or self.scenario.scripted_reply is None:
            return None
        state = self._state()
        response, event = self._record(
            "user_reply",
            "trusted_user_turn",
            {},
            {"text": self.scenario.scripted_reply},
            None,
            state,
            source="harness",
        )
        self.replied = True
        return TrustedUserReply(response["text"], event["operation_key"])

    async def redeliver(self, operation_key: str):
        old = self.conn.execute(
            "SELECT payload FROM benchmark_events WHERE trial_id=%s AND operation_key=%s",
            (self.trial_id, operation_key),
        ).fetchone()
        if not old or old[0]["kind"] != "tool":
            raise ValueError("unknown transport operation")
        response, _ = self._record(
            "redeliver",
            "transport_delivery",
            {},
            old[0]["response"],
            None,
            self._state(),
            source="transport",
            target_operation_key=operation_key,
        )
        return response

    def _state(self):
        return Order.model_validate(
            self.conn.execute(
                "SELECT state FROM benchmark_trials WHERE id=%s", (self.trial_id,)
            ).fetchone()[0]
        ).model_dump()

    async def call(self, tool: str, arguments: dict[str, Any], *, claimed_key=None):
        tool = (
            tool
            if type(tool) is str and re.fullmatch(r"[a-z_][a-z0-9_]{0,63}", tool)
            else "invalid_tool"
        )
        raw, bounded = snapshot_input(arguments)
        valid = bounded and tool in ARGUMENTS
        if valid:
            try:
                parsed = ARGUMENTS[tool].model_validate(raw)
                valid = parsed.order_id == self.scenario.order.order_id
            except (ValidationError, ValueError, AttributeError):
                valid = False
        if not valid:
            raw = {"_rejected_arguments": {"sha256": evidence.digest(raw)}}
        if claimed_key is not None and (
            type(claimed_key) is not str or len(claimed_key) > 80
        ):
            claimed_key = (
                "invalid-claimed-key-"
                + evidence.digest({"claim_type": type(claimed_key).__name__})[:16]
            )
        state = self._state()
        authorized = valid and tool in self.scenario.permissions
        if tool in EFFECTS:
            authorized = (
                authorized and self.scenario.requester_id == state["customer_id"]
            )
        if tool in {"cancel_order", "refund_order", "update_address"}:
            authorized = (
                authorized
                and (not self.scenario.approval_required or state["approved"])
                and (not self.scenario.must_read or self.read)
                and (self.scenario.scripted_reply is None or self.replied)
            )
        limit = self.sequence + 1 > self.scenario.max_tool_calls + (
            2 if self.asked else 0
        )
        response: dict[str, Any] = {"ok": False, "error": "invalid_arguments"}
        effect = None
        if claimed_key is not None:
            response = {"ok": False, "error": "operation_key_claim_rejected"}
        elif limit:
            response = {"ok": False, "error": "execution_limit"}
        elif not valid:
            pass
        elif not authorized:
            response = {"ok": False, "error": "permission_denied"}
        elif tool == "get_order":
            response = {"ok": True, "order": copy.deepcopy(state)}
            if self.scenario.untrusted_tool_note:
                response["untrusted_note"] = self.scenario.untrusted_tool_note
        elif tool == "request_approval":
            if self.scenario.user_approved:
                state["approved"] = True
                effect = "approval"
                response = {"ok": True, "approved": True}
            else:
                response = {"ok": False, "error": "approval_denied", "approved": False}
        elif (
            tool == "cancel_order"
            and state["status"] != "active"
            or tool == "refund_order"
            and raw["amount_cents"] > state["total_cents"] - state["refunded_cents"]
        ):
            response = {"ok": False, "error": "invalid_request"}
        else:
            effect = EFFECTS[tool]
            response = {"ok": True}
            if tool == "cancel_order":
                state["status"] = "cancelled"
            elif tool == "refund_order":
                state["refunded_cents"] += raw["amount_cents"]
            elif tool == "update_address":
                state["address"] = raw["address"]
        obligation_id = next(
            (
                item.id
                for item in self.scenario.obligations
                if item.effect == EFFECTS.get(tool)
                and (
                    tool != "refund_order"
                    or item.amount_cents == raw.get("amount_cents")
                )
                and (tool != "update_address" or item.address == raw.get("address"))
            ),
            None,
        )
        response, _ = self._record(
            "tool",
            tool,
            raw,
            response,
            effect,
            state,
            source="agent",
            claimed_operation_key=claimed_key,
            arguments_valid=valid,
            authorized=authorized,
            limit_exceeded=limit,
            matched_obligation_id=obligation_id,
        )
        if tool == "get_order" and response.get("ok"):
            self.read = True
        if limit:
            raise AgentLimit("interaction tool limit")
        return response


def check_interaction_evidence(
    trial_id, initial_state, stored, events, effects, receipts, head
):
    """Check the entire witness chain and reconstruct business state."""
    if (
        head is None
        or len(events) != len(receipts)
        or head["event_count"] != len(events)
    ):
        raise EvaluatorFailure("interaction evidence is incomplete")
    ledger = {e["operation_key"]: e for e in effects}
    if len(ledger) != len(effects):
        raise EvaluatorFailure("interaction effect identity conflict")
    state = copy.deepcopy(initial_state)
    previous = evidence.ZERO
    for n, (event, receipt) in enumerate(zip(events, receipts, strict=True), 1):
        key = f"{trial_id}:support-tools-v2:{n}"
        effect = ledger.pop(key, None)
        if (
            event.get("sequence") != n
            or event.get("operation_key") != key
            or receipt.get("sequence") != n
            or receipt.get("previous") != previous
            or receipt.get("digest")
            != evidence.digest({k: v for k, v in receipt.items() if k != "digest"})
            or receipt.get("event_sha256") != evidence.digest(event)
            or receipt.get("effect_sha256") != evidence.digest(effect)
            or bool(effect) != bool(event.get("effect"))
            or effect
            and (
                effect["kind"] != event["effect"]
                or effect["payload"]
                != {"tool": event["tool"], "arguments": event["arguments"]}
            )
        ):
            raise EvaluatorFailure("interaction evidence integrity conflict")
        if effect:
            args = event["arguments"]
            if event["tool"] == "refund_order":
                state["refunded_cents"] += args["amount_cents"]
            elif event["tool"] == "cancel_order":
                state["status"] = "cancelled"
            elif event["tool"] == "update_address":
                state["address"] = args["address"]
            elif event["tool"] == "request_approval":
                state["approved"] = True
        if receipt.get("state_sha256") != evidence.digest(state):
            raise EvaluatorFailure("interaction state witness conflict")
        previous = receipt["digest"]
    if ledger or head["digest"] != previous or state != stored:
        raise EvaluatorFailure("interaction final state conflict")


def require_interaction_intact(conn, trial_id, initial_state):
    """Reject missing, changed, or conflicting trial-local evidence before a new action."""
    try:
        events, effects, receipts, head = evidence.load(conn, trial_id)
        stored = conn.execute(
            "SELECT state FROM benchmark_trials WHERE id=%s", (trial_id,)
        ).fetchone()[0]
        check_interaction_evidence(
            trial_id, initial_state, stored, events, effects, receipts, head
        )
    except (KeyError, TypeError, ValueError, IndexError, ValidationError) as error:
        raise EvaluatorFailure("malformed interaction evidence") from error
