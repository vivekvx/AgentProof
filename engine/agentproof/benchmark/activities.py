import asyncio
from uuid import UUID

from opentelemetry import trace
from temporalio import activity
from temporalio.exceptions import ApplicationError

from ..agent_protocol import AgentToolCall
from ..database import connect
from ..reliability import EvaluatorFailure
from .contracts import Scenario
from .execution import (
    execute_trial,
    execution_mode,
    fail_trial,
    finalize,
    finalize_external_trial,
    plan,
    prepare_external_trial,
)
from .external_sessions import (
    SessionError,
    cancel_authorized,
    complete_authorized,
    expire,
    load_authorized,
    terminate_waiting,
)
from .simulator import ARGUMENTS, OrderSimulator, ReplayMismatch


@activity.defn(name="benchmark_plan")
async def benchmark_plan(evaluation_id: str) -> list[str]:
    try:
        return await asyncio.to_thread(plan, evaluation_id)
    except ValueError as error:
        if str(error) != "unknown evaluation":
            raise
        raise ApplicationError(
            "evaluation_missing", type="EvaluationMissing", non_retryable=True
        ) from None


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


@activity.defn(name="benchmark_execution_mode")
async def benchmark_execution_mode(trial_id: str) -> str:
    return await asyncio.to_thread(execution_mode, trial_id)


@activity.defn(name="benchmark_external_wait")
async def benchmark_external_wait(trial_id: str) -> dict:
    info = activity.info()
    return await asyncio.to_thread(prepare_external_trial, trial_id, info.attempt)


@activity.defn(name="benchmark_external_complete")
async def benchmark_external_complete(payload: dict) -> dict:
    required = {
        "run_id",
        "trial_id",
        "attempt",
        "last_sequence",
        "outcome",
    }
    if set(payload) != required:
        raise ApplicationError("invalid external completion", non_retryable=True)

    def persist():
        with connect() as conn:
            session = load_authorized(conn, payload["trial_id"], payload["attempt"])
            if str(session.evaluation_id) != payload["run_id"]:
                raise ValueError("external run identity mismatch")
            if session.state != "waiting":
                raise ValueError("external session is not waiting")
            if (
                type(payload["last_sequence"]) is not int
                or not 0 <= payload["last_sequence"] <= 32
            ):
                raise ValueError("invalid completion sequence")
            complete_authorized(
                conn,
                payload["trial_id"],
                payload["attempt"],
                {**payload["outcome"], "last_sequence": payload["last_sequence"]},
            )
        return {
            "accepted": True,
            "trial_id": payload["trial_id"],
            "outcome": {
                **payload["outcome"],
                "last_sequence": payload["last_sequence"],
            },
        }

    try:
        return await asyncio.to_thread(persist)
    except SessionError as error:
        if "expired" in str(error):
            await asyncio.to_thread(_expire_external_session, payload["trial_id"])
            code = "deadline_exceeded"
        elif "credential" in str(error) or "identity" in str(error):
            code = "unauthorized"
        else:
            code = "unavailable"
        return {"accepted": False, "error_code": code}


@activity.defn(name="benchmark_external_tool")
async def benchmark_external_tool(payload: dict) -> dict:
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
    if set(payload) != required:
        raise ApplicationError("invalid external tool request", non_retryable=True)

    def persist():
        from pydantic import ValidationError

        try:
            call_data = {
                key: value
                for key, value in payload.items()
                if key not in {"credential", "attempt"}
            }
            for field in ("run_id", "trial_id", "operation_id"):
                call_data[field] = UUID(str(call_data[field]))
            call = AgentToolCall.model_validate(call_data)
        except ValidationError:
            return external_tool_error(payload, "invalid_arguments")
        try:
            with connect() as conn, conn.transaction():
                session = load_authorized(conn, payload["trial_id"], payload["attempt"])
                if str(session.evaluation_id) != payload["run_id"]:
                    return external_tool_error(payload, "unauthorized")
                row = conn.execute(
                    "SELECT t.scenario,t.result FROM benchmark_trials t WHERE t.id=%s FOR UPDATE",
                    (payload["trial_id"],),
                ).fetchone()
                if row is None or row[1] is not None or session.state != "waiting":
                    return external_tool_error(payload, "unavailable")
                scenario = Scenario.model_validate(row[0])
                prior = conn.execute(
                    "SELECT sequence,payload FROM benchmark_events WHERE trial_id=%s AND payload->>'operation_id'=%s",
                    (payload["trial_id"], str(call.operation_id)),
                ).fetchone()
                if prior:
                    event = prior[1]
                    if (
                        prior[0] != call.sequence
                        or event.get("tool") != call.tool
                        or event.get("arguments") != call.arguments
                    ):
                        return external_tool_error(payload, "replay_mismatch")
                    return external_tool_result(payload, event["response"])
                latest = conn.execute(
                    "SELECT COALESCE(max(sequence),0) FROM benchmark_events WHERE trial_id=%s",
                    (payload["trial_id"],),
                ).fetchone()[0]
                if call.sequence != latest + 1:
                    return external_tool_error(payload, "replay_mismatch")
                simulator = OrderSimulator(
                    conn,
                    payload["trial_id"],
                    scenario,
                    attempt=payload["attempt"],
                    record_receipts=True,
                )
                response = asyncio.run(
                    simulator.call(
                        call.tool,
                        call.arguments,
                        sequence=call.sequence,
                        operation_id=str(call.operation_id),
                        record_requested_tool=call.tool not in ARGUMENTS,
                    )
                )
                if call.tool not in ARGUMENTS:
                    return external_tool_error(payload, "unknown_tool")
                return external_tool_result(payload, response)
        except ValueError as error:
            message = str(error)
            if "expired" in message:
                _expire_external_session(payload["trial_id"])
            code = (
                "deadline_exceeded"
                if "expired" in message
                else "unauthorized"
                if "credential" in message or "identity" in message
                else "unavailable"
            )
            return external_tool_error(payload, code)
        except ReplayMismatch:
            return external_tool_error(payload, "replay_mismatch")

    result = await asyncio.to_thread(persist)
    trace.get_current_span().set_attributes(
        {
            "agentproof.run_id": payload.get("run_id", ""),
            "agentproof.trial_id": payload.get("trial_id", ""),
            "agentproof.operation_id": payload.get("operation_id", ""),
            "agentproof.tool": payload.get("tool", ""),
        }
    )
    return result


