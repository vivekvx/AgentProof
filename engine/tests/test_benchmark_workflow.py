import asyncio
import copy
import json
import os
import subprocess
import sys
import uuid

import psycopg
import pytest
from agentproof.benchmark import activities
from agentproof.benchmark.contracts import AgentOutcome
from agentproof.benchmark.evaluator import evaluate
from agentproof.benchmark.execution import execute_trial
from agentproof.benchmark.metrics import summarize
from agentproof.benchmark.registry import dataset
from agentproof.benchmark.workflow import BenchmarkWorkflow
from conftest import ROOT
from temporalio import activity
from temporalio.client import Client, WorkflowFailureError
from temporalio.exceptions import ApplicationError, CancelledError
from temporalio.testing import ActivityEnvironment
from temporalio.worker import Replayer, Worker


@pytest.fixture
def benchmark_temporal():
    value = os.getenv("TEST_TEMPORAL_ADDRESS")
    if not value:
        pytest.skip("Set TEST_TEMPORAL_ADDRESS")
    return value


async def test_activity_environment_and_metadata(dsn, benchmark_factory, monkeypatch):
    _, ids, _ = benchmark_factory(ids=["cs-001"])
    monkeypatch.setenv("DATABASE_URL", dsn)
    env = ActivityEnvironment()
    heartbeats = []
    env.on_heartbeat = lambda *details: heartbeats.append(details)
    result = await env.run(activities.benchmark_trial, ids[0])
    assert result["verdict"] == "PASS" and heartbeats
    with psycopg.connect(dsn) as conn:
        assert conn.execute(
            "SELECT attempt,outcome FROM benchmark_attempts WHERE trial_id=%s",
            (ids[0],),
        ).fetchone() == (1, "completed")


async def test_partial_failure_does_not_discard_other_trials(
    dsn, benchmark_factory, benchmark_temporal, monkeypatch
):
    parent, ids, _ = benchmark_factory(ids=["cs-001", "cs-002", "cs-003"])
    monkeypatch.setenv("DATABASE_URL", dsn)

    @activity.defn(name="benchmark_trial")
    async def partial(trial_id: str) -> dict:
        if trial_id == ids[1]:
            raise ApplicationError(
                "injected evaluator defect", type="EvaluatorFailure", non_retryable=True
            )
        return await activities.benchmark_trial(trial_id)

    client = await Client.connect(benchmark_temporal)
    queue = "benchmark-partial-" + str(uuid.uuid4())
    async with Worker(
        client,
        task_queue=queue,
        workflows=[BenchmarkWorkflow],
        activities=[
            activities.benchmark_plan,
            partial,
            activities.benchmark_fail_trial,
            activities.benchmark_finalize,
        ],
    ):
        handle = await client.start_workflow(
            BenchmarkWorkflow.run, parent, id=queue, task_queue=queue
        )
        result = await asyncio.wait_for(handle.result(), 30)
        history = await handle.fetch_history()
    assert result["determinate_trials"] == 2 and result["indeterminate_trials"] == 1
    with psycopg.connect(dsn) as conn:
        failed = conn.execute(
            "SELECT result FROM benchmark_trials WHERE id=%s", (ids[1],)
        ).fetchone()[0]
        assert failed["failure_category"] == "evaluator"
    assert (
        await Replayer(workflows=[BenchmarkWorkflow]).replay_workflow(history)
    ).replay_failure is None


