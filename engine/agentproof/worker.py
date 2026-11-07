import asyncio
import logging
import os
import signal

from temporalio import activity
from temporalio.client import Client
from temporalio.contrib.opentelemetry import TracingInterceptor
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker

from .benchmark.activities import ACTIVITIES as BENCHMARK_ACTIVITIES
from .benchmark.workflow import BenchmarkWorkflow
from .execution import execute, record_infrastructure_failure, record_terminal_failure
from .reliability import Attempt, EvaluatorFailure, journal
from .telemetry import configure
from .workflow import EvaluationWorkflow

logger = logging.getLogger(__name__)


@activity.defn(name="execute_evaluation")
async def execute_evaluation(run_id: str) -> dict:
    info = activity.info()
    attempt = Attempt(
        info.workflow_id, info.workflow_run_id, info.activity_id, info.attempt
    )
    await asyncio.to_thread(journal, run_id, attempt)

    async def heartbeat():
        while True:
            activity.heartbeat({"run_id": run_id, "attempt": attempt.number})
            await asyncio.sleep(1)

    pulse = asyncio.create_task(heartbeat())
    try:
        result = await execute(run_id, attempt=attempt)
        await asyncio.to_thread(journal, run_id, attempt, "completed")
        return result
    except EvaluatorFailure as error:
        await asyncio.to_thread(journal, run_id, attempt, "evaluator", error)
        raise ApplicationError(
            str(error), type="EvaluatorFailure", non_retryable=True
        ) from error
    except asyncio.CancelledError:
        raise
    except Exception as error:
        logger.exception("Evaluation infrastructure error for run %s", run_id)
        await asyncio.to_thread(journal, run_id, attempt, "infrastructure", error)
        raise
    finally:
        pulse.cancel()
        await asyncio.gather(pulse, return_exceptions=True)


@activity.defn(name="record_terminal_failure")
async def terminal_failure(data: dict) -> dict:
    return await asyncio.to_thread(record_terminal_failure, **data)


# Retain this activity name for histories started by Implementation 01.
@activity.defn(name="record_infrastructure_failure")
async def failure(run_id: str):
    return await asyncio.to_thread(record_infrastructure_failure, run_id)


async def main():
    provider = configure()
    client = await Client.connect(
        os.getenv("TEMPORAL_ADDRESS", "localhost:7233"),
        interceptors=[TracingInterceptor()],
    )
    stopped = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig, stopped.set)
    try:
        async with Worker(
            client,
            task_queue="agentproof-v1",
            workflows=[EvaluationWorkflow, BenchmarkWorkflow],
            activities=[
                execute_evaluation,
                terminal_failure,
                failure,
                *BENCHMARK_ACTIVITIES,
            ],
        ):
            logger.info("AgentProof worker ready")
            await stopped.wait()
    finally:
        provider.shutdown()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
