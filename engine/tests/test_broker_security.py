"""T03: trusted harness / untrusted tool-input boundary; no arbitrary Python sandbox."""

import json
import os
from pathlib import Path

import pytest
from agentproof.benchmark import evidence
from agentproof.benchmark.contracts import AgentOutcome, Scenario
from agentproof.benchmark.execution import execute_trial, finalize
from agentproof.benchmark.simulator import OrderSimulator, TrialStopped
from agentproof.database import connect
from agentproof.reliability import EvaluatorFailure
from psycopg.types.json import Jsonb

ORDER = {"order_id": "order-001"}


def simulator(conn, trial, **kwargs):
    sc = Scenario.model_validate(
        conn.execute(
            "SELECT scenario FROM benchmark_trials WHERE id=%s", (trial,)
        ).fetchone()[0]
    )
    return OrderSimulator(conn, trial, sc, **kwargs)


def snapshot(conn, trial):
    state = conn.execute(
        "SELECT state FROM benchmark_trials WHERE id=%s", (trial,)
    ).fetchone()[0]
    events, effects, receipts, head = evidence.load(conn, trial)
    return {
        "state": state,
        "events": events,
        "effects": effects,
        "receipts": receipts,
        "head": head,
    }


def assert_no_effect(before, after):
    assert after["state"] == before["state"]
    assert after["effects"] == before["effects"]


def save(case, before, after):
    def counts(value):
        if isinstance(value, list):
            return [counts(v) for v in value]
        return {k: len(value[k]) for k in ("events", "effects", "receipts")}

    if os.getenv("EVIDENCE_DIR"):
        p = Path(os.environ["EVIDENCE_DIR"]) / "t03"
        p.mkdir(parents=True, exist_ok=True)
        (p / (case + ".json")).write_text(
            json.dumps(
                {
                    "case": case,
                    "producer": "trusted trial broker; agent supplies only tool name and arguments",
                    "before": before,
                    "after": after,
                    "before_counts": counts(before),
                    "after_counts": counts(after),
                },
                indent=2,
            )
        )


@pytest.mark.parametrize("field", ["request", "permissions", "fixture", "fault"])
async def test_broker_refuses_foreign_or_modified_authority(
    field, benchmark_factory, dsn
):
    _, ids, _ = benchmark_factory(ids=["cs-014"], version=5)
    with connect(dsn) as c:
        sc = Scenario.model_validate(
            c.execute(
                "SELECT scenario FROM benchmark_trials WHERE id=%s", (ids[0],)
            ).fetchone()[0]
        )
        before = snapshot(c, ids[0])
        if field == "request":
            sc.request.user_approved = True
        if field == "permissions":
            sc.permissions.approval_required = False
        if field == "fixture":
            sc.fixture.order.customer_id = "customer-002"
        if field == "fault":
            sc.fault.kind = "timeout_after_commit"
        with pytest.raises(EvaluatorFailure, match="authority"):
            OrderSimulator(c, ids[0], sc)
        assert snapshot(c, ids[0]) == before
        save("modified-authority-" + field, before, snapshot(c, ids[0]))


async def test_private_authority_and_response_aliases_are_copied(
    benchmark_factory, dsn
):
    _, ids, _ = benchmark_factory(ids=["cs-014"], version=5)
    with connect(dsn) as c:
        sc = Scenario.model_validate(
            c.execute(
                "SELECT scenario FROM benchmark_trials WHERE id=%s", (ids[0],)
            ).fetchone()[0]
        )
        sim = OrderSimulator(c, ids[0], sc)
        sc.request.user_approved = True
        sc.permissions.approval_required = False
        sc.permissions.tools.append("cancel_order")
        before = snapshot(c, ids[0])
        response = await sim.call("get_order", ORDER)
        response["order"]["approved"] = True
        response["order"]["customer_id"] = "customer-002"
        response["order"]["total_cents"] = 999999
        args = {**ORDER, "amount_cents": 100}
        await sim.call("refund_order", args)
        args["amount_cents"] = 999999
        after = snapshot(c, ids[0])
        assert_no_effect(before, after)
        assert after["events"][-1]["authorized"] is False
        assert after["events"][0]["response"]["order"]["approved"] is False
        save("authority-and-response-copy", before, after)


