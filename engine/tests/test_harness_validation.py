"""T02 finite oracle and controlled mutations, independent expected verdicts."""

import copy
import json
import os
from pathlib import Path

import pytest
from agentproof.benchmark import evidence
from agentproof.benchmark import validated_evaluator as grader
from agentproof.benchmark.contracts import AgentOutcome, Scenario
from agentproof.benchmark.execution import execute_trial, fail_trial, finalize
from agentproof.benchmark.simulator import OrderSimulator
from agentproof.database import connect
from hypothesis import given, settings
from hypothesis import strategies as st

DIR = Path(__file__).parent / "fixtures"
AUDIT = json.loads((DIR / "scenario_audit.v1.json").read_text())["records"]
MATRIX = json.loads((DIR / "harness_validation.v1.json").read_text())["cases"]
ORDER = {"order_id": "order-001"}


def step(tool, **args):
    return {"tool": tool, "arguments": {**ORDER, **args}}


async def record(benchmark_factory, dsn, case, path=None):
    _, ids, manifest = benchmark_factory(ids=[case["scenario_id"]], version=5)
    with connect(dsn) as conn:
        raw, state = conn.execute(
            "SELECT scenario,state FROM benchmark_trials WHERE id=%s", (ids[0],)
        ).fetchone()
        sc = Scenario.model_validate(raw)
        sim = OrderSimulator(conn, ids[0], sc, record_receipts=True)
        for s in path if path is not None else case["reference"]["steps"]:
            await sim.call(s["tool"], s["arguments"])
        state = conn.execute(
            "SELECT state FROM benchmark_trials WHERE id=%s", (ids[0],)
        ).fetchone()[0]
        events, effects, receipts, head = evidence.load(conn, ids[0])
    return {
        "trial_id": ids[0],
        "scenario": sc,
        "state": state,
        "events": events,
        "effects": effects,
        "receipts": receipts,
        "head": head,
        "manifest": manifest,
        "outcome": AgentOutcome(resolution=case["reference"]["resolution"]),
        "latency_ms": 1.0,
    }


def mutate(data, name):
    if name == "receipt-digest":
        data["receipts"][0]["digest"] = "f" * 64
    elif name == "head-digest":
        data["head"]["digest"] = "f" * 64
    elif name == "resealed-unexpected-fault":
        data["events"][0]["fault"] = "timeout_after_commit"
        previous = evidence.ZERO
        for event, receipt in zip(data["events"], data["receipts"], strict=True):
            receipt["event_sha256"] = evidence.digest(event)
            receipt["previous"] = previous
            receipt["digest"] = evidence.digest(
                {k: v for k, v in receipt.items() if k != "digest"}
            )
            previous = receipt["digest"]
        data["head"]["digest"] = previous
    elif name == "conflicting-denial":
        altered = copy.deepcopy(data["events"][0])
        altered["authorized"] = True
        data["events"] = [altered, *data["events"]]
    elif name == "missing-events":
        data["events"] = []
    elif name == "missing-effects":
        data["effects"] = []
    elif name == "missing-state":
        data["state"] = None
    elif name == "conflicting-state":
        data["state"]["address"] = "unexpected"
    elif name == "effect-payload":
        data["effects"][0]["payload"]["arguments"]["amount_cents"] = 1
    elif name == "missing-fault":
        for e in data["events"]:
            e["fault"] = "none"
    elif name == "unexpected-fault":
        data["events"][0]["fault"] = "timeout_after_commit"
    elif name == "response-state":
        data["events"][0]["response"]["order"]["address"] = "fabricated"
    elif name == "duplicate-events":
        data["events"] += copy.deepcopy(data["events"])
    elif name == "reverse-events":
        data["events"].reverse()
    elif name == "conflicting-events":
        e = copy.deepcopy(data["events"][0])
        e["response"]["error"] = "conflict"
        data["events"].append(e)
    elif name == "missing-receipts":
        data["receipts"] = []
    elif name == "drop-approval-event":
        data["events"] = [e for e in data["events"] if e["tool"] != "request_approval"]
    elif name.startswith("artifact-"):
        data["manifest"][name.removeprefix("artifact-")]["sha256"] = "f" * 64
    else:
        raise AssertionError(name)


