import os
import uuid

import pytest
from agentproof.workflow import EvaluationWorkflow
from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Worker


@activity.defn(name="execute_evaluation")
async def success(run_id: str) -> dict:
    return {"status": "passed", "run_id": run_id}


@activity.defn(name="execute_evaluation")
async def broken(run_id: str) -> dict:
    raise RuntimeError("injected infrastructure failure")


@activity.defn(name="record_terminal_failure")
async def record(data: dict):
    return {"status": "infrastructure_failed"}


@pytest.mark.parametrize("fails", [False, True])
async def test_real_temporal_workflow(fails):
    address = os.getenv("TEST_TEMPORAL_ADDRESS")
    if not address:
        pytest.skip("Set TEST_TEMPORAL_ADDRESS for real Temporal integration")
    client = await Client.connect(address)
    queue = "agentproof-test-" + str(uuid.uuid4())
    async with Worker(
        client,
        task_queue=queue,
        workflows=[EvaluationWorkflow],
        activities=[broken if fails else success, record],
    ):
        result = await client.execute_workflow(
            EvaluationWorkflow.run, "test-run", id=queue, task_queue=queue
        )
    assert result["status"] == ("infrastructure_failed" if fails else "passed")


@activity.defn(name="record_infrastructure_failure")
async def legacy_record(run_id: str):
    return None


@pytest.mark.parametrize("fails", [False, True])
async def test_implementation_01_history_replays(fails):
    from legacy_workflow import LegacyEvaluationWorkflow
    from temporalio.worker import Replayer

    address = os.getenv("TEST_TEMPORAL_ADDRESS")
    if not address:
        pytest.skip("Set TEST_TEMPORAL_ADDRESS for replay verification")
    client = await Client.connect(address)
    queue = "legacy-" + str(uuid.uuid4())
    async with Worker(
        client,
        task_queue=queue,
        workflows=[LegacyEvaluationWorkflow],
        activities=[broken if fails else success, legacy_record],
    ):
        handle = await client.start_workflow(
            "EvaluationWorkflow", "legacy-run", id=queue, task_queue=queue
        )
        await handle.result()
        history = await handle.fetch_history()
    result = await Replayer(workflows=[EvaluationWorkflow]).replay_workflow(history)
    assert result.replay_failure is None
