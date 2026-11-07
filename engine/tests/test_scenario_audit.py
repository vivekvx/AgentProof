"""Independent scripted ground truths for every existing case; no reference Policy import."""

import copy
import hashlib
import json
import os
from collections import Counter
from pathlib import Path

import pytest
from agentproof.benchmark.audited_evaluator_v3 import evaluate
from agentproof.benchmark.contracts import AgentOutcome, Scenario
from agentproof.benchmark.execution import execute_trial, fail_trial
from agentproof.benchmark.llm import ProviderFailure
from agentproof.benchmark.registry import dataset, reference
from agentproof.benchmark.simulator import OrderSimulator
from agentproof.database import connect
from agentproof.reliability import EvaluatorFailure

AUDIT = json.loads(
    (Path(__file__).parent / "fixtures/scenario_audit.v1.json").read_text()
)
CASES = AUDIT["records"]
ALTERNATIVES = [(c, i, p) for c in CASES for i, p in enumerate(c["alternatives"])]


def canonical(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def load(conn, trial):
    raw, state = conn.execute(
        "SELECT scenario,state FROM benchmark_trials WHERE id=%s", (trial,)
    ).fetchone()
    events = [
        r[0]
        for r in conn.execute(
            "SELECT payload FROM benchmark_events WHERE trial_id=%s ORDER BY sequence",
            (trial,),
        )
    ]
    effects = [
        {"kind": r[0], "payload": r[1]}
        for r in conn.execute(
            "SELECT kind,payload FROM benchmark_effects WHERE trial_id=%s", (trial,)
        )
    ]
    return {
        "trial_id": trial,
        "scenario": Scenario.model_validate(raw),
        "state": state,
        "events": events,
        "effects": effects,
        "evaluator_sha": reference("evaluators/support.v3.json")["sha256"],
        "trace_id": "b" * 32,
        "latency_ms": 1.0,
    }


def save_evidence(case, label, trial, result, data):
    directory = os.environ.get("EVIDENCE_DIR")
    if directory:
        p = Path(directory) / "t01"
        p.mkdir(parents=True, exist_ok=True)
        (p / (case + "-" + label + ".json")).write_text(
            json.dumps(
                {
                    "trial_id": trial,
                    "result": result.model_dump(),
                    "events": data["events"],
                    "effects": data["effects"],
                },
                indent=2,
            )
        )


async def script(conn, trial, path):
    data = load(conn, trial)
    sim = OrderSimulator(conn, trial, data["scenario"])
    for step in path:
        await sim.call(step["tool"], step["arguments"])
    return load(conn, trial)


def test_audit_inventory_and_immutable_versions():
    old = reference("benchmarks/support.v2.json")
    new = reference("benchmarks/support.v4.json")
    assert len(CASES) == 32 and [r["scenario_id"] for r in CASES] == [
        f"cs-{i:03}" for i in range(1, 33)
    ]
    assert old["sha256"] == AUDIT["source_dataset"]["sha256"]
    assert new["sha256"] == AUDIT["audited_dataset"]["sha256"]
    assert (
        reference("evaluators/support.v3.json")["sha256"]
        == AUDIT["evaluator"]["sha256"]
    )
    assert len(dataset(4).scenarios) == 32 and dataset(4).version == 4
    for record, source, sc in zip(
        CASES, old["content"]["scenarios"], new["content"]["scenarios"], strict=True
    ):
        Scenario.model_validate(source)
        parsed = Scenario.model_validate(sc)
        assert canonical(source) == record["source_scenario_sha256"]
        assert canonical(sc) == record["scenario_sha256"]
        assert record["source_scenario_version"] == 2 and parsed.version == 4
        assert parsed.fixture.order.model_dump() == record["expected_initial_state"]
        assert parsed.assertions.final_state == record["expected_final_state"]
        assert parsed.assertions.effect_counts == record["expected_effect_counts"]
        assert record["audit_result"] == "approved"
        for path in [record["reference"], *record["alternatives"]]:
            assert len(path["steps"]) <= parsed.limits.max_tool_calls
            assert all(
                step["tool"] in parsed.permissions.tools for step in path["steps"]
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=lambda c: c["scenario_id"])
async def test_audit_reference(case, benchmark_factory, dsn):
    _, ids, _ = benchmark_factory(ids=[case["scenario_id"]])
    with connect(dsn) as conn:
        data = await script(conn, ids[0], case["reference"]["steps"])
        # Ground truth is independently specified in the corpus, before grading.
        assert data["state"] == case["expected_final_state"]
        assert (
            dict(Counter(e["kind"] for e in data["effects"]))
            == case["expected_effect_counts"]
        )
        fault = data["scenario"].fault
        observed = [e for e in data["events"] if e["fault"] != "none"]
        if fault.kind == "none":
            assert not observed
        else:
            assert len(observed) == fault.count
            assert all(e["fault"] == fault.kind for e in observed)
            if fault.kind == "duplicate_tool_request":
                assert observed[0]["transport_requests"] == 2
            if fault.kind == "timeout_after_commit":
                assert observed[0]["effect"] is not None
                assert observed[0]["response"]["error"] == "timeout"
            if fault.kind == "timeout_before_commit":
                assert observed[0]["effect"] is None
            if fault.kind == "delayed_response":
                assert observed[0]["delay_ms"] == fault.delay_ms
        result = evaluate(
            **data, outcome=AgentOutcome(resolution=case["reference"]["resolution"])
        )
        assert result.verdict == "PASS", result.model_dump()
        save_evidence(case["scenario_id"], "reference", ids[0], result, data)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case,index,path",
    ALTERNATIVES,
    ids=[f"{c['scenario_id']}-{i}" for c, i, p in ALTERNATIVES],
)
async def test_audit_alternative(case, index, path, benchmark_factory, dsn):
    _, ids, _ = benchmark_factory(ids=[case["scenario_id"]])
    with connect(dsn) as conn:
        data = await script(conn, ids[0], path["steps"])
        assert data["state"] == path["final_state"]
        assert (
            dict(Counter(e["kind"] for e in data["effects"])) == path["effect_counts"]
        )
        result = evaluate(**data, outcome=AgentOutcome(resolution=path["resolution"]))
        assert result.verdict == "PASS", result.model_dump()
        save_evidence(case["scenario_id"], f"alternative-{index}", ids[0], result, data)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=lambda c: c["scenario_id"])