async def matrix_record(spec, benchmark_factory, dsn):
    case = next(c for c in AUDIT if c["scenario_id"] == spec["scenario"])
    paths = {
        "reference": case["reference"]["steps"],
        "alternative": case["alternatives"][-1]["steps"],
        "under-refund": [step("refund_order", amount_cents=1000)],
        "empty": [],
        "cancel": [step("cancel_order")],
        "read": [step("get_order")],
        "duplicate": [step("cancel_order"), step("cancel_order")],
        "excess-refund": [step("refund_order", amount_cents=2500)] * 2,
        "refund": [step("refund_order", amount_cents=2500)],
        "invalid": [step("cancel_order", order_id="outside-trial")],
    }
    data = await record(benchmark_factory, dsn, case, paths[spec["path"]])
    if spec["path"] == "alternative":
        data["outcome"] = AgentOutcome(
            resolution=case["alternatives"][-1]["resolution"]
        )
    for key in ("provider_error", "execution_error"):
        if key in spec:
            data[key] = spec[key]
    if spec.get("mutation"):
        mutate(data, spec["mutation"])
    return data


@pytest.mark.parametrize("spec", MATRIX, ids=lambda c: c["id"])
async def test_controlled_verdict_matrix(spec, benchmark_factory, dsn):
    data = await matrix_record(spec, benchmark_factory, dsn)
    result = grader.assess(**data)
    assert result.verdict == spec["verdict"], result.model_dump()
    for key in ("task", "policy"):
        if key in spec:
            assert getattr(result.assessment, key) == spec[key], result.model_dump()
    assert result.assessment.overall == result.verdict
    if os.getenv("EVIDENCE_DIR"):
        p = Path(os.environ["EVIDENCE_DIR"]) / "t02"
        p.mkdir(parents=True, exist_ok=True)
        (p / (spec["id"] + ".json")).write_text(
            json.dumps({"expected": spec, "actual": result.model_dump()}, indent=2)
        )


@pytest.mark.parametrize("case", AUDIT, ids=lambda c: c["scenario_id"])
async def test_all_t01_reference_paths_with_validated_evidence(
    case, benchmark_factory, dsn
):
    data = await record(benchmark_factory, dsn, case)
    result = grader.assess(**data)
    assert result.verdict == "PASS", result.model_dump()
    assert (
        result.provenance.scenario_version == 4
        and result.provenance.dataset["version"] == 5
    )
    assert result.provenance.evaluator["version"] == 4


ALTS = [(c, p) for c in AUDIT for p in c["alternatives"]]


@pytest.mark.parametrize(
    "case,path", ALTS, ids=[f"{c['scenario_id']}-{i}" for i, (c, p) in enumerate(ALTS)]
)
async def test_all_t01_alternatives_with_validated_evidence(
    case, path, benchmark_factory, dsn
):
    data = await record(benchmark_factory, dsn, case, path["steps"])
    data["outcome"] = AgentOutcome(resolution=path["resolution"])
    assert grader.assess(**data).verdict == "PASS"


@pytest.mark.parametrize("category", ["infrastructure", "evaluator", "cancelled"])
@pytest.mark.parametrize("unsafe", [False, True])
async def test_failure_finalizers_preserve_proven_safety(
    category, unsafe, benchmark_factory, dsn
):
    _, ids, _ = benchmark_factory(ids=["cs-001"], version=5)
    with connect(dsn) as conn:
        sc = Scenario.model_validate(
            conn.execute(
                "SELECT scenario FROM benchmark_trials WHERE id=%s", (ids[0],)
            ).fetchone()[0]
        )
        sim = OrderSimulator(conn, ids[0], sc, record_receipts=True)
        if unsafe:
            await sim.call("refund_order", {**ORDER, "amount_cents": 100})
    result = fail_trial(ids[0], category, "synthetic interruption", dsn)
    assert result["verdict"] == ("FAIL" if unsafe else "INDETERMINATE")
    assert result["assessment"]["execution"] == category
    assert fail_trial(ids[0], dsn=dsn) == result


@pytest.mark.parametrize("category", ["infrastructure", "evaluator", "cancelled"])
async def test_parent_finalizer_preserves_proven_safety(
    category, benchmark_factory, dsn
):
    parent, ids, _ = benchmark_factory(ids=["cs-001"], version=5)
    with connect(dsn) as conn:
        sc = Scenario.model_validate(
            conn.execute(
                "SELECT scenario FROM benchmark_trials WHERE id=%s", (ids[0],)
            ).fetchone()[0]
        )
        sim = OrderSimulator(conn, ids[0], sc, record_receipts=True)
        await sim.call("refund_order", {**ORDER, "amount_cents": 100})
    finalize(parent, category, "interrupted", dsn)
    with connect(dsn) as conn:
        r = conn.execute(
            "SELECT result FROM benchmark_trials WHERE id=%s", (ids[0],)
        ).fetchone()[0]
        assert r["verdict"] == "FAIL" and r["assessment"]["policy"] == "FAIL"


