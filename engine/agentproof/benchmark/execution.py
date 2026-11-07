import asyncio
import copy
import time
from collections import defaultdict

from opentelemetry import trace
from opentelemetry.propagate import extract
from psycopg import Error as DatabaseError
from psycopg.types.json import Jsonb
from pydantic_ai.exceptions import UsageLimitExceeded

from .. import provenance as execution_provenance
from ..database import connect
from ..reliability import EvaluatorFailure
from ..telemetry import tracer
from . import audited_evaluator, audited_evaluator_v3, evidence, validated_evaluator
from .agent import run_reference
from .contracts import (
    AgentConfiguration,
    AgentOutcome,
    AssertionEvidence,
    EvaluationResult,
    PublicContext,
    Scenario,
)
from .evaluator import evaluate
from .llm import Journal, ProviderFailure, run_llm
from .metrics import summarize
from .registry import validate_snapshot
from .simulator import AgentLimit, OrderSimulator, TrialStopped


def lock_trial(conn, trial_id):
    parent = conn.execute(
        "SELECT e.status FROM benchmark_evaluations e JOIN benchmark_trials t ON t.evaluation_id=e.id WHERE t.id=%s FOR SHARE OF e",
        (trial_id,),
    ).fetchone()
    if not parent:
        raise ValueError("unknown benchmark trial")
    return conn.execute(
        "SELECT t.evaluation_id,t.scenario,t.state,t.result,e.manifest FROM benchmark_trials t JOIN benchmark_evaluations e ON e.id=t.evaluation_id WHERE t.id=%s FOR UPDATE OF t",
        (trial_id,),
    ).fetchone()


async def execute_trial(trial_id, dsn=None, attempt=None):
    return await asyncio.to_thread(
        lambda: asyncio.run(_execute_trial(trial_id, dsn, attempt or {}))
    )


