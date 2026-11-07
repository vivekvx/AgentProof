import json
import os
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest


def get(url, payload=None):
    with urlopen(
        Request(
            url,
            data=json.dumps(payload).encode() if payload else None,
            headers={"Content-Type": "application/json"},
        ),
        timeout=10,
    ) as response:
        return json.load(response)


def test_live_benchmark_trace_correlations():
    base = os.getenv("E2E_BASE_URL")
    jaeger = os.getenv("JAEGER_QUERY_URL")
    if not base or not jaeger:
        pytest.skip("Set E2E_BASE_URL and JAEGER_QUERY_URL")
    accepted = get(
        base + "/api/v2/evaluations",
        {
            "dataset_id": "customer-support",
            "dataset_version": 2,
            "agent_id": "support-safe",
            "agent_version": 1,
            "split": "development",
            "trials": 1,
            "scenario_ids": ["cs-016"],
        },
    )
    for _ in range(100):
        evaluation = get(base + "/api/v2/evaluations/" + accepted["id"])
        if evaluation["status"] == "completed":
            break
        time.sleep(0.2)
    trial = evaluation["trials"][0]
    assert trial["result"]["verdict"] == "PASS"
    trace_id = trial["result"]["trace_id"]
    complete = None
    for _ in range(100):
        try:
            data = get(jaeger + "/api/traces/" + trace_id)
        except HTTPError as error:
            if error.code != 404:
                raise
            time.sleep(0.3)
            continue
        if data.get("data"):
            trace = data["data"][0]
            names = {s["operationName"] for s in trace["spans"]}
            span_ids = {s["spanID"] for s in trace["spans"]}
            closed = all(
                p["spanID"] in span_ids for s in trace["spans"] for p in s["references"]
            )
            if (
                "benchmark.trial" in names
                and "benchmark.tool.cancel_order" in names
                and closed
            ):
                complete = trace
                break
        time.sleep(0.3)
    assert complete is not None
    span = next(s for s in complete["spans"] if s["operationName"] == "benchmark.trial")
    attrs = {t["key"]: t["value"] for t in span["tags"]}
    assert attrs["agentproof.run_id"] == evaluation["id"]
    assert (
        attrs["agentproof.dataset.id"] == "customer-support"
        and attrs["agentproof.dataset.version"] == 2
    )
    assert (
        attrs["agentproof.scenario.id"] == "cs-016"
        and attrs["agentproof.trial_id"] == trial["id"]
    )
    assert (
        attrs["agentproof.evaluator.version"] == "support-evaluator-v1"
        and attrs["temporal.activity.attempt"] == 1
    )
    assert attrs["agentproof.failure_category"] == "none"
    services = {p["serviceName"] for p in complete["processes"].values()}
    assert {"agentproof-api", "agentproof-worker"} <= services
    # Sensitive request and assertion content must not appear in exported model spans.
    assert "assertions" not in json.dumps(complete)
    evidence = Path(os.getenv("EVIDENCE_DIR", ".local/implementation-03"))
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / "benchmark-trace.json").write_text(
        json.dumps({"evaluation": evaluation, "trace": complete}, indent=2)
    )