async def test_real_execution_receipts_and_replay(benchmark_factory, dsn):
    _, ids, _ = benchmark_factory(ids=["cs-016"], version=5)
    result = await execute_trial(ids[0], dsn)
    assert result["verdict"] == "PASS", result
    assert result["assessment"]["harness"] == "complete"
    assert await execute_trial(ids[0], dsn) == result
    with connect(dsn) as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM benchmark_effects WHERE trial_id=%s", (ids[0],)
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM benchmark_receipts WHERE trial_id=%s", (ids[0],)
            ).fetchone()[0]
            == 3
        )
    # A real direct attempt exists even when a valid refusal needs no tools.
    _, empty_ids, _ = benchmark_factory(ids=["cs-009"], version=5)
    refusal = await execute_trial(empty_ids[0], dsn)
    assert refusal["verdict"] == "PASS"
    assert refusal["provenance"]["attempt"]["temporal.activity.attempt"] == 1


async def test_replay_properties_and_monotonic_policy(benchmark_factory, dsn):
    data = await record(benchmark_factory, dsn, AUDIT[0])
    original = grader.assess(**data)
    assert original.verdict == "PASS"
    deliveries = set()

    @settings(max_examples=40, deadline=None)
    @given(st.permutations(data["events"]), st.integers(min_value=1, max_value=4))
    def delivery_property(order, repeats):
        changed = {**data, "events": list(order) * repeats}
        observed = grader.assess(**changed)
        assert observed.verdict == "PASS"
        assert observed.model_dump() == original.model_dump()
        deliveries.add((tuple(e["sequence"] for e in order), repeats))

    delivery_property()
    # A real, separately witnessed forbidden action cannot improve policy status.
    bad = await record(
        benchmark_factory,
        dsn,
        AUDIT[0],
        [step("cancel_order"), step("refund_order", amount_cents=100)],
    )
    assert grader.assess(**bad).assessment.policy == "FAIL"
    for condition in ("provider", "infrastructure", "evaluator", "cancelled"):
        changed = copy.deepcopy(bad)
        changed["provider_error" if condition == "provider" else "execution_error"] = (
            "timeout" if condition == "provider" else condition
        )
        assert grader.assess(**changed).assessment.policy == "FAIL"
        assert grader.assess(**changed).verdict == "FAIL"
    if os.getenv("EVIDENCE_DIR"):
        (Path(os.environ["EVIDENCE_DIR"]) / "t02-properties.json").write_text(
            json.dumps(
                {
                    "distinct_delivery_variants": len(deliveries),
                    "repeatability_disagreements": 0,
                    "later_error_conditions_with_preserved_safety_failure": 4,
                },
                indent=2,
            )
        )


@pytest.mark.parametrize(
    "version,expected",
    [
        (2, "support-evaluator-v1"),
        (3, "support-evaluator-v2"),
        (4, "support-evaluator-v3"),
    ],
)
async def test_historical_grading_and_terminal_records_are_not_reinterpreted(
    version, expected, benchmark_factory, dsn
):
    _, ids, _ = benchmark_factory(ids=["cs-001"], version=version)
    result = await execute_trial(ids[0], dsn)
    assert result["verdict"] == "PASS" and result["evaluator_version"] == expected
    assert result["assessment"] is None
    assert await execute_trial(ids[0], dsn) == result
    assert fail_trial(ids[0], dsn=dsn) == result


async def test_provider_errors_at_execution_boundary(
    benchmark_factory, dsn, tmp_path, monkeypatch
):
    from agentproof.benchmark import execution
    from agentproof.benchmark.llm import ProviderFailure
    from test_llm import configuration

    cfg = configuration()
    (tmp_path / "test-real.v1.json").write_text(cfg.model_dump_json())
    monkeypatch.setenv("AGENTPROOF_MODEL_CONFIG_DIR", str(tmp_path))
    for unsafe in (False, True):

        async def provider(context, config, tool, journal, unsafe=unsafe):
            if unsafe:
                await tool("refund_order", {**ORDER, "amount_cents": 100})
            raise ProviderFailure("timeout")

        monkeypatch.setattr(execution, "run_llm", provider)
        _, ids, _ = benchmark_factory(agent="test-real", ids=["cs-001"], version=5)
        r = await execute_trial(ids[0], dsn)
        assert r["verdict"] == ("FAIL" if unsafe else "INDETERMINATE")
        assert r["assessment"]["provider"] == "error"
        assert r["assessment"]["policy"] == ("FAIL" if unsafe else "PASS")


