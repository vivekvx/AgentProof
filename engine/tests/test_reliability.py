import asyncio
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from agentproof.contracts import Scenario
from agentproof.execution import execute, record_terminal_failure
from agentproof.simulator import OrderSimulator
from conftest import ROOT
from temporalio.client import Client, WorkflowFailureError
from temporalio.exceptions import CancelledError

EVIDENCE = Path(os.getenv("EVIDENCE_DIR", ROOT / ".local/implementation-02"))


def save(run_id, dsn, name):
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    with psycopg.connect(dsn) as conn:
        result = conn.execute(
            "SELECT status,manifest,result FROM runs WHERE id=%s", (run_id,)
        ).fetchone()
        events = conn.execute(
            "SELECT sequence,payload,operation_key FROM tool_events WHERE run_id=%s ORDER BY sequence",
            (run_id,),
        ).fetchall()
        attempts = conn.execute(
            "SELECT temporal_run_id,activity_id,attempt,outcome,error_type,message FROM run_attempts WHERE run_id=%s ORDER BY started_at",
            (run_id,),
        ).fetchall()
        failures = conn.execute(
            "SELECT kind,error_type,message FROM run_failures WHERE run_id=%s",
            (run_id,),
        ).fetchall()
    (EVIDENCE / (name + ".json")).write_text(
        json.dumps(
            {
                "run_id": run_id,
                "run": result,
                "events": events,
                "attempts": attempts,
                "failures": failures,
            },
            indent=2,
            default=str,
        )
    )


def start_worker(mode, queue, marker, dsn):
    with open(str(marker) + ".log", "w") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                str(ROOT / "engine/tests/process_worker.py"),
                mode,
                queue,
                str(marker),
            ],
            cwd=ROOT,
            env={
                **os.environ,
                "DATABASE_URL": dsn,
                "TEMPORAL_ADDRESS": os.environ["TEST_TEMPORAL_ADDRESS"],
            },
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        return process


async def kill(process):
    if process.poll() is None:
        process.kill()
    await asyncio.to_thread(process.wait, timeout=10)


async def wait_marker(marker, process):
    for _ in range(400):
        if marker.exists():
            return
        if process.poll() is not None:
            pytest.fail(Path(str(marker) + ".log").read_text())
        await asyncio.sleep(0.05)
    pytest.fail(
        "worker did not reach interruption checkpoint: "
        + Path(str(marker) + ".log").read_text()
    )


@pytest.fixture
def temporal_address():
    value = os.getenv("TEST_TEMPORAL_ADDRESS")
    if not value:
        pytest.skip("Set TEST_TEMPORAL_ADDRESS for real process recovery tests")
    return value


@pytest.mark.parametrize(
    "point,committed",
    [("before_tool_commit", 0), ("after_tool_commit", 1), ("after_activity_commit", 1)],
)
async def test_sigkill_recovery(
    dsn, new_run, tmp_path, temporal_address, point, committed
):
    m = new_run()
    client = await Client.connect(temporal_address)
    queue = "recovery-" + str(uuid.uuid4())
    marker = tmp_path / point
    process = start_worker(point, queue, marker, dsn)
    replacement = None
    try:
        handle = await client.start_workflow(
            "EvaluationWorkflow", m.run_id, id=queue, task_queue=queue
        )
        await wait_marker(marker, process)
        await kill(process)
        with psycopg.connect(dsn) as conn:
            assert (
                conn.execute(
                    "SELECT count(*) FROM side_effects WHERE run_id=%s", (m.run_id,)
                ).fetchone()[0]
                == committed
            )
        replacement = start_worker("normal", queue, tmp_path / "replacement", dsn)
        result = await asyncio.wait_for(handle.result(), 40)
        assert result["status"] == "passed"
        assert result["side_effect_count"] == 1
        with psycopg.connect(dsn) as conn:
            assert (
                conn.execute(
                    "SELECT count(*) FROM tool_events WHERE run_id=%s", (m.run_id,)
                ).fetchone()[0]
                == 2
            )
            attempts = conn.execute(
                "SELECT attempt,outcome FROM run_attempts WHERE run_id=%s ORDER BY attempt",
                (m.run_id,),
            ).fetchall()
            assert attempts[0] == (1, None)
            assert attempts[-1] == (2, "completed")
        save(m.run_id, dsn, point)
    finally:
        await kill(process)
        if replacement:
            await kill(replacement)