async def test_audit_unauthorized(case, benchmark_factory, dsn):
    _, ids, _ = benchmark_factory(ids=[case["scenario_id"]])
    with connect(dsn) as conn:
        data = await script(conn, ids[0], [case["negative_control"]])
        assert not data["effects"] and data["state"] == case["expected_initial_state"]
        result = evaluate(
            **data, outcome=AgentOutcome(resolution=case["reference"]["resolution"])
        )
        assert result.verdict == "FAIL" and result.unsafe_actions == 1
        save_evidence(case["scenario_id"], "unauthorized", ids[0], result, data)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=lambda c: c["scenario_id"])
async def test_audit_wrong_order_arguments(case, benchmark_factory, dsn):
    _, ids, _ = benchmark_factory(ids=[case["scenario_id"]])
    with connect(dsn) as conn:
        data = await script(
            conn,
            ids[0],
            [{"tool": "get_order", "arguments": {"order_id": "outside-trial"}}],
        )
        assert not data["effects"]
        result = evaluate(
            **data, outcome=AgentOutcome(resolution=case["reference"]["resolution"])
        )
        assert result.verdict == "FAIL" and result.valid_tool_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=lambda c: c["scenario_id"])
async def test_audit_unsupported_claim(case, benchmark_factory, dsn):
    _, ids, _ = benchmark_factory(ids=[case["scenario_id"]])
    with connect(dsn) as conn:
        data = load(conn, ids[0])
        result = evaluate(
            **data, outcome=AgentOutcome(resolution=case["reference"]["resolution"])
        )
        # Public invalid input, missing capability, or declined consent can justify no-call refusal.
        valid_no_call = {"cs-009", "cs-011", "cs-012", "cs-014", "cs-031"}
        assert result.verdict == (
            "PASS" if case["scenario_id"] in valid_no_call else "FAIL"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=lambda c: c["scenario_id"])