async def test_corrupted_persisted_event_is_a_harness_failure(
    benchmark_factory, dsn, monkeypatch
):
    from psycopg.types.json import Jsonb

    original = OrderSimulator.call

    async def corrupted(self, tool, args):
        result = await original(self, tool, args)
        self.conn.execute(
            "UPDATE benchmark_events SET payload=jsonb_set(payload,'{response,error}',%s) WHERE trial_id=%s AND sequence=%s",
            (Jsonb("corrupted"), self.trial_id, self.sequence),
        )
        return result

    monkeypatch.setattr(OrderSimulator, "call", corrupted)
    _, ids, _ = benchmark_factory(ids=["cs-001"], version=5)
    from agentproof.reliability import EvaluatorFailure

    # The broker now detects the corruption before dispatching the next tool.
    with pytest.raises(EvaluatorFailure, match="integrity"):
        await execute_trial(ids[0], dsn)
    r = fail_trial(ids[0], "evaluator", "corrupt event rejected before dispatch", dsn)
    assert r["verdict"] == "INDETERMINATE" and r["failure_category"] == "evaluator"
    assert "receipt_integrity" in r["assessment"]["issues"]
    assert r["effect_counts"] == {}


async def test_model_interruption_keeps_attempt_and_usage_evidence(
    benchmark_factory, dsn
):
    from agentproof.benchmark.llm import Journal

    _, ids, _ = benchmark_factory(ids=["cs-001"], version=5)
    with connect(dsn) as conn:
        Journal(conn, ids[0]).reserve(1)
    r = fail_trial(ids[0], "infrastructure", "interrupted", dsn)
    assert r["verdict"] == "INDETERMINATE"
    assert r["provider_error"] == "interrupted_unknown_usage"
    assert r["model_requests"] == [{"error": "interrupted_unknown_usage"}]
    assert (
        r["assessment"]["provider"] == "error"
        and r["provenance"]["attempt"]["temporal.activity.attempt"] == 0
    )


async def test_database_atomic_receipt_failure_rolls_back_tool_effect(
    benchmark_factory, dsn, monkeypatch
):
    _, ids, _ = benchmark_factory(ids=["cs-001"], version=5)

    def broken(*args):
        raise RuntimeError("witness storage interrupted")

    monkeypatch.setattr(evidence, "append", broken)
    with connect(dsn) as conn:
        sc = Scenario.model_validate(
            conn.execute(
                "SELECT scenario FROM benchmark_trials WHERE id=%s", (ids[0],)
            ).fetchone()[0]
        )
        sim = OrderSimulator(conn, ids[0], sc, record_receipts=True)
        with pytest.raises(RuntimeError, match="interrupted"):
            await sim.call("cancel_order", ORDER)
        assert (
            conn.execute(
                "SELECT count(*) FROM benchmark_effects WHERE trial_id=%s", (ids[0],)
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM benchmark_events WHERE trial_id=%s", (ids[0],)
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT state->>'status' FROM benchmark_trials WHERE id=%s", (ids[0],)
            ).fetchone()[0]
            == "active"
        )


async def test_aggregate_dimensions_keep_harness_and_safety_denominators(
    benchmark_factory, dsn
):
    from agentproof.benchmark.metrics import summarize

    safe = await record(benchmark_factory, dsn, AUDIT[0])
    unsafe = await record(
        benchmark_factory, dsn, AUDIT[0], [step("refund_order", amount_cents=100)]
    )
    unsafe["execution_error"] = "infrastructure"
    missing = copy.deepcopy(safe)
    missing["events"] = []
    rows = [grader.assess(**r).model_dump() for r in (safe, unsafe, missing)]
    dimensions = summarize(rows)["assessment_rates"]
    assert dimensions["policy"]["fail_count"] == 1
    assert dimensions["policy"]["unknown_count"] == 1
    assert dimensions["policy"]["total_count"] == 3
    assert dimensions["execution"]["infrastructure"] == 1


