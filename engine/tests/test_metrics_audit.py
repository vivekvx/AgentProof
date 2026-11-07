"""T06 formula-to-fixture checks for planned slots and observed evidence."""

import pytest
from agentproof.benchmark.execution import execute_trial, finalize
from agentproof.benchmark.metrics import rate, summarize
from agentproof.database import connect
from agentproof.provenance import verify_binding


def result(scenario_id, verdict, failure=None, *, policy="PASS", calls=None, **extra):
    return {
        "scenario_id": scenario_id,
        "verdict": verdict,
        "failure_category": failure,
        "assessment": {
            "policy": policy,
            "task": "PASS" if verdict == "PASS" else "INDETERMINATE",
        },
        "unsafe_actions": 0,
        "valid_tool_calls": 1,
        "tool_calls": 1,
        "recovery_eligible": False,
        "recovery_succeeded": None,
        "duplicate_side_effects": False,
        "latency_ms": 10,
        "effect_counts": {},
        "assertions": [],
        "model_requests": calls or [],
        **extra,
    }


def scenario(scenario_id, resolution="completed"):
    return {
        "id": scenario_id,
        "assertions": {
            "resolution": resolution,
            "alternatives": [],
            "forbidden_effects": ["refund"],
        },
    }


def test_mixed_cohort_keeps_every_planned_slot_and_partial_usage():
    rows = [
        result(
            "a",
            "PASS",
            calls=[
                {
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "estimated_cost_usd": 0.02,
                    "latency_ms": 100,
                }
            ],
        ),
        result(
            "a",
            "INDETERMINATE",
            "provider",
            policy="INDETERMINATE",
            calls=[
                {
                    "input_tokens": 3,
                    "output_tokens": 2,
                    "estimated_cost_usd": 0.01,
                    "latency_ms": 80,
                },
                {"error": "timeout", "latency_ms": 70},
            ],
        ),
        result(
            "b",
            "FAIL",
            "agent",
            policy="FAIL",
            valid_tool_calls=0,
            unsafe_actions=1,
            calls=[
                {
                    "error": "tool_generation_rejected",
                    "input_tokens": 20,
                    "latency_ms": 50,
                }
            ],
        ),
        result(
            "b",
            "INDETERMINATE",
            "infrastructure",
            policy="INDETERMINATE",
            calls=[{"error": "timeout", "latency_ms": 40}],
        ),
        result(
            "c",
            "INDETERMINATE",
            "cancelled",
            policy="INDETERMINATE",
            tool_calls=0,
            valid_tool_calls=0,
        ),
    ]
    slots = [
        {
            "scenario": scenario("a"),
            "result": rows[0],
            "outcome": {"resolution": "completed"},
            "started": True,
            "events": [{"authorized": True, "arguments_valid": True}],
        },
        {
            "scenario": scenario("a"),
            "result": rows[1],
            "outcome": {"resolution": "blocked"},
            "started": True,
        },
        {
            "scenario": scenario("b"),
            "result": rows[2],
            "outcome": {"resolution": "blocked"},
            "started": True,
            "events": [{"authorized": False, "arguments_valid": False}],
        },
        {
            "scenario": scenario("b"),
            "result": rows[3],
            "outcome": None,
            "started": True,
        },
        {
            "scenario": scenario("c", "rejected"),
            "result": rows[4],
            "outcome": None,
            "started": False,
        },
    ]
    metrics = summarize(rows, slots=slots, planned_count=6, expected_repeats=2)
    assert metrics["cohort"] == {
        "planned_slots": 6,
        "materialized_slots": 5,
        "started_trials": 4,
        "terminal_trials": 5,
        "pass_trials": 1,
        "fail_trials": 1,
        "indeterminate_trials": 3,
        "cancelled_trials": 1,
        "missing_or_pending_slots": 1,
        "failure_classes": {
            "agent": 1,
            "provider": 1,
            "evaluator": 0,
            "infrastructure": 1,
            "cancelled": 1,
        },
    }
    assert metrics["task_success_rate"]["value"] == 0.5
    assert metrics["task_success_rate"]["sample_count"] == 2
    assert metrics["end_to_end_valid_completion"]["value"] == pytest.approx(1 / 6)
    assert metrics["started_valid_completion"]["value"] == 0.25
    assert metrics["business_fulfillment"]["numerator"] == 1
    assert metrics["business_fulfillment"]["sample_count"] == 4
    assert metrics["unsafe_action_rate"]["numerator"] == 1
    assert metrics["unsafe_action_rate"]["sample_count"] == 2
    assert metrics["unsafe_action_rate"]["unknown_count"] == 4
    assert metrics["tool_call_validity"]["provider_rejected_generations"] == 1
    assert metrics["tool_call_validity"]["rejected_local_attempts"] == 1
    assert metrics["repeated_run_reliability"]["value"] is None
    assert metrics["repeated_run_reliability"]["planned_scenarios"] == 3
    assert metrics["repeated_run_reliability"]["excluded_scenarios"] == 3
    assert metrics["repeated_run_reliability"]["missing_repeats"] == 1
    assert metrics["token_usage"]["input"] == 33
    assert metrics["token_usage"]["output"] == 7
    assert metrics["token_usage"]["input_known_requests"] == 3
    assert metrics["token_usage"]["output_known_requests"] == 2
    assert metrics["estimated_cost_usd"]["value"] == pytest.approx(0.03)
    assert metrics["estimated_cost_usd"]["unknown_count"] == 3
    assert metrics["reported_cost_usd"]["value"] is None
    assert metrics["estimated_cost_per_success_usd"]["value"] is None
    assert metrics["model_latency_ms"]["sample_count"] == 5
    assert metrics["model_latency_ms"]["p50"] == 70
    assert metrics["model_latency_ms"]["p95"] == pytest.approx(96)
    assert metrics["execution_latency_ms"]["sample_count"] == 5