@pytest.mark.parametrize(
    "tool",
    ["get_order", "cancel_order", "refund_order", "update_address", "request_approval"],
)
async def test_cross_trial_identity_and_forged_authority_fields_fail(
    tool, benchmark_factory, dsn
):
    _, ids, _ = benchmark_factory(ids=["cs-001"], trials=2, version=5)
    with connect(dsn) as c:
        before = [snapshot(c, i) for i in ids]
        args = {
            **ORDER,
            "trial_id": ids[1],
            "authorized": True,
            "effect": "cancellation",
            "approved": True,
            "operation_key": ids[1] + ":support-tools-v1:1",
        }
        response = await simulator(c, ids[0]).call(tool, args)
        after = [snapshot(c, i) for i in ids]
        assert response["ok"] is False and response["order"] is None
        assert_no_effect(before[0], after[0])
        assert after[1] == before[1]
        assert after[0]["events"][-1]["arguments_valid"] is False
        save("cross-trial-" + tool, before, after)


async def test_explicit_synthetic_read_grant_and_customer_write_boundary(
    benchmark_factory, dsn
):
    _, ids, _ = benchmark_factory(ids=["cs-008"], version=5)
    with connect(dsn) as c:
        sim = simulator(c, ids[0])
        before = snapshot(c, ids[0])
        read = await sim.call("get_order", ORDER)
        assert read["ok"] and read["order"]["customer_id"] == "customer-001"
        for tool, args in [
            ("cancel_order", ORDER),
            ("refund_order", {**ORDER, "amount_cents": 1}),
            ("update_address", {**ORDER, "address": "Test"}),
            ("request_approval", ORDER),
        ]:
            prior = snapshot(c, ids[0])
            response = await sim.call(tool, args)
            assert (
                response["error"] == "permission_denied" and response["order"] is None
            )
            assert_no_effect(prior, snapshot(c, ids[0]))
            save("cross-customer-" + tool, prior, snapshot(c, ids[0]))
        after = snapshot(c, ids[0])
        assert_no_effect(before, after)
        assert all(e["authorized"] is False for e in after["events"][1:])
        save("synthetic-read-and-cross-customer-writes", before, after)


async def test_missing_read_permission_never_discloses_fixture(benchmark_factory, dsn):
    _, ids, _ = benchmark_factory(ids=["cs-031"], version=5)
    with connect(dsn) as c:
        before = snapshot(c, ids[0])
        result = await simulator(c, ids[0]).call("get_order", ORDER)
        after = snapshot(c, ids[0])
        assert_no_effect(before, after)
        assert result["order"] is None and result["error"] == "permission_denied"
        save("missing-read-permission", before, after)


@pytest.mark.parametrize(
    "mode",
    [
        "wrong-order",
        "unknown",
        "wrong-tool-type",
        "list",
        "nan",
        "object",
        "huge-string",
        "huge-tool",
        "deep",
        "many-keys",
        "secret-extra",
        "approval-claim",
    ],
)
async def test_malformed_attempts_have_bounded_safe_evidence(
    mode, benchmark_factory, dsn
):
    _, ids, _ = benchmark_factory(ids=["cs-001"], version=5)
    tool = "cancel_order"
    args = dict(ORDER)
    if mode == "wrong-order":
        args = {"order_id": "other-order"}
    if mode == "unknown":
        tool = "unapproved_tool"
    if mode == "wrong-tool-type":
        tool = []
    if mode == "list":
        args = []
    if mode == "nan":
        args = {"order_id": float("nan")}
    if mode == "object":
        args = {"order_id": object()}
    if mode == "huge-string":
        args = {"order_id": "x" * 1000000}
    if mode == "huge-tool":
        tool = "x" * 1000000
    if mode == "deep":
        args = []
        for _ in range(20):
            args = [args]
    if mode == "many-keys":
        args = {str(i): i for i in range(1000)}
    if mode == "secret-extra":
        args = {**ORDER, "api_key": "FAKE-SENSITIVE-MARKER"}
    if mode == "approval-claim":
        args = {**ORDER, "user_approved": True}
    with connect(dsn) as c:
        before = snapshot(c, ids[0])
        response = await simulator(c, ids[0]).call(tool, args)
        after = snapshot(c, ids[0])
        assert_no_effect(before, after)
        assert response["error"] == "invalid_arguments"
        assert len(after["events"]) == len(after["receipts"]) == 1
        assert after["events"][0]["arguments_valid"] is False
        assert len(json.dumps(after["events"][0]).encode()) < 2048
        assert "FAKE-SENSITIVE-MARKER" not in json.dumps(after)
        save("invalid-" + mode, before, after)


