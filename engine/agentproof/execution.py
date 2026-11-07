import asyncio
import hashlib
import json
from pathlib import Path

from opentelemetry import trace
from opentelemetry.propagate import extract
from psycopg.types.json import Jsonb

from . import provenance as execution_provenance
from .agent import run_agent
from .contracts import EvaluationResult, Manifest, Scenario
from .database import connect
from .evaluator import evaluate
from .reliability import Attempt, EvaluatorFailure, ExecutionStopped
from .simulator import OrderSimulator
from .telemetry import tracer

ROOT = Path(__file__).resolve().parents[2]


def validate_references(manifest):
    for ref, path in [
        (manifest.scenario, "scenarios/cancel-order.v1.json"),
        (manifest.evaluator, "evaluators/cancellation.v1.json"),
    ]:
        data = (ROOT / "packages/contracts" / path).read_bytes()
        if ref.sha256 != hashlib.sha256(data).hexdigest() or ref.content != json.loads(
            data
        ):
            raise ValueError("unsupported immutable reference")
        if ref.id != ref.content["id"] or ref.version != ref.content["version"]:
            raise ValueError("reference identity mismatch")


async def execute(run_id: str, dsn: str | None = None, attempt: Attempt | None = None):
    # Each execution owns a thread and connection. A lock wait must not block
    # the worker event loop or the execution that currently owns the lock.
    return await asyncio.to_thread(
        lambda: asyncio.run(_execute(run_id, dsn, attempt or Attempt()))
    )


async def _execute(run_id: str, dsn: str | None, attempt: Attempt):
    with connect(dsn) as conn:
        # A session lock serializes retries. The ledger makes completed tools replayable.
        conn.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (run_id,))
        row = conn.execute(
            "SELECT manifest,result FROM runs WHERE id=%s", (run_id,)
        ).fetchone()
        if not row:
            raise ValueError("run not found")
        if row[1] is not None:
            return row[1]
        manifest = Manifest.model_validate(row[0])
        if manifest.run_id != run_id:
            raise ValueError("manifest run ID mismatch")
        validate_references(manifest)
        execution_provenance.record(conn, run_id, "run", row[0])
        scenario = Scenario.model_validate(manifest.scenario.content)
        conn.execute(
            "UPDATE runs SET status='running' WHERE id=%s AND result IS NULL", (run_id,)
        )
        with tracer.start_as_current_span(
            "evaluation.execute",
            context=None
            if trace.get_current_span().get_span_context().is_valid
            else extract({"traceparent": manifest.traceparent}),
            attributes=attempt.attributes(run_id, manifest),
        ) as span:
            span.set_attribute("agentproof.run_id", run_id)
            try:
                simulator = OrderSimulator(conn, run_id, scenario)
                await run_agent(simulator, manifest.agent)
            except ExecutionStopped:
                return conn.execute(
                    "SELECT result FROM runs WHERE id=%s", (run_id,)
                ).fetchone()[0]
            with conn.transaction():
                # Cancellation and completion take the same row lock. The first terminal result wins.
                current = conn.execute(
                    "SELECT result FROM runs WHERE id=%s FOR UPDATE", (run_id,)
                ).fetchone()[0]
                if current is not None:
                    return current
                try:
                    result = evaluate(
                        conn,
                        manifest,
                        f"{trace.get_current_span().get_span_context().trace_id:032x}",
                    )
                except Exception as error:
                    # Database faults are infrastructure faults; evaluator defects are not retried.
                    from psycopg import Error as DatabaseError

                    if isinstance(error, DatabaseError):
                        raise
                    raise EvaluatorFailure(str(error)) from error
                conn.execute(
                    "UPDATE runs SET status='completed',result=%s WHERE id=%s AND result IS NULL",
                    (Jsonb(result.model_dump()), run_id),
                )
                execution_provenance.bind_result(
                    conn, run_id, "run", result.model_dump()
                )
            return result.model_dump()


def record_terminal_failure(
    run_id: str,
    kind="infrastructure",
    error_type="ActivityError",
    message="Execution activity exhausted retries",
    dsn=None,
):
    with connect(dsn) as conn, conn.transaction():
        row = conn.execute(
            "SELECT manifest,result FROM runs WHERE id=%s FOR UPDATE", (run_id,)
        ).fetchone()
        if not row:
            raise ValueError("run not found")
        if row[1] is not None:
            return row[1]
        m = Manifest.model_validate(row[0])
        execution_provenance.record(conn, run_id, "run", row[0])
        order = conn.execute(
            "SELECT order_id,status FROM orders WHERE run_id=%s", (run_id,)
        ).fetchone()
        count = conn.execute(
            "SELECT count(*) FROM side_effects WHERE run_id=%s", (run_id,)
        ).fetchone()[0]
        result = EvaluationResult(
            run_id=run_id,
            status="infrastructure_failed",
            failure_kind="infrastructure",
            checks=[],
            final_state={"order_id": order[0], "status": order[1]} if order else {},
            side_effect_count=count,
            scenario_sha256=m.scenario.sha256,
            evaluator_sha256=m.evaluator.sha256,
            trace_id=m.traceparent.split("-")[1],
            error=f"{kind}: {error_type}: {message[:2000]}",
        )
        conn.execute(
            "INSERT INTO run_failures(run_id,operation_key,kind,error_type,message) VALUES(%s,%s,%s,%s,%s)",
            (run_id, run_id + ":terminal", kind, error_type, message[:2000]),
        )
        conn.execute(
            "UPDATE runs SET status='infrastructure_failed',result=%s WHERE id=%s AND result IS NULL",
            (Jsonb(result.model_dump()), run_id),
        )
        execution_provenance.bind_result(conn, run_id, "run", result.model_dump())
        return result.model_dump()


def record_infrastructure_failure(run_id: str):
    return record_terminal_failure(run_id)
