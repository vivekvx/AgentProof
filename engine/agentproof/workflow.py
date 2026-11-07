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
class EvaluationWorkflow:
    @workflow.run
    async def run(self, run_id: str) -> dict:
        if not workflow.patched("reliability-v2"):
            # Replay pre-upgrade histories with their original command sequence.
            try:
                return await workflow.execute_activity(
                    "execute_evaluation",
                    run_id,
                    start_to_close_timeout=timedelta(seconds=45),
                    schedule_to_close_timeout=timedelta(minutes=3),
                    retry_policy=RetryPolicy(maximum_attempts=3),
                )
            except ActivityError:
                await workflow.execute_activity(
                    "record_infrastructure_failure",
                    run_id,
                    start_to_close_timeout=timedelta(seconds=15),
                    schedule_to_close_timeout=timedelta(minutes=2),
                    retry_policy=RetryPolicy(maximum_attempts=5),
                )
                return {"status": "infrastructure_failed"}
        try:
            return await workflow.execute_activity(
                "execute_evaluation",
                run_id,
                start_to_close_timeout=timedelta(seconds=45),
                schedule_to_close_timeout=timedelta(minutes=3),
                heartbeat_timeout=timedelta(seconds=5),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
        except asyncio.CancelledError:
            await self.cancel(run_id)
            raise
        except ActivityError as error:
            if is_cancelled_exception(error):
                await self.cancel(run_id)
                raise asyncio.CancelledError() from error
            cause = error.cause
            kind = (
                "evaluator"
                if isinstance(cause, ApplicationError)
                and cause.type == "EvaluatorFailure"
                else "infrastructure"
            )
            return await workflow.execute_activity(
                "record_terminal_failure",
                {
                    "run_id": run_id,
                    "kind": kind,
                    "error_type": getattr(cause, "type", type(cause).__name__),
                    "message": str(cause),
                },
                start_to_close_timeout=timedelta(seconds=20),
                retry_policy=RetryPolicy(maximum_attempts=0),
            )

    async def cancel(self, run_id):
        await asyncio.shield(
            workflow.execute_activity(
                "record_terminal_failure",
                {
                    "run_id": run_id,
                    "kind": "cancelled",
                    "error_type": "CancelledError",
                    "message": "Temporal cancellation requested",
                },
                start_to_close_timeout=timedelta(seconds=20),
                retry_policy=RetryPolicy(maximum_attempts=0),
            )
        )
