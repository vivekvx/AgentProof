"""Predeclared matched study checks. This module makes no provider requests."""

import hashlib
import json
import math
from pathlib import Path

from ..artifacts import ROOT

PLAN_PATH = ROOT / "packages/contracts/experiments/groq-gpt-oss-matched.v1.json"


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def load_plan(path=PLAN_PATH):
    return json.loads(Path(path).read_text())


def verify_plan(plan, profile_dir=None, root=ROOT):
    """Fail before spending if references, settings, or ceilings disagree."""
    if (
        plan.get("schema_version") != "experiment-plan-v1"
        or plan.get("version") != 1
        or plan.get("id") != "groq-gpt-oss-matched-development"
        or plan.get("dataset", {}).get("id") != "customer-support"
        or plan.get("dataset", {}).get("version") != 5
        or plan.get("evaluator", {}).get("version") != "support-evaluator-v4"
        or plan.get("tool_schema", {}).get("version") != "support-tools-v1"
    ):
        raise ValueError("unsupported experiment plan")
    assets = {
        "dataset": "benchmarks/support.v5.json",
        "evaluator": "evaluators/support.v4.json",
        "tool_schema": "tools/support-tools.v1.json",
    }
    for name, relative in assets.items():
        raw = (Path(root) / "packages/contracts" / relative).read_bytes()
        if digest(raw) != plan[name]["sha256"]:
            raise ValueError(f"{name} reference changed")
    data = json.loads(
        (Path(root) / "packages/contracts" / assets["dataset"]).read_text()
    )
    selected = plan["scenario_ids"]
    if (
        len(selected) != len(set(selected))
        or not selected
        or any(
            s not in {x["id"] for x in data["scenarios"] if x["split"] == plan["split"]}
            for s in selected
        )
    ):
        raise ValueError("scenario selection differs from published split")
    scenarios = {s["id"]: s for s in data["scenarios"]}
    for identifier in selected:
        if digest(canonical(scenarios[identifier])) != plan["scenario_sha256"].get(
            identifier
        ):
            raise ValueError("scenario fixture or ground truth changed")
    repeats = plan["trials_per_scenario"]
    if type(repeats) is not int or not 2 <= repeats <= 5:
        raise ValueError("unsupported repeat count")
    if [p["model"] for p in plan["profiles"]] != [
        "openai/gpt-oss-20b",
        "openai/gpt-oss-120b",
    ]:
        raise ValueError("model pair changed")
    limits = plan["per_trial_limits"]
    ceiling = plan["study_ceiling"]
    if (
        limits["max_requests"] > 6
        or limits["max_tool_calls"] > 10
        or limits["max_tokens"] > 64000
        or limits["max_active_seconds"] > 5
        or limits["max_estimated_cost_usd"] > 0.1
        or ceiling["max_wall_seconds"] > 180
    ):
        raise ValueError("predeclared study budget exceeded")
    if any(
        limits["max_tool_calls"] > scenarios[identifier]["limits"]["max_tool_calls"]
        or limits["max_active_seconds"] * 1000
        > scenarios[identifier]["limits"]["wall_ms"]
        for identifier in selected
    ):
        raise ValueError("study budget exceeds scenario limit")
    slots = len(selected) * repeats * 2
    for key, per_trial in (
        ("max_requests", "max_requests"),
        ("max_tool_calls", "max_tool_calls"),
        ("max_tokens", "max_tokens"),
        ("max_active_seconds", "max_active_seconds"),
        ("max_estimated_cost_usd", "max_estimated_cost_usd"),
    ):
        if limits[per_trial] <= 0 or not math.isclose(
            ceiling[key], slots * limits[per_trial]
        ):
            raise ValueError("study ceiling differs from planned slots")
    if ceiling["planned_slots"] != slots or ceiling["max_wall_seconds"] <= 0:
        raise ValueError("invalid study ceiling")
    settings = plan["effective_settings"]
    if (
        settings["base_url"] != "https://api.groq.com/openai/v1"
        or settings["provider"] != "openai-compatible"
        or settings["probe_tool_choice"] != "required"
        or settings["business_tool_choice"] != "auto"
        or plan["status"] != "predeclared-not-authorized-to-run"
        or "alternate 20B-first and 120B-first" not in plan["sampling_order"]
    ):
        raise ValueError("experiment execution policy changed")
    analysis = plan["analysis"]
    if (
        not 0 < analysis["minimum_relevant_difference"] <= 1
        or not 0 < analysis["precision_goal_half_width"] <= 1
        or not 0 < analysis["minimum_complete_pair_fraction"] <= 1
        or analysis["confidence"] != 0.95
        or "Hoeffding" not in analysis["interval_method"]
        or "no replacement trials" not in analysis["stop_rule"]
    ):
        raise ValueError("invalid statistical decision gate")
    if settings["temperature"] is not None or settings["top_p"] is not None:
        raise ValueError("GPT-OSS sampling setting is ineffective")
    if (
        settings["max_completion_tokens"] <= 0
        or settings["parallel_tool_calls"] is not False
    ):
        raise ValueError("unsupported effective settings")
    if profile_dir is not None:
        for profile in plan["profiles"]:
            path = Path(profile_dir) / f"{profile['id']}.v{profile['version']}.json"
            raw = path.read_bytes()
            if digest(raw) != profile["sha256"]:
                raise ValueError("model profile bytes changed")
            content = json.loads(raw)
            config = content["model_configuration"]
            if (
                content["id"] != profile["id"]
                or config["model"] != profile["model"]
                or content["prompt_version"] != plan["prompt_version"]
                or digest(content["prompt"].encode()) != plan["prompt_sha256"]
                or content["max_retries"] != settings["max_retries"]
                or config["provider"] != settings["provider"]
                or config["base_url"] != settings["base_url"]
                or config["max_requests"] != limits["max_requests"]
                or config["max_tool_calls"] != limits["max_tool_calls"]
                or config["max_tokens"] != limits["max_tokens"]
                or config["max_estimated_cost_usd"] != limits["max_estimated_cost_usd"]
                or config["max_output_tokens"] != settings["max_completion_tokens"]
                or config["timeout_seconds"] != settings["timeout_seconds"]
                or config["temperature"] != 0
                or config["top_p"] != 1
            ):
                raise ValueError("profile differs from matched execution settings")
    return {"planned_slots": slots, "profile_bytes_checked": profile_dir is not None}