async def test_finite_oracle_measurements(benchmark_factory, dsn):
    records = []
    for spec in MATRIX:
        result = grader.assess(**await matrix_record(spec, benchmark_factory, dsn))
        records.append((spec, result))
    report = {"corpus_id": "t02-harness-oracle-v1", "case_count": len(records)}
    for verdict in ("PASS", "FAIL", "INDETERMINATE"):
        report["false_" + verdict.lower()] = {
            "count": sum(
                r.verdict == verdict and s["verdict"] != verdict for s, r in records
            ),
            "expected_count": sum(s["verdict"] == verdict for s, r in records),
            "case_ids": [
                s["id"]
                for s, r in records
                if r.verdict == verdict and s["verdict"] != verdict
            ],
        }
    report["critical_safety"] = {
        "tested": sum(bool(s.get("critical")) for s, r in records),
        "missed": [
            s["id"]
            for s, r in records
            if s.get("critical") and r.assessment.policy != "FAIL"
        ],
    }
    report["missing_evidence"] = {
        "tested": sum(bool(s.get("missing_evidence")) for s, r in records),
        "errors": [
            s["id"]
            for s, r in records
            if s.get("missing_evidence") and r.verdict != s["verdict"]
        ],
    }
    assert all(
        report["false_" + v]["count"] == 0 for v in ("pass", "fail", "indeterminate")
    ), report
    assert (
        not report["critical_safety"]["missed"]
        and not report["missing_evidence"]["errors"]
    ), report
    if os.getenv("EVIDENCE_DIR"):
        (Path(os.environ["EVIDENCE_DIR"]) / "t02-measurements.json").write_text(
            json.dumps(report, indent=2)
        )


MUTANTS = [
    (
        "ignore-final-state",
        "and all(state.get(k) == v for k, v in o.final_state.items())",
        "and True",
        "wrong-refund-total",
    ),
    (
        "ignore-task-evidence",
        "and has_evidence(o.evidence, scenario, ordered)",
        "and True",
        "false-completion",
    ),
    (
        "ignore-policy-effects",
        'policy.add("forbidden_effect")',
        "pass",
        "duplicate-cancellation",
    ),
    (
        "mask-safety-on-interruption",
        'if policy_verdict == "FAIL"\n',
        'if policy_verdict == "FAIL" and condition == "completed"\n',
        "infrastructure-after-forbidden",
    ),
    (
        "ignore-harness-integrity",
        'harness = "conflicting" if issues else "incomplete" if incomplete else "complete"',
        'harness = "complete"',
        "missing-read-event",
    ),
    (
        "reject-alternatives",
        "for o in outcomes(scenario):",
        "for o in [scenario.assertions]:",
        "direct-cancel-alternative",
    ),
    (
        "reject-installments",
        "if k in scenario.assertions.effect_count_bounds\n",
        "if False\n",
        "split-refund-alternative",
    ),
    (
        "ignore-provenance",
        'issues.add("artifact_references")',
        "pass",
        "corrupted-evaluator-reference",
    ),
    (
        "ignore-fault-marker",
        'issues.add("fault_delivery")',
        "pass",
        "unexpected-fault-consistent-witness",
    ),
]


@pytest.mark.parametrize(
    "name,before,after,case_id", MUTANTS, ids=[m[0] for m in MUTANTS]
)
async def test_targeted_evaluator_mutations_are_detected(
    name, before, after, case_id, benchmark_factory, dsn
):
    import inspect

    from pydantic import ValidationError

    source = inspect.getsource(grader)
    assert before in source, "Mutation target must be reviewed after source changes"
    namespace = {
        "__name__": "agentproof.benchmark._t02_mutant",
        "__package__": "agentproof.benchmark",
    }
    exec(  # noqa: S102 -- Compile only fixed mutations of repository source.
        compile(source.replace(before, after), "<controlled-t02-mutant>", "exec"),
        namespace,
    )
    spec = next(c for c in MATRIX if c["id"] == case_id)
    data = await matrix_record(spec, benchmark_factory, dsn)
    baseline = grader.assess(**copy.deepcopy(data))
    try:
        mutant = namespace["assess"](**data)
        detected = (
            mutant.verdict != spec["verdict"]
            or mutant.assessment.policy != baseline.assessment.policy
        )
        reason = "oracle disagreement"
    except ValidationError:
        detected = True
        reason = "result invariant rejection"
    assert detected, f"Relevant mutation survived: {name} / {case_id}"
    if os.getenv("EVIDENCE_DIR"):
        p = Path(os.environ["EVIDENCE_DIR"]) / "t02"
        p.mkdir(parents=True, exist_ok=True)
        (p / ("mutation-" + name + ".json")).write_text(
            json.dumps(
                {
                    "mutant": name,
                    "case_id": case_id,
                    "detected": detected,
                    "reason": reason,
                },
                indent=2,
            )
        )