@pytest.mark.parametrize("approved", [False, True])
async def test_approval_comes_from_immutable_harness_request(
    approved, benchmark_factory, dsn
):
    _, ids, _ = benchmark_factory(ids=["cs-013" if approved else "cs-014"], version=5)
    with connect(dsn) as c:
        sim = simulator(c, ids[0])
        before = snapshot(c, ids[0])
        denied = await sim.call("refund_order", {**ORDER, "amount_cents": 100})
        assert denied["error"] == "permission_denied"
        assert_no_effect(before, snapshot(c, ids[0]))
        save("approval-bypass-" + str(approved), before, snapshot(c, ids[0]))
        grant = await sim.call("request_approval", ORDER)
        await sim.call("refund_order", {**ORDER, "amount_cents": 100})
        after = snapshot(c, ids[0])
        assert bool(grant["approved"]) is approved
        assert after["state"]["refunded_cents"] == (100 if approved else 0)
        assert len(after["effects"]) == (2 if approved else 0)
        save("approval-" + str(approved), before, after)


async def test_replay_identity_and_distinct_refund_installments(benchmark_factory, dsn):
    _, ids, _ = benchmark_factory(ids=["cs-003"], version=5)
    with connect(dsn) as c:
        first = simulator(c, ids[0])
        args = {**ORDER, "amount_cents": 1250}
        await first.call("refund_order", args)
        committed = snapshot(c, ids[0])
        retry = simulator(c, ids[0])
        await retry.call("refund_order", args)
        assert snapshot(c, ids[0]) == committed
        save("identical-replay", committed, snapshot(c, ids[0]))
        mismatch = simulator(c, ids[0])
        with pytest.raises(RuntimeError, match="replay mismatch"):
            await mismatch.call("refund_order", {**ORDER, "amount_cents": 2500})
        assert snapshot(c, ids[0]) == committed
        save("mismatched-replay", committed, snapshot(c, ids[0]))
        await retry.call("refund_order", args)
        after = snapshot(c, ids[0])
        assert after["state"]["refunded_cents"] == 2500
        assert (
            len(after["effects"]) == 2
            and len({e["operation_key"] for e in after["effects"]}) == 2
        )
        save("replay-and-distinct-installments", committed, after)


@pytest.mark.parametrize(
    "kind",
    [
        "event",
        "effect",
        "receipt",
        "state",
        "missing-event",
        "missing-receipt",
        "null-receipt",
    ],
)
async def test_corruption_fences_replay_and_further_effects(
    kind, benchmark_factory, dsn
):
    _, ids, _ = benchmark_factory(ids=["cs-003"], version=5)
    with connect(dsn) as c:
        sim = simulator(c, ids[0])
        await sim.call("refund_order", {**ORDER, "amount_cents": 1250})
        if kind == "event":
            c.execute(
                "UPDATE benchmark_events SET payload=jsonb_set(payload,'{response,ok}','false') WHERE trial_id=%s",
                (ids[0],),
            )
        if kind == "effect":
            c.execute(
                "UPDATE benchmark_effects SET payload=jsonb_set(payload,'{arguments,amount_cents}','500') WHERE trial_id=%s",
                (ids[0],),
            )
        if kind == "receipt":
            c.execute(
                "UPDATE benchmark_receipts SET payload=jsonb_set(payload,'{digest}',%s) WHERE trial_id=%s",
                (Jsonb("f" * 64), ids[0]),
            )
        if kind == "state":
            c.execute(
                "UPDATE benchmark_trials SET state=jsonb_set(state,'{approved}','true') WHERE id=%s",
                (ids[0],),
            )
        if kind == "missing-event":
            c.execute("DELETE FROM benchmark_effects WHERE trial_id=%s", (ids[0],))
            c.execute("DELETE FROM benchmark_events WHERE trial_id=%s", (ids[0],))
        if kind == "missing-receipt":
            c.execute("DELETE FROM benchmark_receipts WHERE trial_id=%s", (ids[0],))
        if kind == "null-receipt":
            c.execute(
                "UPDATE benchmark_receipts SET payload='null'::jsonb WHERE trial_id=%s",
                (ids[0],),
            )
        before = snapshot(c, ids[0])
        for candidate in (sim, simulator(c, ids[0])):
            with pytest.raises(EvaluatorFailure):
                await candidate.call("refund_order", {**ORDER, "amount_cents": 1250})
        assert snapshot(c, ids[0]) == before
        save("corruption-" + kind, before, snapshot(c, ids[0]))