def cluster_interval(values, confidence=0.95, low=-1.0, high=1.0):
    """Hoeffding bound over independent scenario clusters, not repeat slots."""
    if (
        not values
        or not 0 < confidence < 1
        or any(not low <= v <= high for v in values)
    ):
        raise ValueError("invalid cluster observations")
    center = sum(values) / len(values)
    radius = (high - low) * math.sqrt(
        math.log(2 / (1 - confidence)) / (2 * len(values))
    )
    return [max(low, center - radius), min(high, center + radius)]


def analyze(plan, baseline, candidate):
    """Keep all planned pairs and classify unknown outcomes without replacement."""
    selected = plan["scenario_ids"]
    repeats = plan["trials_per_scenario"]
    categories = {"complete": 0, "missing": 0, "indeterminate": 0}
    cluster_deltas = []
    for scenario in selected:
        differences = []
        for index in range(1, repeats + 1):
            key = (scenario, index)
            left, right = baseline.get(key), candidate.get(key)
            if left is None or right is None:
                categories["missing"] += 1
            elif left not in ("PASS", "FAIL") or right not in ("PASS", "FAIL"):
                categories["indeterminate"] += 1
            else:
                categories["complete"] += 1
                differences.append(int(right == "PASS") - int(left == "PASS"))
        if len(differences) == repeats:
            cluster_deltas.append(sum(differences) / repeats)
    total = len(selected) * repeats
    interval = (
        cluster_interval(cluster_deltas, plan["analysis"]["confidence"])
        if len(cluster_deltas) == len(selected)
        else None
    )
    point = sum(cluster_deltas) / len(cluster_deltas) if interval else None
    analysis = plan["analysis"]
    precision = (
        interval is not None
        and (interval[1] - interval[0]) / 2 <= analysis["precision_goal_half_width"]
    )
    separation = interval is not None and (
        interval[0] > analysis["minimum_relevant_difference"]
        or interval[1] < -analysis["minimum_relevant_difference"]
    )
    eligible = (
        categories["complete"] / total >= analysis["minimum_complete_pair_fraction"]
    )
    return {
        "planned_pairs": total,
        "complete_pairs": categories["complete"],
        "missing_pairs": categories["missing"],
        "indeterminate_pairs": categories["indeterminate"],
        "complete_scenarios": len(cluster_deltas),
        "paired_delta": point,
        "cluster_interval": interval,
        "decision": "candidate_better"
        if eligible and precision and separation and point > 0
        else "baseline_better"
        if eligible and precision and separation and point < 0
        else "descriptive_only",
        "fixed_seed_guarantees_provider_repeatability": False,
    }
