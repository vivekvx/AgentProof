"""Run-scoped simulator. Each tool mutation and its response ledger commit atomically."""

from datetime import UTC, datetime

from opentelemetry import trace
from psycopg.types.json import Jsonb

from .contracts import Scenario, ToolEvent
from .reliability import ExecutionStopped
from .telemetry import tracer


class OrderSimulator:
    def __init__(self, conn, run_id: str, scenario: Scenario):
        self.conn, self.run_id, self.scenario = conn, run_id, scenario
        self.sequence = 0
        with conn.transaction():
            conn.execute(
                "INSERT INTO orders VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                (run_id, scenario.order_id, scenario.initial_status),
            )

    def call(self, tool: str, order_id: str) -> dict:
        self.sequence += 1
        operation_key = f"{self.run_id}:reference-v1:tool:{self.sequence}"
        with tracer.start_as_current_span("tool." + tool) as span:
            span.set_attribute("agentproof.run_id", self.run_id)
            span.set_attribute("tool.sequence", self.sequence)
            span.set_attribute("agentproof.operation_key", operation_key)
            with self.conn.transaction():
                terminal = self.conn.execute(
                    "SELECT result FROM runs WHERE id=%s FOR UPDATE", (self.run_id,)
                ).fetchone()[0]
                if terminal is not None:
                    raise ExecutionStopped("run already terminal")
                old = self.conn.execute(
                    "SELECT payload FROM tool_events WHERE run_id=%s AND sequence=%s",
                    (self.run_id, self.sequence),
                ).fetchone()
                if old:
                    event = ToolEvent.model_validate(old[0])
                    if event.tool != tool or event.arguments != {"order_id": order_id}:
                        raise RuntimeError("tool replay mismatch")
                    return {"outcome": event.outcome, **event.output}
                if order_id != self.scenario.order_id:
                    raise ValueError("order outside isolated run")
                committed = tool == "cancel_order"
                outcome = "ok"
                if committed:
                    count = self.conn.execute(
                        "SELECT count(*) FROM side_effects WHERE run_id=%s",
                        (self.run_id,),
                    ).fetchone()[0]
                    self.conn.execute(
                        "UPDATE orders SET status='cancelled' WHERE run_id=%s",
                        (self.run_id,),
                    )
                    self.conn.execute(
                        "INSERT INTO side_effects(run_id,ordinal,kind,operation_key) VALUES (%s,%s,'cancellation',%s)",
                        (self.run_id, count + 1, operation_key),
                    )
                    if count == 0:
                        outcome = "timeout_after_commit"
                status = self.conn.execute(
                    "SELECT status FROM orders WHERE run_id=%s", (self.run_id,)
                ).fetchone()[0]
                # The timeout response must not disclose the committed order state.
                output = (
                    {"error": "API timeout; cancellation outcome unknown"}
                    if outcome != "ok"
                    else {"status": status}
                )
                ctx = trace.get_current_span().get_span_context()
                event = ToolEvent(
                    sequence=self.sequence,
                    tool=tool,
                    arguments={"order_id": order_id},
                    outcome=outcome,
                    output=output,
                    side_effect_committed=committed,
                    timestamp=datetime.now(UTC).isoformat(),
                    trace_id=f"{ctx.trace_id:032x}",
                    span_id=f"{ctx.span_id:016x}",
                )
                self.conn.execute(
                    "INSERT INTO tool_events(run_id,sequence,payload,operation_key) VALUES (%s,%s,%s,%s)",
                    (
                        self.run_id,
                        self.sequence,
                        Jsonb(event.model_dump()),
                        operation_key,
                    ),
                )
            span.set_attribute("tool.outcome", outcome)
            span.set_attribute("tool.side_effect_committed", committed)
            # The database commit precedes delivery of this simulated transport response.
            return {"outcome": outcome, **output}
