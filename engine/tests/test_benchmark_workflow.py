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
from temporalio.client import Client, WorkflowFailureError, WorkflowUpdateFailedError
from temporalio.exceptions import ApplicationError, CancelledError
from temporalio.service import RPCError
from temporalio.testing import ActivityEnvironment
from temporalio.worker import Replayer, Worker


def external_agent_config(ttl=30):
    return {
        "schema_version": "3.0",
        "id": "external-agent",
        "version": 1,
        "title": "External agent",
        "policy": "external",
        "model_configuration": {
            "provider": "offline",
            "model": "external-agent",
            "external_calls": False,
        },
        "prompt": "Use the public request and permitted tools only.",
        "prompt_version": "support-prompt-v1",
        "tool_schema_version": "support-tools-v1",
        "max_retries": 0,
        "external_session": {
            "execution_mode": "external",
            "transport": "http-json",
            "session_ttl_seconds": ttl,
            "credential_scope": "single-trial",
        },
    }


def install_external_profile(tmp_path, monkeypatch, ttl=30):
    config = external_agent_config(ttl)
    raw = json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
    (tmp_path / "external-agent.v1.json").write_bytes(raw)
    monkeypatch.setenv("AGENTPROOF_MODEL_CONFIG_DIR", str(tmp_path))
    return config


async def wait_for_external_session(dsn, handle, trial_id):
    for _ in range(300):
        with psycopg.connect(dsn) as conn:
            row = conn.execute(
                "SELECT state,attempt FROM benchmark_external_sessions WHERE trial_id=%s",
                (trial_id,),
            ).fetchone()
        if row and await handle.query(
            BenchmarkWorkflow.external_trial_waiting, trial_id
        ):
            return row
        await asyncio.sleep(0.05)
    raise AssertionError("external session did not enter the waiting state")


def external_worker(client, queue, waiting_activity=None, trial_activity=None):
    registered = list(activities.ACTIVITIES)
    if waiting_activity is not None:
        registered = [
            waiting_activity if item is activities.benchmark_external_wait else item
            for item in registered
        ]
    if trial_activity is not None:
        registered = [
            trial_activity if item is activities.benchmark_trial else item
            for item in registered
        ]
    return Worker(
        client, task_queue=queue, workflows=[BenchmarkWorkflow], activities=registered
    )


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
            activities.benchmark_execution_mode,
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


async def test_external_trial_waits_recovers_and_evaluates_trusted_evidence(
    dsn, benchmark_factory, benchmark_temporal, monkeypatch, tmp_path
):
    config = install_external_profile(tmp_path, monkeypatch)
    parent, ids, _ = benchmark_factory(ids=["cs-001"], version=5, agent_config=config)
    trial_id = ids[0]
    monkeypatch.setenv("DATABASE_URL", dsn)
    captured = {}

    @activity.defn(name="benchmark_external_wait")
    async def capture_session(value: str) -> dict:
        result = await activities.benchmark_external_wait(value)
        captured[value] = result
        return result

    bundled_calls = []

    @activity.defn(name="benchmark_trial")
    async def forbidden_bundled_agent(value: str) -> dict:
        bundled_calls.append(value)
        raise AssertionError("external execution called the bundled agent")

    client = await Client.connect(benchmark_temporal)
    queue = "benchmark-external-" + str(uuid.uuid4())
    handle = await client.start_workflow(
        BenchmarkWorkflow.run, parent, id=queue, task_queue=queue
    )
    async with external_worker(client, queue, capture_session, forbidden_bundled_agent):
        row = await wait_for_external_session(dsn, handle, trial_id)
        assert row == ("waiting", 1)
        assert trial_id in captured
        session = captured[trial_id]
        with psycopg.connect(dsn) as conn:
            stored = conn.execute(
                "SELECT evaluation_id,scenario_id,attempt,state,credential_hash "
                "FROM benchmark_external_sessions WHERE trial_id=%s",
                (trial_id,),
            ).fetchone()
            assert stored[:4] == (
                uuid.UUID(parent),
                "cs-001",
                session["attempt"],
                "waiting",
            )
            assert len(stored[4]) == 64
            assert all(c in "0123456789abcdef" for c in stored[4])
        assert session["session_id"] == trial_id

    # A worker restart must continue the same workflow and session.
    async with external_worker(client, queue, capture_session, forbidden_bundled_agent):
        await wait_for_external_session(dsn, handle, trial_id)
        with psycopg.connect(dsn) as conn:
            awaitable_scenario = conn.execute(
                "SELECT scenario FROM benchmark_trials WHERE id=%s", (trial_id,)
            ).fetchone()[0]
            from agentproof.benchmark.contracts import Scenario
            from agentproof.benchmark.simulator import OrderSimulator

            scenario = Scenario.model_validate(awaitable_scenario)
            broker = OrderSimulator(
                conn,
                trial_id,
                scenario,
                attempt=session["attempt"],
                record_receipts=True,
            )
            await broker.call("cancel_order", {"order_id": scenario.request.order_id})

        base_payload = {
            "trial_id": trial_id,
            "attempt": session["attempt"],
            "run_id": parent,
            "last_sequence": 1,
            "outcome": {"resolution": "completed"},
        }
        with pytest.raises(WorkflowUpdateFailedError):
            await handle.execute_update(
                BenchmarkWorkflow.complete_external_trial,
                {**base_payload, "attempt": session["attempt"] + 1},
            )
        metrics = await asyncio.wait_for(
            handle.execute_update(
                BenchmarkWorkflow.complete_external_trial, base_payload
            ),
            20,
        )
        assert metrics == {"accepted": True}
        final = await asyncio.wait_for(handle.result(), 20)
        assert final["task_success_rate"]["value"] == 1
        with pytest.raises((WorkflowUpdateFailedError, RPCError)):
            await handle.execute_update(
                BenchmarkWorkflow.complete_external_trial, base_payload
            )
        assert not bundled_calls
        with psycopg.connect(dsn) as conn:
            assert (
                conn.execute(
                    "SELECT count(*) FROM benchmark_external_sessions WHERE trial_id=%s",
                    (trial_id,),
                ).fetchone()[0]
                == 1
            )
            session_state = conn.execute(
                "SELECT state FROM benchmark_external_sessions WHERE trial_id=%s",
                (trial_id,),
            ).fetchone()[0]
            result = conn.execute(
                "SELECT result FROM benchmark_trials WHERE id=%s", (trial_id,)
            ).fetchone()[0]
            assert session_state == "completed"
            assert result["verdict"] == "PASS"
            assert result["tool_calls"] == 1
        history = await handle.fetch_history()
        assert (
            await Replayer(workflows=[BenchmarkWorkflow]).replay_workflow(history)
        ).replay_failure is None


