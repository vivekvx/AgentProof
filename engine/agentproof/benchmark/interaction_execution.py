"""Version 3 interaction execution using the existing trial and Temporal records."""

import asyncio
import time

from opentelemetry import trace
from opentelemetry.propagate import extract
from psycopg.types.json import Jsonb

from .. import provenance as execution_provenance
from ..telemetry import tracer
from . import evidence, interaction_evaluator
from .interaction_agent import run_reference
from .interaction_contracts import InteractionScenario
from .interaction_simulator import InteractionSimulator
from .registry import validate_snapshot
from .simulator import AgentLimit, TrialStopped


async def execute_locked(conn, trial_id, attempt):
    from .execution import lock_trial

    with conn.transaction():
        row = lock_trial(conn, trial_id)
        if row[3] is not None:
            return row[3]
        evaluation_id, raw, _state, _, manifest = row
        validate_snapshot(manifest)
        scenario = InteractionScenario.model_validate(raw)
        published = next(
            (
                s
                for s in manifest["dataset"]["content"]["scenarios"]
                if s["id"] == scenario.id
            ),
            None,
        )
        if published != raw:
            raise ValueError("interaction trial differs from publication")
        execution_provenance.record(conn, trial_id, "benchmark", manifest, raw)
        conn.execute(
            "UPDATE benchmark_trials SET status='running',started_at=COALESCE(started_at,clock_timestamp()) WHERE id=%s",
            (trial_id,),
        )
    attrs = {
        "agentproof.run_id": str(evaluation_id),
        "agentproof.trial_id": trial_id,
        "agentproof.scenario.id": scenario.id,
        "agentproof.scenario.version": scenario.version,
        "agentproof.dataset.id": manifest["dataset"]["id"],
        "agentproof.dataset.version": manifest["dataset"]["version"],
        "agentproof.dataset.sha256": manifest["dataset"]["sha256"],
        "agentproof.evaluator.version": "support-evaluator-v5",
        "agentproof.tool_schema.version": "support-tools-v2",
        **attempt,
    }
    context = (
        None
        if trace.get_current_span().get_span_context().is_valid
        else extract({"traceparent": manifest["traceparent"]})
    )
    with tracer.start_as_current_span(
        "benchmark.trial", context=context, attributes=attrs
    ) as span:
        started = time.perf_counter()
        simulator = InteractionSimulator(
            conn, trial_id, scenario, int(attempt.get("temporal.activity.attempt", 1))
        )
        public = {
            "schema_version": "3.0",
            "user_message": scenario.initial_user,
            "order_id": scenario.order.order_id,
            "customer_id": scenario.requester_id,
            "tools": list(scenario.permissions),
            "limits": scenario.limits.model_dump(),
        }
        execution_error = None
        limit_hit = False
        try:
            async with asyncio.timeout(scenario.limits.wall_ms / 1000):
                outcome = await run_reference(
                    public, simulator.call, simulator.ask, simulator.receive_user_reply
                )
        except TimeoutError:
            outcome = None
            execution_error = "infrastructure"
        except AgentLimit:
            outcome = None
            limit_hit = True
        except TrialStopped:
            return conn.execute(
                "SELECT result FROM benchmark_trials WHERE id=%s", (trial_id,)
            ).fetchone()[0]
        if (
            outcome is not None
            and (time.perf_counter() - started) * 1000 > scenario.limits.wall_ms
        ):
            outcome = None
            execution_error = "infrastructure"
        with conn.transaction():
            row = lock_trial(conn, trial_id)
            if row[3] is not None:
                return row[3]
            state = row[2]
            events, effects, receipts, head = evidence.load(conn, trial_id)
            latency = float(
                conn.execute(
                    "SELECT EXTRACT(epoch FROM (clock_timestamp()-started_at))*1000 FROM benchmark_trials WHERE id=%s",
                    (trial_id,),
                ).fetchone()[0]
            )
            execution_hash = execution_provenance.record(
                conn, trial_id, "benchmark", manifest, raw
            )
            result = interaction_evaluator.assess(
                trial_id=trial_id,
                scenario=scenario,
                state=state,
                events=events,
                effects=effects,
                receipts=receipts,
                head=head,
                manifest=manifest,
                outcome=outcome,
                attempt={
                    **(attempt or {"temporal.activity.attempt": 1, "source": "direct"}),
                    "execution_sha256": execution_hash,
                },
                latency_ms=latency,
                trace_id=f"{trace.get_current_span().get_span_context().trace_id:032x}",
                execution_error=execution_error,
                limit_hit=limit_hit,
            )
            span.set_attribute("agentproof.verdict", result.verdict)
            span.set_attribute(
                "agentproof.failure_category", result.failure_category or "none"
            )
            conn.execute(
                "UPDATE benchmark_trials SET status='completed',completed_at=clock_timestamp(),result=%s,agent_outcome=%s WHERE id=%s",
                (
                    Jsonb(result.model_dump()),
                    Jsonb(outcome.model_dump()) if outcome else None,
                    trial_id,
                ),
            )
            execution_provenance.bind_result(
                conn, trial_id, "benchmark", result.model_dump()
            )
            return result.model_dump()


def fail_locked(conn, trial_id, category):
    from .execution import lock_trial

    row = lock_trial(conn, trial_id)
    if row[3] is not None:
        return row[3]
    _, raw, state, _, manifest = row
    scenario = InteractionScenario.model_validate(raw)
    execution_hash = execution_provenance.record(
        conn, trial_id, "benchmark", manifest, raw
    )
    events, effects, receipts, head = evidence.load(conn, trial_id)
    result = interaction_evaluator.assess(
        trial_id=trial_id,
        scenario=scenario,
        state=state,
        events=events,
        effects=effects,
        receipts=receipts,
        head=head,
        manifest=manifest,
        execution_error=category,
        attempt={
            "temporal.activity.attempt": max(
                (e.get("attempt", 0) for e in events), default=0
            ),
            "source": "terminal_failure",
            "execution_sha256": execution_hash,
        },
        trace_id=manifest["traceparent"].split("-")[1],
    )
    conn.execute(
        "UPDATE benchmark_trials SET status='completed',completed_at=clock_timestamp(),result=%s WHERE id=%s",
        (Jsonb(result.model_dump()), trial_id),
    )
    execution_provenance.bind_result(conn, trial_id, "benchmark", result.model_dump())
    return result.model_dump()
