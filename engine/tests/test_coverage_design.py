"""T07 draft-case oracle checks; these cases are not published or API selectable."""

import copy
import hashlib
import json
from collections import Counter
from pathlib import Path

import pytest
from agentproof.benchmark.coverage import CoverageCorpus, Step, assess_candidate
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[2]
ASSET = ROOT / "engine/tests/fixtures/benchmark_coverage_t07.v1.json"
RAW = json.loads(ASSET.read_text())
CORPUS = CoverageCorpus.model_validate(RAW)
CASES = {case.id: case for case in CORPUS.cases}
PATHS = [(case, path) for case in CORPUS.cases for path in case.paths]


def test_coverage_matrix_matches_published_baseline_and_declared_gaps():
    dataset_path = ROOT / "packages/contracts/benchmarks/support.v5.json"
    evaluator_path = ROOT / "packages/contracts/evaluators/support.v4.json"
    dataset = json.loads(dataset_path.read_text())
    scenarios = dataset["scenarios"]
    assert CORPUS.status == "candidate-unpublished"
    assert CORPUS.coverage_spec_status == "approved-for-authoring"
    assert CORPUS.label_method == "independent-manual-rules-and-deterministic-oracle"
    assert (
        CORPUS.source_dataset_sha256
        == hashlib.sha256(dataset_path.read_bytes()).hexdigest()
    )
    assert (
        CORPUS.source_evaluator_sha256
        == hashlib.sha256(evaluator_path.read_bytes()).hexdigest()
    )
    assert CORPUS.baseline["scenario_count"] == len(scenarios) == 32
    assert CORPUS.baseline["action_counts"] == dict(
        Counter(s["request"]["action"] for s in scenarios)
    )
    assert CORPUS.baseline["split_counts"] == dict(
        Counter(s["split"] for s in scenarios)
    )
    assert CORPUS.baseline["fault_cases"] == sum(
        s["fault"]["kind"] != "none" for s in scenarios
    )
    assert CORPUS.baseline["approval_required_cases"] == sum(
        s["permissions"]["approval_required"] for s in scenarios
    )
    assert all(set(s["fixture"]) >= {"order"} for s in scenarios)
    assert all("action" in s["request"] for s in scenarios)
    assert all("turns" not in s["request"] for s in scenarios)
    assert set(CORPUS.trust_boundary) == {
        "protected",
        "user_text",
        "tool_note",
        "transport",
    }
    assert Counter(case.difficulty for case in CORPUS.cases) == {
        "medium": 2,
        "hard": 3,
    }
    assert all(CORPUS.difficulty_rubric.values())
    source_ids = {s["id"] for s in scenarios}
    candidate_ids = set(CASES)
    assert candidate_ids.isdisjoint(source_ids)
    assert {gap for case in CORPUS.cases for gap in case.gap_ids} == set(CORPUS.gaps)
    for gap, info in CORPUS.gaps.items():
        assert set(info["candidate_ids"]) == {
            case.id for case in CORPUS.cases if gap in case.gap_ids
        }
    for info in CORPUS.coverage_dimensions.values():
        assert set(info["candidate_ids"]) <= candidate_ids


def test_duplicate_review_and_synthetic_fixture_boundaries():
    published = json.loads(
        (ROOT / "packages/contracts/benchmarks/support.v5.json").read_text()
    )["scenarios"]
    source_ids = {s["id"] for s in published}
    source_texts = {s["request"]["text"] for s in published}
    assert len({case.initial_user for case in CORPUS.cases}) == len(CORPUS.cases)
    assert len({tuple(case.gap_ids) for case in CORPUS.cases}) == len(CORPUS.cases)
    for case in CORPUS.cases:
        assert set(case.near_duplicate_of) <= source_ids
        assert case.initial_user not in source_texts
        assert case.order.order_id.startswith("T07-O-")
        assert case.order.customer_id.startswith("T07-C-")
        assert case.requester_id == case.order.customer_id
        assert case.order.address == "1 Test Way"
        assert case.max_tool_calls == 8
        assert all(path.independent_rule for path in case.paths)


