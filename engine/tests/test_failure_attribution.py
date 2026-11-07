"""T04 execution boundaries preserve safety without inventing budget misuse."""

import pytest
from agentproof.benchmark import execution
from agentproof.benchmark.simulator import AgentLimit, ReplayMismatch
from agentproof.reliability import EvaluatorFailure


@pytest.mark.parametrize("violation", [False, True])
@pytest.mark.parametrize("failure", ["deadline", "budget"])
async def test_failure_boundary_preserves_known_policy(
    benchmark_factory, dsn, monkeypatch, violation, failure
):
    _, ids, _ = benchmark_factory(ids=["cs-001"], version=5)

    async def agent(public, config, tool):
        if violation:
            await tool("cancel_order", {"order_id": "order-001"})
            await tool("cancel_order", {"order_id": "order-001"})
        if failure == "deadline":
            raise TimeoutError("must not enter stored evidence")
        raise AgentLimit("configured tool budget")

    monkeypatch.setattr(execution, "run_reference", agent)
    result = await execution.execute_trial(ids[0], dsn)
    assert result["verdict"] == (
        "FAIL" if violation or failure == "budget" else "INDETERMINATE"
    )
    assert result["assessment"]["execution"] == (
        "infrastructure" if failure == "deadline" else "completed"
    )
    assert result["assessment"]["policy"] == (
        "FAIL" if violation or failure == "budget" else "PASS"
    )
    assert result["effect_counts"].get("cancellation", 0) == (2 if violation else 0)
    assert "must not enter" not in str(result)


def test_replay_mismatch_is_nonretryable_harness_failure():
    assert isinstance(ReplayMismatch("changed tool sequence"), EvaluatorFailure)


@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        "secret",
        {"error": []},
        {"error": {"code": ["bad"]}},
        {"error": {"code": "FAKE_SECRET_KEY"}},
        {"error": {"code": "x" * 100000}},
    ],
)
def test_untrusted_provider_error_codes_are_not_retained(body):
    from agentproof.benchmark.llm import classify, provider_code

    error = RuntimeError("sensitive body")
    error.body, error.status_code = body, 400
    assert provider_code(error) is None
    assert classify(error) == "unsupported_capability_or_configuration"


def test_generation_rejection_does_not_claim_local_tool_execution():
    from agentproof.benchmark.llm import classify

    error = RuntimeError("untrusted generated text")
    error.body = {"error": {"code": "tool_use_failed", "failed_generation": "secret"}}
    error.status_code = 400
    assert classify(error) == "tool_generation_rejected"


async def test_sdk_gate_wait_is_not_agent_budget_misuse(
    benchmark_factory, dsn, monkeypatch
):
    from agentproof.benchmark.llm import _MODEL_GATE, model_gate

    _, ids, _ = benchmark_factory(ids=["cs-001"], version=5)

    async def waiting(public, config, tool):
        async with model_gate():
            raise AssertionError("gate must remain held")

    monkeypatch.setattr(execution, "run_reference", waiting)
    assert _MODEL_GATE.acquire(blocking=False)
    try:
        result = await execution.execute_trial(ids[0], dsn)
    finally:
        _MODEL_GATE.release()
    assert result["verdict"] == "INDETERMINATE"
    assert result["assessment"]["execution"] == "infrastructure"
    assert result["assessment"]["policy"] == "PASS"
    assert result["tool_calls"] == 0


@pytest.mark.parametrize("kind", ["evaluator", "infrastructure"])
async def test_activity_failure_redacts_exception_before_persistence(
    kind, benchmark_factory, dsn, monkeypatch
):
    from types import SimpleNamespace

    from agentproof.benchmark import activities
    from agentproof.database import connect
    from temporalio.exceptions import ApplicationError

    _, ids, _ = benchmark_factory(ids=["cs-001"], version=5)
    monkeypatch.setattr(activities, "connect", lambda: connect(dsn))
    monkeypatch.setattr(
        activities.activity,
        "info",
        lambda: SimpleNamespace(
            attempt=1, workflow_id="test", workflow_run_id="test", activity_id="test"
        ),
    )
    monkeypatch.setattr(activities.activity, "heartbeat", lambda *args: None)

    async def broken(*args, **kwargs):
        raise (EvaluatorFailure if kind == "evaluator" else RuntimeError)(
            "FAKE_SECRET " * 1000
        )

    monkeypatch.setattr(activities, "execute_trial", broken)
    with pytest.raises(ApplicationError) as caught:
        await activities.benchmark_trial(ids[0])
    assert "FAKE_SECRET" not in str(caught.value)
    assert caught.value.__suppress_context__
    assert caught.value.non_retryable is (kind == "evaluator")
    with connect(dsn) as c:
        row = c.execute(
            "SELECT outcome,error FROM benchmark_attempts WHERE trial_id=%s", (ids[0],)
        ).fetchone()
        assert row == (kind, "activity_" + kind)


async def test_completed_return_after_deadline_is_not_pass(
    benchmark_factory, dsn, monkeypatch
):
    from agentproof.benchmark.contracts import AgentOutcome

    _, ids, _ = benchmark_factory(ids=["cs-001"], version=5)

    async def late(public, config, tool):
        await tool("cancel_order", {"order_id": "order-001"})
        clock = execution.time.perf_counter
        monkeypatch.setattr(execution.time, "perf_counter", lambda: clock() + 10)
        return AgentOutcome(resolution="completed")

    monkeypatch.setattr(execution, "run_reference", late)
    result = await execution.execute_trial(ids[0], dsn)
    assert result["verdict"] == "INDETERMINATE"
    assert result["assessment"]["task"] == "PASS"
    assert result["assessment"]["policy"] == "PASS"
    assert result["assessment"]["execution"] == "infrastructure"


async def test_rate_limit_retry_budget_is_not_agent_misuse():
    from agentproof.benchmark.llm import ProviderFailure, run_llm
    from test_llm import Client, FakeModel, MemoryJournal, configuration, public

    error = RuntimeError("secret provider error")
    error.status_code = 429
    model = FakeModel([error])
    config = configuration(max_requests=1)
    config.max_retries = 1
    with pytest.raises(ProviderFailure, match="^rate_limit$"):
        await run_llm(
            public(), config, None, MemoryJournal(), lambda _: (model, Client())
        )
    assert model.calls == 1
