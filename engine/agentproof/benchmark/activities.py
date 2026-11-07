import asyncio

from opentelemetry import trace
from temporalio import activity
from temporalio.exceptions import ApplicationError

from ..database import connect
from ..reliability import EvaluatorFailure
from .execution import execute_trial, fail_trial, finalize, plan


@activity.defn(name="benchmark_plan")
async def benchmark_plan(evaluation_id: str) -> list[str]:
    return await asyncio.to_thread(plan, evaluation_id)


@activity.defn(name="benchmark_trial")
async def benchmark_trial(trial_id: str) -> dict:
    info = activity.info()
    identifiers = {
        "temporal.activity.attempt": info.attempt,
        "temporal.workflow.id": info.workflow_id,
        "temporal.workflow.run_id": info.workflow_run_id,
        "temporal.activity.id": info.activity_id,
    }

    def journal(outcome=None, error=None):
        with connect() as conn:
            conn.execute(
                "INSERT INTO benchmark_attempts(trial_id,workflow_run_id,activity_id,attempt) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (trial_id, info.workflow_run_id, info.activity_id, info.attempt),
            )
            if outcome:
                conn.execute(
                    "UPDATE benchmark_attempts SET finished_at=now(),outcome=%s,error=%s WHERE trial_id=%s AND workflow_run_id=%s AND activity_id=%s AND attempt=%s AND finished_at IS NULL",
                    (
                        outcome,
                        "activity_" + outcome if error else None,
                        trial_id,
                        info.workflow_run_id,
                        info.activity_id,
                        info.attempt,
                    ),
                )

    trace.get_current_span().set_attributes(
        {"agentproof.trial_id": trial_id, **identifiers}
    )
    await asyncio.to_thread(journal)

    async def heartbeat():
        while True:
            activity.heartbeat({"trial_id": trial_id, "attempt": info.attempt})
            await asyncio.sleep(1)

    pulse = asyncio.create_task(heartbeat())
    try:
        result = await execute_trial(trial_id, attempt=identifiers)
        await asyncio.to_thread(journal, "completed")
        return result
    except EvaluatorFailure as error:
        trace.get_current_span().set_attribute(
            "agentproof.failure_category", "evaluator"
        )
        await asyncio.to_thread(journal, "evaluator", error)
        raise ApplicationError(
            "evaluator_failure", type="EvaluatorFailure", non_retryable=True
        ) from None
    except asyncio.CancelledError:
        trace.get_current_span().set_attribute(
            "agentproof.failure_category", "cancelled"
        )
        raise
    except Exception as error:  # noqa: BLE001 -- redact errors at the activity boundary
        trace.get_current_span().set_attribute(
            "agentproof.failure_category", "infrastructure"
        )
        await asyncio.to_thread(journal, "infrastructure", error)
        raise ApplicationError(
            "infrastructure_failure", type="InfrastructureFailure"
        ) from None
    finally:
        pulse.cancel()
        await asyncio.gather(pulse, return_exceptions=True)


@activity.defn(name="benchmark_fail_trial")
async def benchmark_fail_trial(data: dict) -> dict:
    return await asyncio.to_thread(fail_trial, **data)


@activity.defn(name="benchmark_finalize")
async def benchmark_finalize(data: dict) -> dict:
    return await asyncio.to_thread(finalize, **data)


ACTIVITIES = [benchmark_plan, benchmark_trial, benchmark_fail_trial, benchmark_finalize]
