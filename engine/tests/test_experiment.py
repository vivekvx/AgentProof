"""Pre-spend matched experiment gates; no model calls or credentials."""

import copy
import random

import pytest
from agentproof.benchmark.experiment import (
    analyze,
    cluster_interval,
    load_plan,
    verify_plan,
)


def test_published_plan_pins_assets_profiles_and_ceilings(tmp_path):
    plan = load_plan()
    assert verify_plan(plan) == {"planned_slots": 8, "profile_bytes_checked": False}
    changed = copy.deepcopy(plan)
    changed["dataset"]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="dataset reference"):
        verify_plan(changed)
    changed = copy.deepcopy(plan)
    changed["evaluator"]["version"] = "support-evaluator-v5"
    with pytest.raises(ValueError, match="unsupported experiment plan"):
        verify_plan(changed)
    changed = copy.deepcopy(plan)
    changed["scenario_sha256"]["cs-001"] = "0" * 64
    with pytest.raises(ValueError, match="scenario fixture"):
        verify_plan(changed)
    changed = copy.deepcopy(plan)
    changed["study_ceiling"]["max_requests"] -= 1
    with pytest.raises(ValueError, match="study ceiling"):
        verify_plan(changed)
    changed = copy.deepcopy(plan)
    changed["effective_settings"]["temperature"] = 0
    with pytest.raises(ValueError, match="ineffective"):
        verify_plan(changed)
    with pytest.raises(FileNotFoundError):
        verify_plan(plan, profile_dir=tmp_path)


def test_pairs_keep_missing_and_indeterminate_slots():
    plan = load_plan()
    baseline = {
        (scenario, trial): "PASS"
        for scenario in plan["scenario_ids"]
        for trial in (1, 2)
    }
    candidate = dict(baseline)
    candidate[("cs-001", 1)] = "FAIL"
    result = analyze(plan, baseline, candidate)
    assert result["planned_pairs"] == 4
    assert result["complete_pairs"] == 4
    assert result["complete_scenarios"] == 2
    assert result["decision"] == "descriptive_only"
    assert result["cluster_interval"] is not None
    assert result["fixed_seed_guarantees_provider_repeatability"] is False
    del candidate[("cs-001", 2)]
    candidate[("cs-016", 2)] = "INDETERMINATE"
    result = analyze(plan, baseline, candidate)
    assert (
        result["complete_pairs"],
        result["missing_pairs"],
        result["indeterminate_pairs"],
    ) == (2, 1, 1)
    assert result["paired_delta"] is None
    assert result["cluster_interval"] is None


def test_tiny_all_success_cannot_claim_certainty():
    plan = load_plan()
    values = {
        (scenario, trial): "PASS"
        for scenario in plan["scenario_ids"]
        for trial in (1, 2)
    }
    result = analyze(plan, values, values)
    assert result["paired_delta"] == 0
    assert result["cluster_interval"] == [-1.0, 1.0]
    assert result["decision"] == "descriptive_only"


@pytest.mark.parametrize("prevalence", [0.02, 0.3])
def test_cluster_interval_coverage_with_correlated_repeats_and_rare_events(prevalence):
    rng = random.Random(17)
    covered = 0
    for _ in range(1000):
        # Repeats within each scenario share one outcome. Only clusters count as samples.
        clusters = [int(rng.random() < prevalence) for _ in range(32)]
        low, high = cluster_interval(clusters, confidence=0.95, low=0, high=1)
        covered += low <= prevalence <= high
    assert covered >= 950