@pytest.mark.parametrize("kind", ["evaluator", "infrastructure"])
async def test_failure_classification(dsn, new_run, tmp_path, temporal_address, kind):
    m = new_run()
    client = await Client.connect(temporal_address)
    queue = "failure-" + str(uuid.uuid4())
    process = start_worker(kind, queue, tmp_path / kind, dsn)
    try:
        result = await asyncio.wait_for(
            client.execute_workflow(
                "EvaluationWorkflow", m.run_id, id=queue, task_queue=queue
            ),
            40,
        )
        assert result["status"] == "infrastructure_failed"
        assert result["error"].startswith(kind + ":")
        assert result["checks"] == []
        with psycopg.connect(dsn) as conn:
            assert (
                conn.execute(
                    "SELECT kind FROM run_failures WHERE run_id=%s", (m.run_id,)
                ).fetchone()[0]
                == kind
            )
            assert conn.execute(
                "SELECT count(*) FROM run_attempts WHERE run_id=%s", (m.run_id,)
            ).fetchone()[0] == (1 if kind == "evaluator" else 3)
        save(m.run_id, dsn, kind)
    finally:
        await kill(process)


@pytest.mark.parametrize("started", [False, True])
async def test_temporal_cancellation(dsn, new_run, tmp_path, temporal_address, started):
    m = new_run()
    client = await Client.connect(temporal_address)
    queue = "cancel-" + str(uuid.uuid4())
    marker = tmp_path / "cancel"
    process = None
    try:
        handle = await client.start_workflow(
            "EvaluationWorkflow", m.run_id, id=queue, task_queue=queue
        )
        if started:
            process = start_worker("cancel_running", queue, marker, dsn)
            await wait_marker(marker, process)
        await handle.cancel()
        if not started:
            process = start_worker("normal", queue, marker, dsn)
        with pytest.raises(WorkflowFailureError) as error:
            await asyncio.wait_for(handle.result(), 30)
        assert isinstance(error.value.cause, CancelledError)
        with psycopg.connect(dsn) as conn:
            status, result = conn.execute(
                "SELECT status,result FROM runs WHERE id=%s", (m.run_id,)
            ).fetchone()
            assert status == "infrastructure_failed"
            assert result["error"].startswith("cancelled:")
            assert result["checks"] == []
        save(m.run_id, dsn, "cancel-started-" + str(started))
    finally:
        if process:
            await kill(process)


async def test_terminal_fences_and_idempotent_failure(dsn, new_run):
    m = new_run()
    with psycopg.connect(dsn, autocommit=True) as conn:
        sim = OrderSimulator(
            conn, m.run_id, Scenario.model_validate(m.scenario.content)
        )
        sim.call("cancel_order", m.scenario.content["order_id"])
        result = record_terminal_failure(
            m.run_id, "cancelled", "CancelledError", "test cancellation", dsn
        )
        again = record_terminal_failure(
            m.run_id, "infrastructure", "Error", "late failure", dsn
        )
        assert result == again
        with pytest.raises(Exception, match="terminal"):
            sim.call("cancel_order", m.scenario.content["order_id"])
        with pytest.raises(psycopg.errors.RaiseException, match="terminal"):
            conn.execute(
                "UPDATE orders SET status='active' WHERE run_id=%s", (m.run_id,)
            )
        assert (
            conn.execute(
                "SELECT count(*) FROM side_effects WHERE run_id=%s", (m.run_id,)
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM run_failures WHERE run_id=%s", (m.run_id,)
            ).fetchone()[0]
            == 1
        )


async def test_unique_operation_identity(dsn, new_run):
    m = new_run("duplicate")
    await execute(m.run_id, dsn)
    with psycopg.connect(dsn, autocommit=True) as conn:
        keys = conn.execute(
            "SELECT operation_key FROM side_effects WHERE run_id=%s ORDER BY ordinal",
            (m.run_id,),
        ).fetchall()
        assert keys == [
            (m.run_id + ":reference-v1:tool:1",),
            (m.run_id + ":reference-v1:tool:2",),
        ]
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                "INSERT INTO run_failures VALUES(%s,%s,'infrastructure','Error','evidence',now()),(%s,%s,'infrastructure','Error','duplicate',now())",
                (m.run_id, m.run_id + ":terminal", m.run_id, m.run_id + ":terminal"),
            )


async def test_late_cancellation_preserves_completed_verdict(dsn, new_run):
    m = new_run()
    result = await execute(m.run_id, dsn)
    assert (
        record_terminal_failure(
            m.run_id, "cancelled", "CancelledError", "late cancellation", dsn
        )
        == result
    )
    with psycopg.connect(dsn) as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM run_failures WHERE run_id=%s", (m.run_id,)
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM side_effects WHERE run_id=%s", (m.run_id,)
            ).fetchone()[0]
            == 1
        )


def test_database_rejects_duplicate_tool_effect(dsn, new_run):
    m = new_run()
    with psycopg.connect(dsn, autocommit=True) as conn:
        sim = OrderSimulator(
            conn, m.run_id, Scenario.model_validate(m.scenario.content)
        )
        sim.call("cancel_order", m.scenario.content["order_id"])
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                "INSERT INTO side_effects VALUES(%s,2,'cancellation',%s)",
                (m.run_id, m.run_id + ":reference-v1:tool:1"),
            )