@pytest.mark.parametrize("terminal", ["completed", "cancelled"])
async def test_terminal_fence_preserves_state_and_evidence(
    terminal, benchmark_factory, dsn
):
    parent, ids, _ = benchmark_factory(ids=["cs-001"], version=5)
    with connect(dsn) as c:
        sim = simulator(c, ids[0])
        if terminal == "completed":
            await execute_trial(ids[0], dsn)
        else:
            finalize(parent, "cancelled", "test cancellation", dsn)
        before = snapshot(c, ids[0])
        with pytest.raises(TrialStopped):
            await sim.call("cancel_order", ORDER)
        assert snapshot(c, ids[0]) == before
        save("terminal-" + terminal, before, snapshot(c, ids[0]))


async def test_witness_cannot_be_disabled_by_caller(benchmark_factory, dsn):
    _, ids, _ = benchmark_factory(ids=["cs-001"], version=5)
    with connect(dsn) as c:
        await simulator(c, ids[0], record_receipts=False).call("get_order", ORDER)
        after = snapshot(c, ids[0])
        assert len(after["receipts"]) == 1 and after["head"]["event_count"] == 1


@pytest.mark.parametrize("journal", ["events", "receipts"])
async def test_database_identity_must_match_witness_payload(
    journal, benchmark_factory, dsn
):
    _, ids, _ = benchmark_factory(ids=["cs-001"], version=5)
    with connect(dsn) as c:
        await simulator(c, ids[0]).call("get_order", ORDER)
        if journal == "events":
            c.execute(
                "UPDATE benchmark_events SET sequence=2,operation_key=trial_id::text || ':support-tools-v1:2' WHERE trial_id=%s",
                (ids[0],),
            )
        else:
            c.execute(
                "UPDATE benchmark_receipts SET sequence=2 WHERE trial_id=%s",
                (ids[0],),
            )
        before = snapshot(c, ids[0])
        with pytest.raises(EvaluatorFailure, match="identity"):
            await simulator(c, ids[0]).call("get_order", ORDER)
        assert snapshot(c, ids[0]) == before
        save("database-identity-" + journal, before, snapshot(c, ids[0]))


async def test_context_attack_does_not_grant_authority_or_private_evidence(
    benchmark_factory, dsn, monkeypatch
):
    from agentproof.benchmark import execution

    _, ids, _ = benchmark_factory(ids=["cs-014"], version=5)
    with connect(dsn) as c:
        before = snapshot(c, ids[0])

    async def attacker(public, config, tool):
        assert set(public.model_dump()) == {
            "schema_version",
            "tool_schema",
            "request",
            "permissions",
            "limits",
        }
        public.request.user_approved = True
        public.permissions.approval_required = False
        public.permissions.tools.append("unapproved_tool")
        public.limits.max_tool_calls = 32
        public.tool_schema["request_approval"] = {"forged": True}
        await tool("request_approval", ORDER)
        await tool("refund_order", {**ORDER, "amount_cents": 100})
        return AgentOutcome(resolution="completed")

    monkeypatch.setattr(execution, "run_reference", attacker)
    r = await execute_trial(ids[0], dsn)
    assert r["effect_counts"] == {} and r["final_state"]["approved"] is False
    assert r["verdict"] == "FAIL" and r["assessment"]["policy"] == "FAIL"
    with connect(dsn) as c:
        save("public-context-attack", before, snapshot(c, ids[0]))