async def test_external_claim_without_broker_evidence_cannot_pass(
    dsn, benchmark_factory, benchmark_temporal, monkeypatch, tmp_path
):
    config = install_external_profile(tmp_path, monkeypatch)
    parent, ids, _ = benchmark_factory(ids=["cs-001"], version=5, agent_config=config)
    monkeypatch.setenv("DATABASE_URL", dsn)
    captured = {}

    @activity.defn(name="benchmark_external_wait")
    async def capture_session(value: str) -> dict:
        result = await activities.benchmark_external_wait(value)
        captured[value] = result
        return result

    client = await Client.connect(benchmark_temporal)
    queue = "benchmark-external-no-evidence-" + str(uuid.uuid4())
    handle = await client.start_workflow(
        BenchmarkWorkflow.run, parent, id=queue, task_queue=queue
    )
    async with external_worker(client, queue, capture_session):
        await wait_for_external_session(dsn, handle, ids[0])
        result = await handle.execute_update(
            BenchmarkWorkflow.complete_external_trial,
            {
                "trial_id": ids[0],
                "attempt": captured[ids[0]]["attempt"],
                "run_id": parent,
                "last_sequence": 0,
                "outcome": {"resolution": "completed"},
            },
        )
        assert result["accepted"]
        await asyncio.wait_for(handle.result(), 20)
    with psycopg.connect(dsn) as conn:
        verdict = conn.execute(
            "SELECT result->>'verdict' FROM benchmark_trials WHERE id=%s", (ids[0],)
        ).fetchone()[0]
        assert verdict != "PASS"


async def test_external_expiry_and_cancellation_are_persisted(
    dsn, benchmark_factory, benchmark_temporal, monkeypatch, tmp_path
):
    config = install_external_profile(tmp_path, monkeypatch, ttl=2)
    expired_eval, expired_ids, _ = benchmark_factory(
        ids=["cs-001"], version=5, agent_config=config
    )
    cancelled_eval, cancelled_ids, _ = benchmark_factory(
        ids=["cs-002"], version=5, agent_config=config
    )
    monkeypatch.setenv("DATABASE_URL", dsn)
    client = await Client.connect(benchmark_temporal)

    async def run_trial(evaluation_id, trial_id, cancel=False):
        queue = "benchmark-external-terminal-" + str(uuid.uuid4())
        handle = await client.start_workflow(
            BenchmarkWorkflow.run, evaluation_id, id=queue, task_queue=queue
        )
        async with external_worker(client, queue):
            await wait_for_external_session(dsn, handle, trial_id)
            if cancel:
                await handle.cancel()
                with pytest.raises(WorkflowFailureError):
                    await asyncio.wait_for(handle.result(), 20)
            else:
                await asyncio.wait_for(handle.result(), 20)

    await run_trial(expired_eval, expired_ids[0])
    with psycopg.connect(dsn) as conn:
        assert (
            conn.execute(
                "SELECT state FROM benchmark_external_sessions WHERE trial_id=%s",
                (expired_ids[0],),
            ).fetchone()[0]
            == "expired"
        )
        assert (
            conn.execute(
                "SELECT result->>'verdict' FROM benchmark_trials WHERE id=%s",
                (expired_ids[0],),
            ).fetchone()[0]
            != "PASS"
        )

    await run_trial(cancelled_eval, cancelled_ids[0], cancel=True)
    with psycopg.connect(dsn) as conn:
        assert (
            conn.execute(
                "SELECT state FROM benchmark_external_sessions WHERE trial_id=%s",
                (cancelled_ids[0],),
            ).fetchone()[0]
            == "cancelled"
        )
        assert (
            conn.execute(
                "SELECT result->>'verdict' FROM benchmark_trials WHERE id=%s",
                (cancelled_ids[0],),
            ).fetchone()[0]
            != "PASS"
        )


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
            activities.benchmark_execution_mode,
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
