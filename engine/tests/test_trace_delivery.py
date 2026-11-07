"""Live OTLP export regression: API -> Temporal -> Python -> Jaeger."""

import json
import os
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest


def get(url, payload=None, key=None):
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Idempotency-Key"] = key
    with urlopen(
        Request(
            url, data=json.dumps(payload).encode() if payload else None, headers=headers
        ),
        timeout=10,
    ) as response:
        return json.load(response)


def test_live_trace_delivery():
    jaeger = os.getenv("JAEGER_QUERY_URL")
    base = os.getenv("E2E_BASE_URL")
    if not jaeger or not base:
        pytest.skip(
            "Set JAEGER_QUERY_URL and E2E_BASE_URL for live export verification"
        )
    key = "trace-" + str(uuid.uuid4())
    accepted = get(base + "/api/v1/runs", {"agent": "safe"}, key)
    assert get(base + "/api/v1/runs", {"agent": "safe"}, key)["id"] == accepted["id"]
    run = None
    for _ in range(100):
        run = get(base + "/api/v1/runs/" + accepted["id"])
        if run["result"]:
            break
        time.sleep(0.2)
    assert run["result"]["status"] == "passed"
    trace_id = run["result"]["trace_id"]
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
            candidate = data["data"][0]
            names = {s["operationName"] for s in candidate["spans"]}
            services = {p["serviceName"] for p in candidate["processes"].values()}
            ids = {s["spanID"] for s in candidate["spans"]}
            closed = all(
                parent["traceID"] == trace_id and parent["spanID"] in ids
                for span in candidate["spans"]
                for parent in span["references"]
            )
            has_run_root = any(
                candidate["processes"][span["processID"]]["serviceName"]
                == "agentproof-api"
                and any(
                    tag["key"] == "agentproof.run_id" and tag["value"] == run["id"]
                    for tag in span["tags"]
                )
                for span in candidate["spans"]
            )
            if (
                closed
                and has_run_root
                and {"agentproof-api", "agentproof-worker"} <= services
                and {
                    "evaluation.execute",
                    "tool.cancel_order",
                    "tool.get_order",
                }
                <= names
            ):
                complete = candidate
                break
        time.sleep(0.3)
    assert complete is not None, f"Incomplete trace {trace_id}"
    span_ids = {s["spanID"] for s in complete["spans"]}
    assert all(event["span_id"] in span_ids for event in run["events"])
    execution = next(
        s for s in complete["spans"] if s["operationName"] == "evaluation.execute"
    )
    attrs = {tag["key"]: tag["value"] for tag in execution["tags"]}
    assert attrs["agentproof.run_id"] == run["id"]
    assert attrs["agentproof.scenario.id"] == "cancel-order"
    assert attrs["agentproof.scenario.sha256"] == run["manifest"]["scenario"]["sha256"]
    assert attrs["temporal.activity.attempt"] == 1
    assert attrs["temporal.activity.id"]
    assert attrs["temporal.workflow.id"] == "evaluation-" + run["id"]
    api = next(
        s
        for s in complete["spans"]
        if complete["processes"][s["processID"]]["serviceName"] == "agentproof-api"
        and any(
            t["key"] == "agentproof.run_id" and t["value"] == run["id"]
            for t in s["tags"]
        )
    )
    assert {t["key"]: t["value"] for t in api["tags"]}["agentproof.run_id"] == run["id"]
    for span in complete["spans"]:
        for parent in span["references"]:
            assert parent["traceID"] == trace_id
            assert parent["spanID"] in span_ids
    evidence = Path(os.getenv("EVIDENCE_DIR", ".local/implementation-02"))
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / "jaeger-trace.json").write_text(
        json.dumps({"run": run, "trace": complete}, indent=2)
    )