async def _execute_trial(trial_id, dsn, attempt):
    with connect(dsn) as conn:
        conn.execute("SELECT pg_advisory_lock(hashtextextended(%s,2))", (trial_id,))
        version = conn.execute(
            "SELECT e.manifest->'dataset'->>'version' FROM benchmark_evaluations e JOIN benchmark_trials t ON t.evaluation_id=e.id WHERE t.id=%s",
            (trial_id,),
        ).fetchone()
        if version and version[0] == "6":
            from .interaction_execution import execute_locked

            return await execute_locked(conn, trial_id, attempt)
        with conn.transaction():
            row = lock_trial(conn, trial_id)
            if row[3] is not None:
                return row[3]
            evaluation_id, raw, _state, _, manifest = row
            validate_snapshot(manifest)
            scenario = Scenario.model_validate(raw)
            published = next(
                (
                    s
                    for s in manifest["dataset"]["content"]["scenarios"]
                    if s["id"] == scenario.id
                ),
                None,
            )
            if published != raw:
                raise ValueError("trial scenario differs from published dataset")
            config = AgentConfiguration.model_validate(manifest["agent"]["content"])
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
            "agentproof.agent.id": config.id,
            "agentproof.agent.version": config.version,
            "agentproof.fixture.version": scenario.fixture.version,
            "agentproof.prompt.version": config.prompt_version,
            "agentproof.tool_schema.version": config.tool_schema_version,
            "agentproof.evaluator.version": manifest["evaluator"]["content"][
                "implementation"
            ],
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
            active_start = time.perf_counter()
            limit_hit = False
            effective_scenario = scenario.model_copy(deep=True)
            if config.policy == "llm":
                effective_scenario.limits.max_tool_calls = min(
                    scenario.limits.max_tool_calls,
                    config.model_configuration.max_tool_calls,
                )
            simulator = OrderSimulator(
                conn,
                trial_id,
                effective_scenario,
                int(attempt.get("temporal.activity.attempt", 1)),
                record_receipts=manifest["evaluator"]["version"] == 4,
            )
            public = PublicContext(
                tool_schema={
                    name: copy.deepcopy(
                        manifest["tool_schema"]["content"]["tools"][name]
                    )
                    for name in scenario.permissions.tools
                },
                request=scenario.request.model_copy(deep=True),
                permissions=scenario.permissions.model_copy(deep=True),
                limits=effective_scenario.limits.model_copy(deep=True),
            )
            journal = Journal(conn, trial_id)
            provider_error = None
            execution_error = None
            try:
                async with asyncio.timeout(scenario.limits.wall_ms / 1000):
                    if config.policy == "llm":
                        outcome = await run_llm(public, config, simulator.call, journal)
                    else:
                        outcome = await run_reference(public, config, simulator.call)
            except ProviderFailure as error:
                provider_error = str(error)
                outcome = AgentOutcome(resolution="blocked")
            except TimeoutError:
                outcome = AgentOutcome(resolution="blocked")
                if manifest["evaluator"]["version"] == 4:
                    execution_error = "infrastructure"
                else:
                    outcome = AgentOutcome(resolution="limit")
                    limit_hit = True
            except (AgentLimit, UsageLimitExceeded):
                outcome = AgentOutcome(resolution="limit")
                limit_hit = True
            except TrialStopped:
                return conn.execute(
                    "SELECT result FROM benchmark_trials WHERE id=%s", (trial_id,)
                ).fetchone()[0]
            limit_hit = limit_hit or (
                manifest["evaluator"]["version"] != 4
                and (time.perf_counter() - active_start) * 1000
                > scenario.limits.wall_ms
            )
            if (
                manifest["evaluator"]["version"] == 4
                and not limit_hit
                and not provider_error
                and (time.perf_counter() - active_start) * 1000
                > scenario.limits.wall_ms
            ):
                execution_error = "infrastructure"
            with conn.transaction():
                row = lock_trial(conn, trial_id)
                if row[3] is not None:
                    return row[3]
                events = [
                    r[0]
                    for r in conn.execute(
                        "SELECT payload FROM benchmark_events WHERE trial_id=%s ORDER BY sequence",
                        (trial_id,),
                    )
                ]
                effects = [
                    {"kind": r[0], "payload": r[1]}
                    for r in conn.execute(
                        "SELECT kind,payload FROM benchmark_effects WHERE trial_id=%s",
                        (trial_id,),
                    )
                ]
                latency = float(
                    conn.execute(
                        "SELECT EXTRACT(epoch FROM (clock_timestamp()-started_at))*1000 FROM benchmark_trials WHERE id=%s",
                        (trial_id,),
                    ).fetchone()[0]
                )
                try:
                    audited = {2: audited_evaluator, 3: audited_evaluator_v3}.get(
                        manifest["evaluator"]["version"]
                    )
                    evaluator = audited.evaluate if audited else evaluate
                    if manifest["evaluator"]["version"] == 4:
                        result = _validated_result(
                            conn,
                            trial_id,
                            # A provider failure is not an agent refusal.
                            outcome=None
                            if provider_error or execution_error
                            else outcome,
                            execution_error=execution_error,
                            attempt=attempt
                            or {"temporal.activity.attempt": 1, "source": "direct"},
                            latency_ms=latency,
                            limit_hit=limit_hit,
                            provider_error=provider_error,
                            trace_id=f"{trace.get_current_span().get_span_context().trace_id:032x}",
                        )
                    else:
                        result = evaluator(
                            trial_id,
                            scenario,
                            row[2],
                            events,
                            effects,
                            outcome,
                            manifest["evaluator"]["sha256"],
                            f"{trace.get_current_span().get_span_context().trace_id:032x}",
                            latency,
                            limit_hit,
                        )
                except DatabaseError:
                    raise
                except Exception as error:
                    raise EvaluatorFailure(str(error)) from error
                if config.policy == "llm":
                    calls = journal.metadata()
                    result.model_requests = calls
                    complete = bool(calls) and all(
                        c.get("input_tokens") is not None
                        and c.get("output_tokens") is not None
                        for c in calls
                    )
                    if complete:
                        result.input_tokens = sum(c["input_tokens"] for c in calls)
                        result.output_tokens = sum(c["output_tokens"] for c in calls)
                        result.estimated_cost_usd = sum(
                            c["estimated_cost_usd"] for c in calls
                        )
                    result.provider_error = provider_error
                    # Security/argument violations retain a deterministic agent failure even if a provider later fails.
                    if (
                        result.assessment is None
                        and provider_error
                        and result.unsafe_actions == 0
                        and result.valid_tool_calls == result.tool_calls
                        and (
                            audited is None
                            or not audited.irreversible_violation(scenario, result)
                        )
                    ):
                        result.verdict = "INDETERMINATE"
                        result.failure_category = "provider"
                        result.error = "provider: " + provider_error
                span.set_attribute("agentproof.verdict", result.verdict)
                if result.assessment:
                    for dimension in (
                        "task",
                        "policy",
                        "provider",
                        "harness",
                        "execution",
                    ):
                        span.set_attribute(
                            "agentproof." + dimension,
                            getattr(result.assessment, dimension),
                        )
                span.set_attribute(
                    "agentproof.failure_category", result.failure_category or "none"
                )
                conn.execute(
                    "UPDATE benchmark_trials SET status='completed',completed_at=clock_timestamp(),result=%s,agent_outcome=%s WHERE id=%s",
                    (Jsonb(result.model_dump()), Jsonb(outcome.model_dump()), trial_id),
                )
                execution_provenance.bind_result(
                    conn, trial_id, "benchmark", result.model_dump()
                )
                return result.model_dump()


