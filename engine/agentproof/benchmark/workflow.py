import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import (
    ActivityError,
    ApplicationError,
    is_cancelled_exception,
)


@workflow.defn
class BenchmarkWorkflow:
    @workflow.run
    async def run(self, evaluation_id: str) -> dict:
        try:
            trials = await workflow.execute_activity(
                "benchmark_plan",
                evaluation_id,
                start_to_close_timeout=timedelta(seconds=20),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            for trial_id in trials:
                try:
                    await workflow.execute_activity(
                        "benchmark_trial",
                        trial_id,
                        start_to_close_timeout=timedelta(seconds=45),
                        schedule_to_close_timeout=timedelta(minutes=3),
                        heartbeat_timeout=timedelta(seconds=5),
                        retry_policy=RetryPolicy(maximum_attempts=3),
                    )
                except ActivityError as error:
                    if is_cancelled_exception(error):
                        raise asyncio.CancelledError() from error
                    cause = error.cause
                    category = (
                        "evaluator"
                        if isinstance(cause, ApplicationError)
                        and cause.type == "EvaluatorFailure"
                        else "infrastructure"
                    )
                    await workflow.execute_activity(
                        "benchmark_fail_trial",
                        {
                            "trial_id": trial_id,
                            "category": category,
                            "message": category + "_failure",
                        },
                        start_to_close_timeout=timedelta(seconds=20),
                        retry_policy=RetryPolicy(maximum_attempts=0),
                    )
            return await workflow.execute_activity(
                "benchmark_finalize",
                {"evaluation_id": evaluation_id},
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=0),
            )
        except asyncio.CancelledError:
            await asyncio.shield(
                workflow.execute_activity(
                    "benchmark_finalize",
                    {
                        "evaluation_id": evaluation_id,
                        "category": "cancelled",
                        "message": "Temporal cancellation requested",
                    },
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=RetryPolicy(maximum_attempts=0),
                )
            )
            raise
        except ActivityError as error:
            if is_cancelled_exception(error):
                await asyncio.shield(
                    workflow.execute_activity(
                        "benchmark_finalize",
                        {
                            "evaluation_id": evaluation_id,
                            "category": "cancelled",
                            "message": "Temporal cancellation requested",
                        },
                        start_to_close_timeout=timedelta(seconds=30),
                        retry_policy=RetryPolicy(maximum_attempts=0),
                    )
                )
                raise asyncio.CancelledError() from error
            return await workflow.execute_activity(
                "benchmark_finalize",
                {
                    "evaluation_id": evaluation_id,
                    "category": "infrastructure",
                    "message": "infrastructure_failure",
                },
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=0),
            )