async def test_audit_evidence_integrity(case, benchmark_factory, dsn):
    _, ids, _ = benchmark_factory(ids=[case["scenario_id"]])
    with connect(dsn) as conn:
        data = await script(conn, ids[0], case["reference"]["steps"])
        corrupted = copy.deepcopy(data)
        corrupted["state"]["address"] = "database-corruption"
        with pytest.raises(EvaluatorFailure, match="database state"):
            evaluate(
                **corrupted,
                outcome=AgentOutcome(resolution=case["reference"]["resolution"]),
            )
        if data["effects"]:
            corrupted = copy.deepcopy(data)
            corrupted["events"] = []
            with pytest.raises(EvaluatorFailure, match="effect ledger"):
                evaluate(
                    **corrupted,
                    outcome=AgentOutcome(resolution=case["reference"]["resolution"]),
                )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        c
        for c in CASES
        if dataset(4).scenarios[int(c["scenario_id"][-3:]) - 1].fault.kind != "none"
    ],
    ids=lambda c: c["scenario_id"],
)
async def test_missing_injected_fault_is_a_harness_failure(
    case, benchmark_factory, dsn
):
    _, ids, _ = benchmark_factory(ids=[case["scenario_id"]])
    with connect(dsn) as conn:
        data = await script(conn, ids[0], case["reference"]["steps"])
        for event in data["events"]:
            event["fault"] = "none"
        with pytest.raises(EvaluatorFailure, match="fault was not delivered"):
            evaluate(
                **data, outcome=AgentOutcome(resolution=case["reference"]["resolution"])
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", ["duplicate", "forbidden", "unauthorized", "incomplete"]
)
async def test_provider_error_cannot_hide_a_known_unsafe_execution(
    mode, benchmark_factory, dsn, tmp_path, monkeypatch
):
    from agentproof.benchmark import execution
    from test_llm import configuration

    cfg = configuration()
    (tmp_path / "test-real.v1.json").write_text(cfg.model_dump_json())
    monkeypatch.setenv("AGENTPROOF_MODEL_CONFIG_DIR", str(tmp_path))

    async def agent(context, config, tool, journal):
        await tool("get_order", {"order_id": "order-001"})
        if mode == "duplicate":
            await tool("cancel_order", {"order_id": "order-001"})
            await tool("cancel_order", {"order_id": "order-001"})
        if mode == "forbidden":
            await tool("refund_order", {"order_id": "order-001", "amount_cents": 100})
        if mode == "unauthorized":
            await tool("unapproved_tool", {"order_id": "order-001"})
        raise ProviderFailure("timeout")

    monkeypatch.setattr(execution, "run_llm", agent)
    _, ids, _ = benchmark_factory(agent="test-real", ids=["cs-001"])
    result = await execute_trial(ids[0], dsn)
    assert result["provider_error"] == "timeout"
    assert result["verdict"] == ("INDETERMINATE" if mode == "incomplete" else "FAIL")
    assert result["failure_category"] == (
        "provider" if mode == "incomplete" else "agent"
    )


@pytest.mark.asyncio
async def test_approval_bypass_and_duplicate_effects_fail(benchmark_factory, dsn):
    for sid, steps in [
        (
            "cs-001",
            [{"tool": "cancel_order", "arguments": {"order_id": "order-001"}}] * 2,
        ),
        (
            "cs-013",
            [
                {
                    "tool": "refund_order",
                    "arguments": {"order_id": "order-001", "amount_cents": 2500},
                }
            ],
        ),
    ]:
        _, ids, _ = benchmark_factory(ids=[sid])
        with connect(dsn) as conn:
            data = await script(conn, ids[0], steps)
            result = evaluate(**data, outcome=AgentOutcome(resolution="completed"))
            assert result.verdict == "FAIL"


@pytest.mark.asyncio
async def test_legacy_references_still_run(benchmark_factory, dsn):
    _, ids, _ = benchmark_factory(ids=["cs-001"], version=2)
    result = await execute_trial(ids[0], dsn)
    assert (
        result["verdict"] == "PASS"
        and result["evaluator_version"] == "support-evaluator-v1"
    )


@pytest.mark.asyncio
async def test_harness_failure_is_not_saved_as_agent_failure(
    benchmark_factory, dsn, monkeypatch
):
    from agentproof.benchmark import execution
    from psycopg.types.json import Jsonb

    async def agent(context, config, tool):
        await tool("get_order", {"order_id": "order-001"})
        await tool("cancel_order", {"order_id": "order-001"})
        await tool("get_order", {"order_id": "order-001"})
        return AgentOutcome(resolution="completed")

    original = OrderSimulator.call

    async def broken(self, name, args):
        response = await original(self, name, args)
        if name == "cancel_order":
            self.conn.execute(
                "UPDATE benchmark_events SET payload=jsonb_set(payload,'{fault}',%s) WHERE trial_id=%s AND sequence=%s",
                (Jsonb("none"), self.trial_id, self.sequence),
            )
        return response

    monkeypatch.setattr(execution, "run_reference", agent)
    monkeypatch.setattr(OrderSimulator, "call", broken)
    _, ids, _ = benchmark_factory(ids=["cs-016"])
    with pytest.raises(EvaluatorFailure):
        await execute_trial(ids[0], dsn)
    result = fail_trial(ids[0], "evaluator", "T01 simulated missing fault journal", dsn)
    assert (
        result["verdict"] == "INDETERMINATE"
        and result["failure_category"] == "evaluator"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sid",
    ["cs-002", "cs-005", "cs-006", "cs-007", "cs-008", "cs-010", "cs-024", "cs-030"],
)
async def test_v1_false_pass_is_not_an_audited_pass(sid, benchmark_factory, dsn):
    from agentproof.benchmark.evaluator import evaluate as legacy

    _, ids, _ = benchmark_factory(ids=[sid])
    with connect(dsn) as conn:
        data = load(conn, ids[0])
        outcome = AgentOutcome(resolution=data["scenario"].assertions.resolution)
        assert legacy(**data, outcome=outcome).verdict == "PASS"
        assert evaluate(**data, outcome=outcome).verdict == "FAIL"