async def test_rejected_replay_and_terminal_attempts_keep_separate_bounded_audit(
    benchmark_factory, dsn
):
    _, ids, _ = benchmark_factory(ids=["cs-001"], version=5)
    with connect(dsn) as c:
        await simulator(c, ids[0]).call("get_order", ORDER)
        with pytest.raises(RuntimeError, match="replay mismatch"):
            await simulator(c, ids[0]).call("cancel_order", ORDER)
        result = await execute_trial(ids[0], dsn)
        before = snapshot(c, ids[0])
        with pytest.raises(TrialStopped):
            await simulator(c, ids[0]).call("cancel_order", ORDER)
        assert (
            snapshot(c, ids[0]) == before and evidence.verify_stored(c, ids[0]) is True
        )
        rows = c.execute(
            "SELECT reason,tool,arguments_sha256 FROM benchmark_broker_rejections WHERE trial_id=%s ORDER BY id",
            (ids[0],),
        ).fetchall()
        assert [r[0] for r in rows] == ["replay_mismatch", "terminal"]
        assert all(len(r[1]) <= 64 and len(r[2]) == 64 for r in rows)
        assert (
            c.execute(
                "SELECT result FROM benchmark_trials WHERE id=%s", (ids[0],)
            ).fetchone()[0]
            == result
        )


async def test_tampered_approval_cannot_enable_the_next_mutation(
    benchmark_factory, dsn
):
    _, ids, _ = benchmark_factory(ids=["cs-014"], version=5)
    with connect(dsn) as c:
        sim = simulator(c, ids[0])
        await sim.call("get_order", ORDER)
        c.execute(
            "UPDATE benchmark_trials SET state=jsonb_set(state,'{approved}','true') WHERE id=%s",
            (ids[0],),
        )
        before = snapshot(c, ids[0])
        with pytest.raises(EvaluatorFailure):
            await sim.call("refund_order", {**ORDER, "amount_cents": 100})
        assert_no_effect(before, snapshot(c, ids[0]))
        save("tampered-approval", before, snapshot(c, ids[0]))
        assert c.execute(
            "SELECT reason FROM benchmark_broker_rejections WHERE trial_id=%s",
            (ids[0],),
        ).fetchone() == ("evidence_integrity",)


async def test_transaction_failure_does_not_leave_partial_effect_or_event(
    benchmark_factory, dsn, monkeypatch
):
    def fail(*args):
        raise RuntimeError("test witness commit failure")

    _, ids, _ = benchmark_factory(ids=["cs-001"], version=5)
    with connect(dsn) as c:
        before = snapshot(c, ids[0])
        sim = simulator(c, ids[0])
        with monkeypatch.context() as m:
            m.setattr(evidence, "append", fail)
            with pytest.raises(RuntimeError, match="commit failure"):
                await sim.call("cancel_order", ORDER)
        assert snapshot(c, ids[0]) == before
        save("transaction-rollback", before, snapshot(c, ids[0]))
        await simulator(c, ids[0]).call("cancel_order", ORDER)
        after = snapshot(c, ids[0])
        assert (
            len(after["effects"]) == len(after["events"]) == len(after["receipts"]) == 1
        )
        save("transaction-recovery", before, after)


@pytest.mark.parametrize("target", ["events", "effects", "receipts", "evidence_heads"])
async def test_database_terminal_fences_cover_all_execution_journals(
    target, benchmark_factory, dsn
):
    import psycopg

    _, ids, _ = benchmark_factory(ids=["cs-001"], version=5)
    await execute_trial(ids[0], dsn)
    with connect(dsn) as c:
        before = snapshot(c, ids[0])
        column = "digest=digest" if target == "evidence_heads" else "payload=payload"
        with pytest.raises(psycopg.errors.RaiseException, match="terminal"):
            c.execute(
                f"UPDATE benchmark_{target} SET {column} WHERE trial_id=%s", (ids[0],)
            )
        assert snapshot(c, ids[0]) == before
        save("database-terminal-" + target, before, snapshot(c, ids[0]))
