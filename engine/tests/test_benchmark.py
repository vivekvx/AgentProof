import asyncio
import copy
import json

import psycopg
import pytest
from agentproof.benchmark.contracts import (
    Dataset,
    PublicContext,
    RefundArguments,
    Scenario,
)
from agentproof.benchmark.execution import execute_trial, fail_trial, finalize
from agentproof.benchmark.metrics import quantile, rate, summarize
from agentproof.benchmark.registry import ROOT as CONTRACT_ROOT
from agentproof.benchmark.registry import dataset, reference
from agentproof.benchmark.simulator import OrderSimulator
from hypothesis import given
from hypothesis import strategies as st
from psycopg.types.json import Jsonb
from pydantic import ValidationError


async def test_all_reviewed_reference_outcomes(dsn, benchmark_factory):
    evaluation_id, ids, _ = benchmark_factory()
    results = [await execute_trial(i, dsn) for i in ids]
    failures = [
        (r["scenario_id"], [a["id"] for a in r["assertions"] if a["verdict"] != "PASS"])
        for r in results
        if r["verdict"] != "PASS"
    ]
    assert failures == []
    metrics = finalize(evaluation_id, dsn=dsn)
    assert metrics["total_trials"] == 32
    assert metrics["task_success_rate"]["value"] == 1
    assert metrics["inference_cost_usd"]["value"] is None
    assert metrics["token_usage"]["input"] is None


async def test_blind_policy_has_real_regressions(dsn, benchmark_factory):
    _, ids, _ = benchmark_factory(
        "support-blind", ["cs-015", "cs-016", "cs-017", "cs-020"]
    )
    results = [await execute_trial(i, dsn) for i in ids]
    by_id = {r["scenario_id"]: r for r in results}
    assert by_id["cs-015"]["verdict"] == "PASS"
    for name in ["cs-016", "cs-017", "cs-020"]:
        assert by_id[name]["verdict"] == "FAIL"
        assert by_id[name]["duplicate_side_effects"]
        assert by_id[name]["failure_category"] == "agent"


async def test_timeout_before_and_after_are_distinct(dsn, benchmark_factory):
    _, ids, _manifest = benchmark_factory(ids=["cs-015", "cs-016"])
    with psycopg.connect(dsn, autocommit=True) as conn:
        responses = []
        counts = []
        for trial_id in ids:
            raw = conn.execute(
                "SELECT scenario FROM benchmark_trials WHERE id=%s", (trial_id,)
            ).fetchone()[0]
            simulator = OrderSimulator(conn, trial_id, Scenario.model_validate(raw))
            responses.append(
                await simulator.call("cancel_order", {"order_id": "order-001"})
            )
            counts.append(
                conn.execute(
                    "SELECT count(*) FROM benchmark_effects WHERE trial_id=%s",
                    (trial_id,),
                ).fetchone()[0]
            )
        assert counts == [0, 1]
        assert responses[0] == responses[1]
        assert responses[0]["error"] == "timeout"
        assert responses[0]["order"] is None


async def test_replay_and_repeated_trials_are_isolated(dsn, benchmark_factory):
    parent, ids, _ = benchmark_factory(ids=["cs-016"], trials=3)
    results = await asyncio.gather(*(execute_trial(i, dsn) for i in ids))
    assert all(r["verdict"] == "PASS" for r in results)
    for i, r in zip(ids, results):
        assert await execute_trial(i, dsn) == r
    with psycopg.connect(dsn) as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM benchmark_effects WHERE trial_id=ANY(%s::uuid[])",
                (ids,),
            ).fetchone()[0]
            == 3
        )
    assert finalize(parent, dsn=dsn)["total_trials"] == 3


async def test_partial_harness_failure_and_terminal_fencing(dsn, benchmark_factory):
    parent, ids, _ = benchmark_factory(ids=["cs-001", "cs-002", "cs-003"])
    await execute_trial(ids[0], dsn)
    failure = fail_trial(ids[1], "evaluator", "injected evaluator failure", dsn)
    assert (
        failure["verdict"] == "INDETERMINATE"
        and failure["failure_category"] == "evaluator"
    )
    metrics = finalize(
        parent, category="cancelled", message="test cancellation", dsn=dsn
    )
    assert metrics["determinate_trials"] == 1 and metrics["indeterminate_trials"] == 2
    assert await execute_trial(ids[2], dsn) == fail_trial(ids[2], dsn=dsn)
    assert finalize(parent, dsn=dsn) == metrics
    with (
        psycopg.connect(dsn, autocommit=True) as conn,
        pytest.raises(psycopg.errors.RaiseException, match="immutable"),
    ):
        conn.execute(
            "UPDATE benchmark_trials SET state='{}'::jsonb WHERE id=%s", (ids[0],)
        )


async def test_trial_recovery_after_effect_commit(dsn, benchmark_factory, monkeypatch):
    _, ids, _ = benchmark_factory(ids=["cs-016"])
    original = OrderSimulator.call

    async def interrupted(self, *args, **kwargs):
        value = await original(self, *args, **kwargs)
        if self.sequence == 2:
            raise RuntimeError("worker interrupted after commit")
        return value

    monkeypatch.setattr(OrderSimulator, "call", interrupted)
    with pytest.raises(RuntimeError, match="interrupted"):
        await execute_trial(ids[0], dsn)
    monkeypatch.setattr(OrderSimulator, "call", original)
    result = await execute_trial(ids[0], dsn)
    assert result["verdict"] == "PASS" and result["effect_counts"] == {
        "cancellation": 1
    }


