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
    def __init__(self):
        self.external_completions: dict[str, dict] = {}
        self.external_open: set[str] = set()
        self.external_sessions: dict[str, dict] = {}
        self.evaluation_id = ""

    @workflow.query(name="external_trial_session")
    def external_trial_session(self, trial_id: str) -> dict:
        session = self.external_sessions.get(trial_id)
        if trial_id not in self.external_open or session is None:
            raise ValueError("external trial is not waiting")
        return session

    @workflow.query(name="external_trial_waiting")
    def external_trial_waiting(self, trial_id: str) -> bool:
        return trial_id in self.external_open

    @workflow.update(name="complete_external_trial")
    async def complete_external_trial(self, payload: dict) -> dict:
        trial_id = payload.get("trial_id")
        if not isinstance(trial_id, str) or trial_id not in self.external_open:
            raise ValueError("external trial is not waiting")
        if trial_id in self.external_completions:
            raise ValueError("external completion already received")
        result = await workflow.execute_activity(
            "benchmark_external_complete",
            payload,
            start_to_close_timeout=timedelta(seconds=15),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
        if not result.get("accepted"):
            return result
        self.external_completions[trial_id] = result
        return {"accepted": True}

    @workflow.update(name="external_tool_call")
    async def external_tool_call(self, payload: dict) -> dict:
        result = await workflow.execute_activity(
            "benchmark_external_tool",
            payload,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
        return result

    @external_tool_call.validator
    def validate_external_tool_call(self, payload: dict) -> None:
        required = {
            "schema_version",
            "run_id",
            "trial_id",
            "attempt",
            "sequence",
            "operation_id",
            "tool",
            "arguments",
        }
        trial_id = payload.get("trial_id") if isinstance(payload, dict) else None
        session = self.external_sessions.get(trial_id)
        if (
            not isinstance(payload, dict)
            or set(payload) != required
            or trial_id not in self.external_open
            or session is None
            or payload.get("run_id") != session.get("run_id")
            or payload.get("attempt") != session.get("attempt")
        ):
            raise ValueError("external tool identity or state is invalid")

    @complete_external_trial.validator
    def validate_external_completion(self, payload: dict) -> None:
        required = {
            "run_id",
            "trial_id",
            "attempt",
            "last_sequence",
            "outcome",
        }
        trial_id = payload.get("trial_id") if isinstance(payload, dict) else None
        session = self.external_sessions.get(trial_id)
        if (
            not isinstance(payload, dict)
            or set(payload) != required
            or trial_id not in self.external_open
            or trial_id in self.external_completions
            or session is None
            or payload.get("attempt") != session.get("attempt")
            or payload.get("run_id") != session.get("run_id")
        ):
            raise ValueError("external session identity or state is invalid")

    @workflow.update(name="cancel_external_trial")
    async def cancel_external_trial(self, payload: dict) -> dict:
        result = await workflow.execute_activity(
            "benchmark_external_cancel",
            payload,
            start_to_close_timeout=timedelta(seconds=15),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
        if not result.get("accepted"):
            return result
        self.external_completions[payload["trial_id"]] = {"cancelled": True}
        return {"accepted": True, **result}

    @cancel_external_trial.validator
    def validate_external_cancellation(self, payload: dict) -> None:
        trial_id = payload.get("trial_id") if isinstance(payload, dict) else None
        session = self.external_sessions.get(trial_id)
        if (
            not isinstance(payload, dict)
            or set(payload) != {"run_id", "trial_id", "attempt"}
            or trial_id not in self.external_open
            or session is None
            or payload.get("run_id") != session.get("run_id")
            or payload.get("attempt") != session.get("attempt")
            or trial_id in self.external_completions
        ):
            raise ValueError("external session identity or state is invalid")

    @workflow.run
    async def run(self, evaluation_id: str) -> dict:
        self.evaluation_id = evaluation_id
        try:
            trials = await workflow.execute_activity(
                "benchmark_plan",
                evaluation_id,
                start_to_close_timeout=timedelta(seconds=20),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            for trial_id in trials:
                try:
                    mode = await workflow.execute_activity(
                        "benchmark_execution_mode",
                        trial_id,
                        start_to_close_timeout=timedelta(seconds=10),
                        retry_policy=RetryPolicy(maximum_attempts=2),
                    )
                    if mode == "external":
                        session = await workflow.execute_activity(
                            "benchmark_external_wait",
                            trial_id,
                            start_to_close_timeout=timedelta(seconds=15),
                            retry_policy=RetryPolicy(maximum_attempts=2),
                        )
                        self.external_open.add(trial_id)
                        self.external_sessions[trial_id] = session
                        try:
                            await workflow.wait_condition(
                                lambda waiting_trial=trial_id: (
                                    waiting_trial in self.external_completions
                                ),
                                timeout=timedelta(seconds=session["ttl_seconds"]),
                            )
                            completed = True
                        except TimeoutError:
                            completed = False
                        self.external_open.discard(trial_id)
                        outcome = (
                            self.external_completions.pop(trial_id)
                            if completed
                            else {"expired": True, "trial_id": trial_id}
                        )
                        if not completed:
                            await workflow.execute_activity(
                                "benchmark_external_terminal",
                                {
                                    "trial_id": trial_id,
                                    "attempt": session["attempt"],
                                    "state": "expired",
                                },
                                start_to_close_timeout=timedelta(seconds=15),
                                retry_policy=RetryPolicy(maximum_attempts=1),
                            )
                        await workflow.execute_activity(
                            "benchmark_external_finalize",
                            {
                                "trial_id": trial_id,
                                "completion": outcome,
                                "attempt": session["attempt"],
                            },
                            start_to_close_timeout=timedelta(seconds=30),
                            retry_policy=RetryPolicy(maximum_attempts=1),
                        )
                        self.external_sessions.pop(trial_id, None)
                        continue
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
            await workflow.wait_condition(workflow.all_handlers_finished)
            return await workflow.execute_activity(
                "benchmark_finalize",
                {"evaluation_id": evaluation_id},
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=0),
            )
        except asyncio.CancelledError:
            await asyncio.shield(
                workflow.wait_condition(workflow.all_handlers_finished)
            )
            for trial_id, session in list(self.external_sessions.items()):
                try:
                    completion = self.external_completions.pop(trial_id, None)
                    if completion is None:
                        await asyncio.shield(
                            workflow.execute_activity(
                                "benchmark_external_terminal",
                                {
                                    "trial_id": trial_id,
                                    "attempt": session["attempt"],
                                    "state": "cancelled",
                                },
                                start_to_close_timeout=timedelta(seconds=15),
                                retry_policy=RetryPolicy(maximum_attempts=1),
                            )
                        )
                        completion = {"cancelled": True}
                    await asyncio.shield(
                        workflow.execute_activity(
                            "benchmark_external_finalize",
                            {
                                "trial_id": trial_id,
                                "completion": completion,
                                "attempt": session["attempt"],
                            },
                            start_to_close_timeout=timedelta(seconds=30),
                            retry_policy=RetryPolicy(maximum_attempts=1),
                        )
                    )
                except (
                    ActivityError
                ):  # Keep evaluation cancellation finalization available.
                    workflow.logger.error(
                        "Could not finalize cancelled external trial %s", trial_id
                    )
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
            if isinstance(error.cause, ApplicationError) and error.cause.type in {
                "EvaluationMissing",
                "TrialMissing",
            }:
                raise ApplicationError(
                    "evaluation_evidence_missing",
                    type="EvaluationEvidenceMissing",
                    non_retryable=True,
                ) from None
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