def fail_trial(
    trial_id, category="infrastructure", message="Execution activity failed", dsn=None
):
    with connect(dsn) as conn, conn.transaction():
        row = lock_trial(conn, trial_id)
        if row[3] is not None:
            return row[3]
        _, scenario, state, _, manifest = row
        if manifest["dataset"]["version"] == 6:
            from .interaction_execution import fail_locked

            return fail_locked(conn, trial_id, category)
        execution_provenance.record(conn, trial_id, "benchmark", manifest, scenario)
        if manifest["evaluator"]["version"] == 4:
            result = _validated_result(conn, trial_id, execution_error=category)
            conn.execute(
                "UPDATE benchmark_trials SET status='completed',completed_at=clock_timestamp(),result=%s WHERE id=%s",
                (Jsonb(result.model_dump()), trial_id),
            )
            execution_provenance.bind_result(
                conn, trial_id, "benchmark", result.model_dump()
            )
            return result.model_dump()
        events = [
            r[0]
            for r in conn.execute(
                "SELECT payload FROM benchmark_events WHERE trial_id=%s ORDER BY sequence",
                (trial_id,),
            )
        ]
        counts = dict(
            conn.execute(
                "SELECT kind,count(*) FROM benchmark_effects WHERE trial_id=%s GROUP BY kind",
                (trial_id,),
            ).fetchall()
        )
        age = conn.execute(
            "SELECT EXTRACT(epoch FROM (clock_timestamp()-started_at))*1000 FROM benchmark_trials WHERE id=%s",
            (trial_id,),
        ).fetchone()[0]
        result = EvaluationResult(
            trial_id=trial_id,
            scenario_id=scenario["id"],
            verdict="INDETERMINATE",
            failure_category=category,
            evaluator_version=manifest["evaluator"]["content"]["implementation"],
            evaluator_sha256=manifest["evaluator"]["sha256"],
            assertions=[
                AssertionEvidence(
                    id="harness",
                    verdict="INDETERMINATE",
                    expected="successful harness execution",
                    actual=category,
                    event_sequences=[e["sequence"] for e in events],
                )
            ],
            final_state=state,
            effect_counts=counts,
            unsafe_actions=sum(not e["authorized"] for e in events),
            valid_tool_calls=sum(e["arguments_valid"] for e in events),
            tool_calls=len(events),
            recovery_eligible=scenario["assertions"]["recovery_required"],
            recovery_succeeded=None,
            duplicate_side_effects=any(
                counts.get(k, 0) > 1
                for k in ["cancellation", "refund", "address_change"]
            ),
            latency_ms=float(age) if age is not None else None,
            trace_id=manifest["traceparent"].split("-")[1],
            error=f"{category}: {message[:1000]}",
        )
        conn.execute(
            "UPDATE benchmark_trials SET status='completed',completed_at=clock_timestamp(),result=%s WHERE id=%s",
            (Jsonb(result.model_dump()), trial_id),
        )
        execution_provenance.bind_result(
            conn, trial_id, "benchmark", result.model_dump()
        )
        return result.model_dump()


def plan(evaluation_id, dsn=None):
    with connect(dsn) as conn:
        row = conn.execute(
            "SELECT manifest,result FROM benchmark_evaluations WHERE id=%s",
            (evaluation_id,),
        ).fetchone()
        if not row:
            raise ValueError("unknown evaluation")
        validate_snapshot(row[0])
        if row[1] is None:
            execution_provenance.record(conn, evaluation_id, "evaluation", row[0])
            conn.execute(
                "UPDATE benchmark_evaluations SET status='running' WHERE id=%s AND result IS NULL",
                (evaluation_id,),
            )
        return [
            str(r[0])
            for r in conn.execute(
                "SELECT id FROM benchmark_trials WHERE evaluation_id=%s ORDER BY scenario_id,trial_index",
                (evaluation_id,),
            )
        ]


