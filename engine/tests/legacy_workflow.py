"""Frozen Implementation 01 commands for upgrade replay regression tests."""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError


@workflow.defn(name="EvaluationWorkflow")
class LegacyEvaluationWorkflow:
    @workflow.run
    async def run(self, run_id: str) -> dict:
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