@activity.defn(name="benchmark_external_cancel")
async def benchmark_external_cancel(payload: dict) -> dict:
    if set(payload) != {"run_id", "trial_id", "attempt"}:
        raise ApplicationError("invalid external cancellation", non_retryable=True)

    def persist():
        try:
            with connect() as conn, conn.transaction():
                session = load_authorized(conn, payload["trial_id"], payload["attempt"])
                if str(session.evaluation_id) != payload["run_id"]:
                    return {"accepted": False, "error_code": "unauthorized"}
                cancelled = cancel_authorized(
                    conn, payload["trial_id"], payload["attempt"]
                )
                return {"accepted": True, "state": cancelled.state}
        except SessionError as error:
            if "expired" in str(error):
                with connect() as conn:
                    expire(conn, payload["trial_id"])
                return {"accepted": False, "error_code": "deadline_exceeded"}
            return {"accepted": False, "error_code": "unavailable"}

    return await asyncio.to_thread(persist)


def _expire_external_session(trial_id):
    with connect() as conn:
        expire(conn, trial_id)


def external_tool_error(payload, code):
    return {
        "schema_version": "1.0",
        "run_id": payload.get("run_id", ""),
        "trial_id": payload.get("trial_id", ""),
        "sequence": payload.get("sequence", 1),
        "operation_id": payload.get(
            "operation_id", "00000000-0000-0000-0000-000000000000"
        ),
        "ok": False,
        "error_code": code,
    }


def external_tool_result(payload, response):
    if response.get("error") == "permission_denied":
        return external_tool_error(payload, "unauthorized")
    if response.get("error") == "invalid_arguments":
        return external_tool_error(payload, "invalid_arguments")
    if response.get("error") == "execution_limit":
        return external_tool_error(payload, "budget_exhausted")
    if response.get("error") == "timeout":
        return external_tool_error(payload, "unavailable")
    if response.get("malformed"):
        return external_tool_error(payload, "unavailable")
    return {
        "schema_version": "1.0",
        "run_id": payload["run_id"],
        "trial_id": payload["trial_id"],
        "sequence": payload["sequence"],
        "operation_id": payload["operation_id"],
        "ok": True,
        "result": response,
    }


@activity.defn(name="benchmark_external_finalize")
async def benchmark_external_finalize(payload: dict) -> dict:
    return await asyncio.to_thread(
        finalize_external_trial,
        payload["trial_id"],
        payload["completion"],
        payload["attempt"],
    )


@activity.defn(name="benchmark_external_terminal")
async def benchmark_external_terminal(payload: dict) -> dict:
    if set(payload) != {"trial_id", "attempt", "state"}:
        raise ApplicationError("invalid external terminal request", non_retryable=True)

    def persist():
        with connect() as conn, conn.transaction():
            session = terminate_waiting(
                conn, payload["trial_id"], payload["attempt"], payload["state"]
            )
            return {"trial_id": str(session.trial_id), "state": session.state}

    return await asyncio.to_thread(persist)


@activity.defn(name="benchmark_fail_trial")
async def benchmark_fail_trial(data: dict) -> dict:
    try:
        return await asyncio.to_thread(fail_trial, **data)
    except ValueError as error:
        if str(error) != "unknown benchmark trial":
            raise
        raise ApplicationError(
            "trial_missing", type="TrialMissing", non_retryable=True
        ) from None


@activity.defn(name="benchmark_finalize")
async def benchmark_finalize(data: dict) -> dict:
    try:
        return await asyncio.to_thread(finalize, **data)
    except ValueError as error:
        if str(error) != "unknown evaluation":
            raise
        raise ApplicationError(
            "evaluation_missing", type="EvaluationMissing", non_retryable=True
        ) from None


ACTIVITIES = [
    benchmark_plan,
    benchmark_trial,
    benchmark_fail_trial,
    benchmark_finalize,
    benchmark_execution_mode,
    benchmark_external_wait,
    benchmark_external_complete,
    benchmark_external_finalize,
    benchmark_external_terminal,
    benchmark_external_tool,
    benchmark_external_cancel,
]