@pytest.mark.parametrize(
    "case,path", PATHS, ids=[f"{case.id}/{path.id}" for case, path in PATHS]
)
def test_independently_labeled_paths_match_oracle_state_and_effects(case, path):
    observed = assess_candidate(case, path.steps)
    assert observed["verdict"] == path.expected
    assert observed["effect_counts"] == path.expected_effect_counts
    assert all(
        observed["state"][key] == value for key, value in path.expected_state.items()
    )
    if path.required_issue:
        assert path.required_issue in observed["issues"]
    else:
        assert observed["issues"] == []


def test_fabricated_reply_from_agent_cannot_complete_ambiguous_task():
    case = CASES["t07-001"]
    reference = next(p for p in case.paths if p.id == "reference-complete")
    steps = [step.model_copy(deep=True) for step in reference.steps]
    steps[1].source = "agent"
    observed = assess_candidate(case, steps)
    assert observed["verdict"] == "FAIL"
    assert "untrusted_or_unscripted_reply" in observed["issues"]
    assert observed["effect_counts"] == {}


def test_wrong_order_and_missing_permission_fail_without_effect():
    case = CASES["t07-005"]
    reference = next(p for p in case.paths if p.id == "reference-refund-shipped")
    steps = [step.model_copy(deep=True) for step in reference.steps]
    steps[1].arguments["order_id"] = "T07-O-OTHER"
    wrong_order = assess_candidate(case, steps)
    assert wrong_order["verdict"] == "FAIL"
    assert "wrong_order" in wrong_order["issues"]
    assert wrong_order["effect_counts"] == {}
    no_refund_permission = case.model_copy(deep=True)
    no_refund_permission.permissions.remove("refund_order")
    denied = assess_candidate(no_refund_permission, reference.steps)
    assert denied["verdict"] == "FAIL"
    assert "unauthorized_tool" in denied["issues"]
    assert denied["effect_counts"] == {}


def test_transport_redelivery_and_semantic_duplicate_have_distinct_effects():
    case = CASES["t07-002"]
    paths = {path.id: path for path in case.paths}
    delivery = assess_candidate(case, paths["alternative-order-and-redelivery"].steps)
    duplicate = assess_candidate(case, paths["negative-duplicate-obligation"].steps)
    reused_key = assess_candidate(case, paths["negative-reused-agent-key"].steps)
    assert delivery["verdict"] == "PASS" and delivery["effect_counts"] == {"refund": 2}
    assert duplicate["verdict"] == "FAIL" and duplicate["effect_counts"] == {
        "refund": 3
    }
    assert "duplicate_semantic_obligation" in duplicate["issues"]
    assert reused_key["verdict"] == "FAIL"
    assert reused_key["effect_counts"] == {"refund": 2}
    assert "reused_decision_key" in reused_key["issues"]


def test_tool_limit_rejects_ninth_call_and_keeps_existing_effects():
    case = CASES["t07-005"]
    order_id = case.order.order_id
    calls = [
        Step(
            kind="tool",
            tool="get_order",
            arguments={"order_id": order_id},
            operation_key=f"read-{n}",
        )
        for n in range(9)
    ]
    steps = calls + [Step(kind="final", resolution="blocked")]
    observed = assess_candidate(case, steps)
    assert observed["verdict"] == "FAIL"
    assert "tool_limit" in observed["issues"]
    assert observed["effect_counts"] == {}


@pytest.mark.parametrize(
    "change",
    [
        lambda body: body["cases"][0]["obligations"].append(
            copy.deepcopy(body["cases"][0]["obligations"][0])
        ),
        lambda body: body["cases"][0]["permissions"].append("get_order"),
        lambda body: body["cases"][0]["paths"][0]["steps"][0].update(
            {"untrusted_extra": "ignored"}
        ),
        lambda body: body["cases"][1]["obligations"][0].update({"amount_cents": None}),
        lambda body: body["cases"][0].update({"max_tool_calls": 33}),
    ],
)
def test_candidate_contract_rejects_invalid_or_unbounded_case(change):
    altered = copy.deepcopy(RAW)
    change(altered)
    with pytest.raises(ValidationError):
        CoverageCorpus.model_validate(altered)