def test_private_assertions_not_in_public_context():
    sc = dataset().scenarios[15]
    public = PublicContext(
        request=sc.request, permissions=sc.permissions, limits=sc.limits
    )
    data = public.model_dump()
    assert set(data) == {
        "schema_version",
        "request",
        "permissions",
        "limits",
        "tool_schema",
    }
    assert "assertions" not in public.model_dump_json()
    assert "timeout_after_commit" not in public.model_dump_json()
    assert "timeout after commit" not in public.model_dump_json().lower()
    assert "reconcile" not in public.request.text.lower()
    assert sc.id not in public.model_dump_json()


def test_invalid_dataset_rejected():
    raw = reference("benchmarks/support.v2.json")["content"]
    for mutate in [
        lambda d: d["scenarios"].append(copy.deepcopy(d["scenarios"][0])),
        lambda d: d["scenarios"][0]["limits"].update(max_tool_calls=0),
        lambda d: d["scenarios"][0]["fault"].update(kind="unknown"),
        lambda d: d["scenarios"][0]["assertions"].update(
            forbidden_effects=["cancellation"]
        ),
        lambda d: d["scenarios"][0]["fixture"]["order"].update(refunded_cents=9999999),
    ]:
        changed = copy.deepcopy(raw)
        mutate(changed)
        with pytest.raises(ValidationError):
            Dataset.model_validate(changed)


@given(st.integers(max_value=0))
def test_nonpositive_refund_arguments_rejected(amount):
    with pytest.raises(ValidationError):
        RefundArguments(order_id="order-001", amount_cents=amount)


@given(
    st.lists(
        st.floats(min_value=0, max_value=100000, allow_nan=False, allow_infinity=False),
        min_size=1,
        max_size=40,
    )
)
def test_latency_quantiles_are_bounded(values):
    assert min(values) <= quantile(values, 0.5) <= quantile(values, 0.95) <= max(values)


@given(st.lists(st.booleans(), max_size=30))
def test_rate_counts_and_intervals(values):
    rows = [{"scenario_id": str(i), "value": v} for i, v in enumerate(values)]
    result = rate(rows, lambda r: r["value"])
    assert result["sample_count"] == len(values)
    assert result["numerator"] == sum(values)
    if not values:
        assert result["value"] is None
    elif result["interval"]:
        assert 0 <= result["interval"]["low"] <= result["interval"]["high"] <= 1


def test_unavailable_metrics_are_not_zero():
    metrics = summarize([])
    assert metrics["task_success_rate"]["value"] is None
    assert metrics["execution_latency_ms"]["p50"] is None
    assert metrics["token_usage"]["input"] is None
    assert metrics["cost_per_success_usd"]["value"] is None


@pytest.mark.parametrize(
    "scenario_id", ["cs-008", "cs-009", "cs-013", "cs-014", "cs-031"]
)
async def test_unsafe_or_invalid_requests_are_blocked(
    dsn, benchmark_factory, scenario_id
):
    _, ids, _ = benchmark_factory("support-blind", [scenario_id])
    result = await execute_trial(ids[0], dsn)
    assert result["verdict"] == "FAIL"
    assert (
        result["unsafe_actions"] > 0
        or result["valid_tool_calls"] < result["tool_calls"]
    )
    assert result["effect_counts"] == {}


def test_all_v2_schemas_match_sources():
    from agentproof.benchmark import contracts

    for path in (CONTRACT_ROOT / "v2").glob("*.schema.json"):
        model = getattr(contracts, path.name.split(".")[0])
        assert json.loads(path.read_text()) == model.model_json_schema()


def test_agent_configuration_blocks_external_calls():
    from agentproof.benchmark.contracts import AgentConfiguration

    data = reference("agents/support-safe.v1.json")["content"]
    data["model_configuration"] = {"provider": "external", "api_key": "not-a-real-key"}
    with pytest.raises(ValidationError):
        AgentConfiguration.model_validate(data)


@pytest.mark.parametrize(
    "target,payload",
    [
        ("trial", None),
        ("trial", []),
        (
            "trial",
            {"schema_version": "2.0", "verdict": "PASS", "failure_category": "agent"},
        ),
        ("evaluation", None),
    ],
)
def test_database_rejects_invalid_terminal_results(
    dsn, benchmark_factory, target, payload
):
    parent, ids, _ = benchmark_factory(ids=["cs-001"])
    table = "benchmark_trials" if target == "trial" else "benchmark_evaluations"
    identifier = ids[0] if target == "trial" else parent
    with (
        psycopg.connect(dsn, autocommit=True) as conn,
        pytest.raises(psycopg.errors.CheckViolation),
    ):
        conn.execute(
            f"UPDATE {table} SET status='completed',result=%s WHERE id=%s",
            (Jsonb(payload), identifier),
        )


async def test_agent_cannot_change_harness_authority(
    dsn, benchmark_factory, monkeypatch
):
    from agentproof.benchmark import execution
    from agentproof.benchmark.contracts import AgentOutcome

    _, ids, _ = benchmark_factory(ids=["cs-014"])

    async def altering_context(context, configuration, tool_call):
        context.request.user_approved = True
        context.permissions.approval_required = False
        context.limits.max_tool_calls = 32
        await tool_call("refund_order", {"order_id": "order-001", "amount_cents": 2500})
        return AgentOutcome(resolution="blocked")

    monkeypatch.setattr(execution, "run_reference", altering_context)
    result = await execute_trial(ids[0], dsn)
    assert result["effect_counts"] == {}
    assert result["unsafe_actions"] == 1
    assert result["final_state"]["approved"] is False
    assert result["final_state"]["refunded_cents"] == 0