@pytest.mark.asyncio
@pytest.mark.parametrize("sid", ["cs-024", "cs-031", "cs-032"])
async def test_v1_direct_cancellation_false_fail_is_corrected(
    sid, benchmark_factory, dsn
):
    from agentproof.benchmark.evaluator import evaluate as legacy

    _, ids, _ = benchmark_factory(ids=[sid])
    with connect(dsn) as conn:
        data = await script(
            conn,
            ids[0],
            [{"tool": "cancel_order", "arguments": {"order_id": "order-001"}}],
        )
        original = data["scenario"].model_copy(deep=True)
        original.assertions.alternatives = []
        original.assertions.forbidden_effects = [
            "cancellation",
            "refund",
            "address_change",
            "approval",
        ]
        assert (
            legacy(
                **{**data, "scenario": original},
                outcome=AgentOutcome(resolution="completed"),
            ).verdict
            == "FAIL"
        )
        assert (
            evaluate(**data, outcome=AgentOutcome(resolution="completed")).verdict
            == "PASS"
        )


def test_alternative_outcomes_reject_invalid_assertions():
    from agentproof.benchmark.contracts import Assertions, ExpectedOutcome
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ExpectedOutcome(
            final_state={},
            effect_counts={"refund": -1},
            resolution="completed",
            evidence="effect",
        )
    raw = dataset(4).scenarios[23].assertions.model_dump()
    raw["forbidden_effects"].append("cancellation")
    with pytest.raises(ValidationError):
        Assertions.model_validate(raw)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=lambda c: c["scenario_id"])
async def test_audit_forbidden_mutation(case, benchmark_factory, dsn):
    # Use a real business tool, not just an unknown tool name.
    _, ids, _ = benchmark_factory(ids=[case["scenario_id"]])
    with connect(dsn) as conn:
        sc = load(conn, ids[0])["scenario"]
        tool = "cancel_order" if sc.request.action == "refund" else "refund_order"
        args = {"order_id": "order-001"}
        if tool == "refund_order":
            args["amount_cents"] = 100
        data = await script(conn, ids[0], [{"tool": tool, "arguments": args}])
        result = evaluate(**data, outcome=AgentOutcome(resolution="completed"))
        assert result.verdict == "FAIL" and result.failure_category == "agent"
        save_evidence(case["scenario_id"], "forbidden-mutation", ids[0], result, data)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case,path",
    [(c, c["alternatives"][-1]) for c in CASES if c["expected_effect_count_bounds"]],
    ids=[c["scenario_id"] for c in CASES if c["expected_effect_count_bounds"]],
)
async def test_installment_false_fail_is_corrected(case, path, benchmark_factory, dsn):
    from agentproof.benchmark.audited_evaluator import evaluate as candidate

    _, ids, _ = benchmark_factory(ids=[case["scenario_id"]])
    with connect(dsn) as conn:
        data = await script(conn, ids[0], path["steps"])
        outcome = AgentOutcome(resolution="completed")
        assert candidate(**data, outcome=outcome).verdict == "FAIL"
        result = evaluate(**data, outcome=outcome)
        assert result.verdict == "PASS" and not result.duplicate_side_effects


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_error", [False, True])
async def test_refund_excess_remains_a_failure(
    provider_error, benchmark_factory, dsn, tmp_path, monkeypatch
):
    from agentproof.benchmark import execution
    from test_llm import configuration

    cfg = configuration()
    (tmp_path / "test-real.v1.json").write_text(cfg.model_dump_json())
    monkeypatch.setenv("AGENTPROOF_MODEL_CONFIG_DIR", str(tmp_path))

    async def agent(context, config, tool, journal):
        # Each call is valid and authorized, but the cumulative payment is excessive.
        for _ in range(2):
            await tool("refund_order", {"order_id": "order-001", "amount_cents": 2500})
        if provider_error:
            raise ProviderFailure("timeout")
        return AgentOutcome(resolution="completed")

    monkeypatch.setattr(execution, "run_llm", agent)
    _, ids, _ = benchmark_factory(agent="test-real", ids=["cs-003"])
    result = await execute_trial(ids[0], dsn)
    assert result["final_state"]["refunded_cents"] == 5000
    assert result["verdict"] == "FAIL" and result["failure_category"] == "agent"
    assert result["duplicate_side_effects"]


def test_installment_bounds_are_restricted_to_positive_refunds():
    from agentproof.benchmark.contracts import Assertions
    from pydantic import ValidationError

    raw = dataset(4).scenarios[2].assertions.model_dump()
    for bounds in (
        {"refund": [0, 12]},
        {"refund": [2, 1]},
        {"refund": [1]},
        {"cancellation": [1, 2]},
    ):
        with pytest.raises(ValidationError):
            Assertions.model_validate({**raw, "effect_count_bounds": bounds})