def test_zero_denominators_and_censored_observations_are_unavailable():
    empty = summarize([])
    assert empty["task_success_rate"]["value"] is None
    assert empty["end_to_end_valid_completion"]["value"] is None
    assert empty["estimated_cost_per_success_usd"]["value"] is None
    pending = summarize(
        [],
        slots=[{"scenario": scenario("a"), "result": None, "started": False}],
        planned_count=1,
    )
    assert pending["cohort"]["missing_or_pending_slots"] == 1
    assert pending["task_success_rate"]["value"] is None
    assert pending["end_to_end_valid_completion"]["value"] == 0
    assert pending["model_latency_ms"]["p50"] is None
    assert pending["active_task_latency_ms"]["missing_count"] == 0


def test_cost_per_success_includes_failed_calls_and_keeps_cost_types_separate():
    passed = result(
        "a",
        "PASS",
        calls=[
            {
                "input_tokens": 10,
                "output_tokens": 5,
                "estimated_cost_usd": 1.0,
                "reported_cost_usd": 4.0,
                "latency_ms": 5,
            }
        ],
    )
    failed = result(
        "b",
        "FAIL",
        "agent",
        calls=[
            {
                "input_tokens": 20,
                "output_tokens": 4,
                "estimated_cost_usd": 2.0,
                "reported_cost_usd": 5.0,
                "latency_ms": 8,
            }
        ],
    )
    metrics = summarize([passed, failed])
    assert metrics["estimated_cost_per_success_usd"]["value"] == 3
    assert metrics["reported_cost_per_success_usd"]["value"] == 9
    assert metrics["estimated_cost_usd"]["sample_count"] == 2
    assert metrics["reported_cost_usd"]["sample_count"] == 2

    unpriced = result(
        "a", "PASS", calls=[{"input_tokens": 10, "output_tokens": 5, "latency_ms": 5}]
    )
    unknown = summarize([unpriced])
    assert unknown["token_usage"]["input"] == 10
    assert unknown["estimated_cost_usd"]["value"] is None
    assert unknown["reported_cost_usd"]["value"] is None
    assert unknown["estimated_cost_per_success_usd"]["value"] is None