def finalize(evaluation_id, category=None, message="", dsn=None):
    with connect(dsn) as conn, conn.transaction():
        row = conn.execute(
            "SELECT manifest,result,created_at FROM benchmark_evaluations WHERE id=%s FOR UPDATE",
            (evaluation_id,),
        ).fetchone()
        if row[1] is not None:
            return row[1]
        execution_provenance.record(conn, evaluation_id, "evaluation", row[0])
        pending = [
            str(r[0])
            for r in conn.execute(
                "SELECT id FROM benchmark_trials WHERE evaluation_id=%s AND result IS NULL ORDER BY id",
                (evaluation_id,),
            )
        ]
        # Use this same connection: the parent row lock must fence late activity threads.
        for trial_id in pending:
            _fail_locked(
                conn,
                trial_id,
                category or "infrastructure",
                message or "Workflow ended before trial completion",
            )
        trial_rows = conn.execute(
            "SELECT id,result,scenario,agent_outcome,started_at,completed_at FROM benchmark_trials WHERE evaluation_id=%s ORDER BY scenario_id,trial_index",
            (evaluation_id,),
        ).fetchall()
        attempts = defaultdict(list)
        for trial_id, started_at, finished_at in conn.execute(
            "SELECT a.trial_id,a.started_at,a.finished_at FROM benchmark_attempts a JOIN benchmark_trials t ON t.id=a.trial_id WHERE t.evaluation_id=%s",
            (evaluation_id,),
        ):
            attempts[str(trial_id)].append((started_at, finished_at))
        events = defaultdict(list)
        for trial_id, payload in conn.execute(
            "SELECT e.trial_id,e.payload FROM benchmark_events e JOIN benchmark_trials t ON t.id=e.trial_id WHERE t.evaluation_id=%s ORDER BY e.trial_id,e.sequence",
            (evaluation_id,),
        ):
            events[str(trial_id)].append(payload)
        slots = []
        for trial_id, result, scenario, outcome, started_at, completed_at in trial_rows:
            activity = attempts[str(trial_id)]
            slots.append(
                {
                    "result": result,
                    "scenario": scenario,
                    "outcome": outcome,
                    "started": started_at is not None,
                    "events": events[str(trial_id)],
                    "active_task_ms": sum(
                        (finish - start).total_seconds() * 1000
                        for start, finish in activity
                    )
                    if activity and all(finish is not None for _, finish in activity)
                    else None,
                    "prestart_wait_ms": (started_at - row[2]).total_seconds() * 1000
                    if started_at
                    else None,
                    "trial_elapsed_ms": (completed_at - row[2]).total_seconds() * 1000
                    if completed_at
                    else None,
                }
            )
        config = row[0]["agent"]["content"].get("model_configuration")
        now = conn.execute("SELECT clock_timestamp()").fetchone()[0]
        metrics = summarize(
            [slot["result"] for slot in slots if slot["result"] is not None],
            slots=slots,
            planned_count=len(row[0]["scenario_ids"]) * row[0]["trials"],
            expected_repeats=row[0]["trials"],
            planned_scenario_ids=row[0]["scenario_ids"],
            evaluation_elapsed_ms=(now - row[2]).total_seconds() * 1000,
            model_configuration=config if isinstance(config, dict) else None,
        )
        conn.execute(
            "UPDATE benchmark_evaluations SET status=%s,result=%s WHERE id=%s",
            (
                "cancelled" if category == "cancelled" else "completed",
                Jsonb(metrics),
                evaluation_id,
            ),
        )
        execution_provenance.bind_result(conn, evaluation_id, "evaluation", metrics)
        return metrics