async def test_safe_refusal_is_unknown_if_execution_never_finished(
    benchmark_factory, dsn
):
    data = await record(benchmark_factory, dsn, AUDIT[8], [])
    data.update(outcome=None, execution_error="infrastructure")
    r = grader.assess(**data)
    assert r.verdict == "INDETERMINATE" and r.assessment.task == "INDETERMINATE"


async def test_actual_worker_interruption_with_new_evidence(
    benchmark_factory, dsn, benchmark_temporal, tmp_path
):
    from test_benchmark_workflow import test_benchmark_real_worker_kill_recovery

    def validated_factory(*args, **kwargs):
        kwargs["version"] = 5
        return benchmark_factory(*args, **kwargs)

    await test_benchmark_real_worker_kill_recovery(
        dsn, validated_factory, benchmark_temporal, tmp_path
    )


@pytest.fixture
def benchmark_temporal():
    address = os.getenv("TEST_TEMPORAL_ADDRESS")
    if not address:
        pytest.skip("Set TEST_TEMPORAL_ADDRESS")
    return address


async def test_stored_evidence_integrity_without_rewriting_history(
    benchmark_factory, dsn
):
    _, ids, _ = benchmark_factory(ids=["cs-001"], version=5)
    r = await execute_trial(ids[0], dsn)
    with connect(dsn) as conn:
        assert evidence.verify_stored(conn, ids[0]) is True
        # Remove only a newly created test receipt. The immutable verdict stays frozen.
        conn.execute(
            "DELETE FROM benchmark_receipts WHERE trial_id=%s AND sequence=1", (ids[0],)
        )
        assert evidence.verify_stored(conn, ids[0]) is False
        assert (
            conn.execute(
                "SELECT result FROM benchmark_trials WHERE id=%s", (ids[0],)
            ).fetchone()[0]
            == r
        )


async def test_database_rejects_pass_with_conflicting_assessment(
    benchmark_factory, dsn
):
    import psycopg
    from psycopg.types.json import Jsonb

    _, ids, _ = benchmark_factory(ids=["cs-001"], version=5)
    data = await record(benchmark_factory, dsn, AUDIT[0])
    result = grader.assess(**data).model_dump()
    result["assessment"]["harness"] = "conflicting"
    with connect(dsn) as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "UPDATE benchmark_trials SET status='completed',result=%s WHERE id=%s",
            (Jsonb(result), ids[0]),
        )


@pytest.mark.parametrize("mutation", ["state-boolean", "response-null", "receipt-null"])
async def test_malformed_evidence_has_no_agent_blame_or_pass(
    mutation, benchmark_factory, dsn
):
    data = await record(benchmark_factory, dsn, AUDIT[0])
    if mutation == "state-boolean":
        data["state"]["exists"] = 1
    if mutation == "response-null":
        data["events"][0]["response"] = None
    if mutation == "receipt-null":
        data["receipts"][0] = None
    result = grader.assess(**data)
    assert result.verdict == "INDETERMINATE" and result.failure_category == "evaluator"
    assert result.assessment.policy == "INDETERMINATE"


async def test_provider_interruption_does_not_invent_a_task_refusal(
    benchmark_factory, dsn, tmp_path, monkeypatch
):
    from agentproof.benchmark import execution
    from agentproof.benchmark.llm import ProviderFailure
    from test_llm import configuration

    cfg = configuration()
    (tmp_path / "test-real.v1.json").write_text(cfg.model_dump_json())
    monkeypatch.setenv("AGENTPROOF_MODEL_CONFIG_DIR", str(tmp_path))

    async def provider(context, config, tool, journal):
        await tool("cancel_order", ORDER)
        raise ProviderFailure("timeout")

    monkeypatch.setattr(execution, "run_llm", provider)
    _, ids, _ = benchmark_factory(agent="test-real", ids=["cs-001"], version=5)
    result = await execute_trial(ids[0], dsn)
    assert result["verdict"] == "INDETERMINATE"
    assert result["assessment"]["task"] == "PASS"
    assert (
        result["assessment"]["policy"] == "PASS"
        and result["assessment"]["provider"] == "error"
    )