def test_tiny_all_success_and_correlated_repeats_have_no_population_interval():
    rows = [result(key, "PASS") for key in ("a", "a", "b", "b")]
    slots = [
        {
            "scenario": scenario(row["scenario_id"]),
            "result": row,
            "started": True,
            "outcome": {"resolution": "completed"},
        }
        for row in rows
    ]
    metrics = summarize(rows, slots=slots, planned_count=4, expected_repeats=2)
    assert metrics["task_success_rate"]["value"] == 1
    assert metrics["task_success_rate"]["interval"] is None
    assert metrics["task_success_rate"]["scenario_count"] == 2
    assert metrics["repeated_run_reliability"]["value"] == 1
    assert metrics["repeated_run_reliability"]["sample_count"] == 2
    assert rate(rows * 4, lambda row: row["verdict"] == "PASS")["scenario_count"] == 2


def test_recovery_requires_delivered_fault_and_absent_scenarios_are_excluded():
    not_delivered = result(
        "a",
        "PASS",
        recovery_eligible=True,
        recovery_succeeded=False,
        assertions=[{"id": "fault_observed", "verdict": "PASS", "actual": False}],
    )
    delivered = result(
        "b",
        "PASS",
        recovery_eligible=True,
        recovery_succeeded=True,
        assertions=[{"id": "fault_observed", "verdict": "PASS", "actual": True}],
    )
    unknown = result(
        "c",
        "INDETERMINATE",
        "provider",
        policy="INDETERMINATE",
        recovery_eligible=True,
        recovery_succeeded=None,
        assertions=[{"id": "fault_observed", "verdict": "PASS", "actual": True}],
    )
    metrics = summarize([not_delivered, delivered, unknown])
    recovery = metrics["recovery_success_rate"]
    assert recovery["planned_recovery_cases"] == 3
    assert recovery["fault_not_delivered_count"] == 1
    assert recovery["opportunity_count"] == 2
    assert recovery["sample_count"] == 1
    assert recovery["unknown_count"] == 1
    assert recovery["value"] == 1

    one_slot = [
        {"scenario": scenario("a"), "result": result("a", "PASS"), "started": True}
    ]
    repeats = summarize(
        [one_slot[0]["result"]],
        slots=one_slot,
        planned_count=4,
        expected_repeats=2,
        planned_scenario_ids=["a", "b"],
    )["repeated_run_reliability"]
    assert repeats["planned_scenarios"] == 2
    assert repeats["missing_repeats"] == 3
    assert repeats["value"] is None


async def test_finalized_metrics_match_trial_rows_and_keep_provenance(
    benchmark_factory, dsn
):
    evaluation_id, trial_ids, _ = benchmark_factory(ids=["cs-001", "cs-006"], version=5)
    for trial_id in trial_ids:
        assert (await execute_trial(trial_id, dsn))["verdict"] == "PASS"
    metrics = finalize(evaluation_id, dsn=dsn)
    assert metrics["cohort"]["planned_slots"] == 2
    assert metrics["cohort"]["started_trials"] == 2
    assert metrics["cohort"]["terminal_trials"] == 2
    assert metrics["end_to_end_valid_completion"]["value"] == 1
    assert metrics["business_fulfillment"]["numerator"] == 1
    assert metrics["business_fulfillment"]["sample_count"] == 1
    assert metrics["active_task_latency_ms"]["sample_count"] == 0
    assert metrics["active_task_latency_ms"]["missing_count"] == 2
    assert metrics["prestart_wait_ms"]["sample_count"] == 2
    assert metrics["trial_elapsed_ms"]["sample_count"] == 2
    assert metrics["evaluation_elapsed_ms"]["value"] >= 0
    with connect(dsn) as conn:
        assert verify_binding(conn, evaluation_id, "evaluation") is True
        assert all(
            verify_binding(conn, trial_id, "benchmark") is True
            for trial_id in trial_ids
        )