async def test_benchmark_cancellation_finalizes_pending_trials(
    dsn, benchmark_factory, benchmark_temporal, monkeypatch
):
    parent, _ids, _ = benchmark_factory(ids=["cs-001", "cs-002"])
    monkeypatch.setenv("DATABASE_URL", dsn)
    started = asyncio.Event()

    @activity.defn(name="benchmark_trial")
    async def waiting(trial_id: str) -> dict:
        started.set()
        while True:
            activity.heartbeat()
            await asyncio.sleep(0.1)

    client = await Client.connect(benchmark_temporal)
    queue = "benchmark-cancel-" + str(uuid.uuid4())
    async with Worker(
        client,
        task_queue=queue,
        workflows=[BenchmarkWorkflow],
        activities=[
            activities.benchmark_plan,
            waiting,
            activities.benchmark_fail_trial,
            activities.benchmark_finalize,
        ],
    ):
        handle = await client.start_workflow(
            BenchmarkWorkflow.run, parent, id=queue, task_queue=queue
        )
        await asyncio.wait_for(started.wait(), 15)
        await handle.cancel()
        with pytest.raises(WorkflowFailureError) as error:
            await asyncio.wait_for(handle.result(), 30)
        assert isinstance(error.value.cause, CancelledError)
    with psycopg.connect(dsn) as conn:
        status, metrics = conn.execute(
            "SELECT status,result FROM benchmark_evaluations WHERE id=%s", (parent,)
        ).fetchone()
        assert status == "cancelled" and metrics["indeterminate_trials"] == 2
        assert metrics["cohort"]["planned_slots"] == 2
        assert metrics["cohort"]["terminal_trials"] == 2
        assert metrics["cohort"]["cancelled_trials"] == 2
        assert metrics["task_success_rate"]["value"] is None
        assert metrics["end_to_end_valid_completion"]["value"] == 0
        assert all(
            row[0]["failure_category"] == "cancelled"
            for row in conn.execute(
                "SELECT result FROM benchmark_trials WHERE evaluation_id=%s", (parent,)
            )
        )


async def test_benchmark_real_worker_kill_recovery(
    dsn, benchmark_factory, benchmark_temporal, tmp_path
):
    parent, ids, _ = benchmark_factory(ids=["cs-016"])
    queue = "benchmark-kill-" + str(uuid.uuid4())
    marker = tmp_path / "commit"
    processes = []

    def start(mode):
        with open(tmp_path / (mode + ".log"), "w") as output:
            proc = subprocess.Popen(
                [
                    sys.executable,
                    str(ROOT / "engine/tests/benchmark_process_worker.py"),
                    mode,
                    queue,
                    str(marker),
                ],
                cwd=ROOT,
                env={
                    **os.environ,
                    "DATABASE_URL": dsn,
                    "TEMPORAL_ADDRESS": benchmark_temporal,
                },
                stdout=output,
                stderr=subprocess.STDOUT,
            )
        processes.append(proc)
        return proc

    client = await Client.connect(benchmark_temporal)
    first = start("after_commit")
    try:
        handle = await client.start_workflow(
            BenchmarkWorkflow.run, parent, id=queue, task_queue=queue
        )
        for _ in range(400):
            if marker.exists():
                break
            if first.poll() is not None:
                pytest.fail((tmp_path / "after_commit.log").read_text())
            await asyncio.sleep(0.05)
        assert marker.exists()
        first.kill()
        await asyncio.to_thread(first.wait, timeout=10)
        start("normal")
        metrics = await asyncio.wait_for(handle.result(), 40)
        assert metrics["task_success_rate"]["value"] == 1
        with psycopg.connect(dsn) as conn:
            assert (
                conn.execute(
                    "SELECT count(*) FROM benchmark_effects WHERE trial_id=%s",
                    (ids[0],),
                ).fetchone()[0]
                == 1
            )
            attempts = conn.execute(
                "SELECT attempt,outcome FROM benchmark_attempts WHERE trial_id=%s ORDER BY attempt",
                (ids[0],),
            ).fetchall()
            assert attempts == [(1, None), (2, "completed")]
        evidence = ROOT / ".local/implementation-03"
        evidence.mkdir(parents=True, exist_ok=True)
        if os.getenv("EVIDENCE_DIR"):
            evidence = __import__("pathlib").Path(os.environ["EVIDENCE_DIR"])
        (evidence / "benchmark-kill-recovery.json").write_text(
            json.dumps(
                {
                    "evaluation_id": parent,
                    "trial_id": ids[0],
                    "attempts": attempts,
                    "metrics": metrics,
                },
                indent=2,
            )
        )
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            await asyncio.to_thread(process.wait, timeout=10)