def _fail_locked(conn, trial_id, category, message):
    # Finalizer already holds the parent lock. Build the same v2 failure result locally.
    row = conn.execute(
        "SELECT t.scenario_id,t.state,t.scenario,e.manifest FROM benchmark_trials t JOIN benchmark_evaluations e ON t.evaluation_id=e.id WHERE t.id=%s FOR UPDATE OF t",
        (trial_id,),
    ).fetchone()
    if row[3]["dataset"]["version"] == 6:
        from .interaction_execution import fail_locked

        return fail_locked(conn, trial_id, category)
    execution_provenance.record(conn, trial_id, "benchmark", row[3], row[2])
    if row[3]["evaluator"]["version"] == 4:
        result = _validated_result(conn, trial_id, execution_error=category)
        conn.execute(
            "UPDATE benchmark_trials SET status='completed',completed_at=clock_timestamp(),result=%s WHERE id=%s AND result IS NULL",
            (Jsonb(result.model_dump()), trial_id),
        )
        execution_provenance.bind_result(
            conn, trial_id, "benchmark", result.model_dump()
        )
        return
    events = [
        r[0]
        for r in conn.execute(
            "SELECT payload FROM benchmark_events WHERE trial_id=%s ORDER BY sequence",
            (trial_id,),
        )
    ]
    counts = dict(
        conn.execute(
            "SELECT kind,count(*) FROM benchmark_effects WHERE trial_id=%s GROUP BY kind",
            (trial_id,),
        ).fetchall()
    )
    result = EvaluationResult(
        trial_id=trial_id,
        scenario_id=row[0],
        verdict="INDETERMINATE",
        failure_category=category,
        evaluator_version=row[3]["evaluator"]["content"]["implementation"],
        evaluator_sha256=row[3]["evaluator"]["sha256"],
        assertions=[
            AssertionEvidence(
                id="harness",
                verdict="INDETERMINATE",
                expected="successful harness execution",
                actual=category,
                event_sequences=[e["sequence"] for e in events],
            )
        ],
        final_state=row[1],
        effect_counts=counts,
        unsafe_actions=sum(not e["authorized"] for e in events),
        valid_tool_calls=sum(e["arguments_valid"] for e in events),
        tool_calls=len(events),
        recovery_eligible=row[2]["assertions"]["recovery_required"],
        recovery_succeeded=None,
        duplicate_side_effects=any(
            counts.get(k, 0) > 1 for k in ["cancellation", "refund", "address_change"]
        ),
        latency_ms=None,
        trace_id=row[3]["traceparent"].split("-")[1],
        error=f"{category}: {message[:1000]}",
    )
    conn.execute(
        "UPDATE benchmark_trials SET status='completed',completed_at=clock_timestamp(),result=%s WHERE id=%s AND result IS NULL",
        (Jsonb(result.model_dump()), trial_id),
    )
    execution_provenance.bind_result(conn, trial_id, "benchmark", result.model_dump())


def _validated_result(conn, trial_id, **conditions):
    raw, state, manifest = conn.execute(
        "SELECT t.scenario,t.state,e.manifest FROM benchmark_trials t JOIN benchmark_evaluations e ON e.id=t.evaluation_id WHERE t.id=%s",
        (trial_id,),
    ).fetchone()
    events, effects, receipts, head = evidence.load(conn, trial_id)
    if not conditions.get("attempt"):
        latest = conn.execute(
            "SELECT attempt,workflow_run_id,activity_id FROM benchmark_attempts WHERE trial_id=%s ORDER BY started_at DESC LIMIT 1",
            (trial_id,),
        ).fetchone()
        conditions["attempt"] = (
            {
                "temporal.activity.attempt": latest[0],
                "temporal.workflow.run_id": latest[1],
                "temporal.activity.id": latest[2],
            }
            if latest
            else {
                "temporal.activity.attempt": max(
                    (e.get("attempt", 0) for e in events), default=0
                ),
                "source": "direct_or_not_started",
            }
        )
    execution_hash = execution_provenance.record(
        conn, trial_id, "benchmark", manifest, raw
    )
    conditions["attempt"] = {
        **conditions["attempt"],
        "execution_sha256": execution_hash,
    }
    calls = Journal(conn, trial_id).metadata()
    if "provider_error" not in conditions:
        conditions["provider_error"] = next(
            (c["error"] for c in calls if c.get("error")), None
        )
    conditions.setdefault("outcome", None)
    conditions.setdefault("trace_id", manifest["traceparent"].split("-")[1])
    result = validated_evaluator.assess(
        trial_id=trial_id,
        scenario=Scenario.model_validate(raw),
        state=state,
        events=events,
        effects=effects,
        receipts=receipts,
        head=head,
        manifest=manifest,
        **conditions,
    )
    result.model_requests = calls
    return result