async def test_call_budget_is_enforced(dsn, benchmark_factory):
    _, ids, _ = benchmark_factory("support-blind", ["cs-024"])
    result = await execute_trial(ids[0], dsn)
    assert result["verdict"] == "FAIL" and result["failure_category"] == "agent"
    assert (
        next(a for a in result["assertions"] if a["id"] == "execution_limits")[
            "verdict"
        ]
        == "FAIL"
    )
    assert result["effect_counts"] == {}


async def test_wall_budget_is_enforced(dsn, benchmark_factory, monkeypatch):
    from agentproof.benchmark import execution

    _, ids, _ = benchmark_factory(ids=["cs-001"])

    async def stalled(*args, **kwargs):
        await asyncio.sleep(60)

    monkeypatch.setattr(execution, "run_reference", stalled)
    result = await asyncio.wait_for(execute_trial(ids[0], dsn), 10)
    assert result["verdict"] == "FAIL" and result["failure_category"] == "agent"
    assert (
        next(a for a in result["assertions"] if a["id"] == "execution_limits")[
            "verdict"
        ]
        == "FAIL"
    )


def test_evaluator_detects_independent_state_and_security_errors():
    sc = dataset().scenarios[0]
    state = sc.fixture.order.model_dump()
    state["status"] = "cancelled"
    event = {
        "sequence": 1,
        "tool": "cancel_order",
        "arguments_valid": True,
        "authorized": True,
        "effect": "cancellation",
        "fault": "none",
        "response": {},
    }
    kwargs = {
        "trial_id": "test",
        "scenario": sc,
        "state": state,
        "events": [event],
        "effects": [{"kind": "cancellation"}],
        "outcome": AgentOutcome(resolution="completed"),
        "evaluator_sha": "a" * 64,
        "trace_id": "b" * 32,
        "latency_ms": 1.0,
    }
    assert evaluate(**kwargs).verdict == "PASS"
    for field, value in [
        ("state", {**state, "address": "unexpected"}),
        ("effects", [{"kind": "cancellation"}, {"kind": "cancellation"}]),
        ("events", [{**event, "authorized": False}]),
        ("events", [{**event, "arguments_valid": False}]),
    ]:
        bad = copy.deepcopy(kwargs)
        bad[field] = value
        assert evaluate(**bad).verdict == "FAIL"


def test_known_cost_and_partial_coverage_metrics():
    base = {
        "verdict": "PASS",
        "scenario_id": "a",
        "unsafe_actions": 0,
        "valid_tool_calls": 1,
        "tool_calls": 1,
        "recovery_eligible": False,
        "recovery_succeeded": None,
        "duplicate_side_effects": False,
        "latency_ms": 10.0,
        "input_tokens": 10,
        "output_tokens": 3,
        "inference_cost_usd": 1.0,
    }
    rows = [
        base,
        {
            **base,
            "verdict": "FAIL",
            "scenario_id": "b",
            "latency_ms": 20.0,
            "inference_cost_usd": 5.0,
        },
        {**base, "verdict": "INDETERMINATE", "scenario_id": "c"},
    ]
    m = summarize(rows)
    assert (
        m["task_success_rate"]["sample_count"] == 2
        and m["task_success_rate"]["value"] == 0.5
    )
    assert m["indeterminate_trials"] == 1 and m["execution_latency_ms"][
        "p95"
    ] == pytest.approx(19.0)
    assert m["execution_latency_ms"]["sample_count"] == 3
    assert m["cost_per_success_usd"]["value"] == 7.0
    assert m["estimated_cost_usd"]["value"] == 7.0
