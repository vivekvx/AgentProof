"""Measure eight synthetic runs in the isolated local Compose stack."""

import argparse
import json
import os
import statistics
import time
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg

COUNT = 8
CONCURRENCY = 4
DEADLINE_SECONDS = 45
MAX_P95_QUEUE_MS = 5000
MAX_P95_ACTIVE_MS = 5000

parser = argparse.ArgumentParser()
parser.add_argument("--base-url", default="http://127.0.0.1:5174")
parser.add_argument("--database-url", required=True)
args = parser.parse_args()
address = urllib.parse.urlsplit(args.base_url)
if address.scheme != "http" or address.hostname not in {"127.0.0.1", "localhost"}:
    parser.error("Use only a local synthetic test endpoint")
if "agentproof_test" in args.database_url:
    parser.error("Use the isolated Compose application database, not the test database")

token = os.getenv("AGENTPROOF_API_TOKEN")
headers = {"Content-Type": "application/json"}
if token:
    headers["Authorization"] = f"Bearer {token}"


def call(path, method="GET", payload=None, idempotency_key=None):
    request_headers = dict(headers)
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    request = urllib.request.Request(
        args.base_url + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers=request_headers,
        method=method,
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


def percentile(values, rank):
    ordered = sorted(values)
    position = (len(ordered) - 1) * rank
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


started = time.monotonic()
batch = uuid.uuid4().hex


def submit(index):
    agent = "safe" if index % 2 == 0 else "duplicate"
    key = f"bounded-workload-{batch}-{index}"
    accepted = call("/api/v1/runs", "POST", {"agent": agent}, key)
    replay = call("/api/v1/runs", "POST", {"agent": agent}, key)
    assert accepted["id"] == replay["id"]
    return {"agent": agent, "id": accepted["id"], "key": key}


with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
    runs = list(pool.map(submit, range(COUNT)))
terminal_at = {}
deadline = time.monotonic() + DEADLINE_SECONDS
while len(terminal_at) != COUNT and time.monotonic() < deadline:
    for run in runs:
        if run["id"] in terminal_at:
            continue
        status = call("/api/v1/runs/" + run["id"])
        if status["result"] is not None:
            terminal_at[run["id"]] = time.monotonic()
    if len(terminal_at) != COUNT:
        time.sleep(0.1)
assert len(terminal_at) == COUNT, "bounded workload did not reach terminal state"

observed = []
with psycopg.connect(args.database_url) as conn:
    for run in runs:
        row = conn.execute(
            """SELECT r.status,r.result->>'status',r.created_at,
                      min(a.started_at),max(a.finished_at),
                      (SELECT status FROM orders o WHERE o.run_id=r.id),
                      (SELECT count(*) FROM side_effects s WHERE s.run_id=r.id),
                      (SELECT count(*) FROM tool_events e WHERE e.run_id=r.id)
               FROM runs r LEFT JOIN run_attempts a ON a.run_id=r.id
               WHERE r.id=%s GROUP BY r.id""",
            (run["id"],),
        ).fetchone()
        assert row is not None
        (
            status,
            verdict,
            created,
            first_attempt,
            finished,
            order_state,
            effects,
            events,
        ) = row
        expected_verdict = "passed" if run["agent"] == "safe" else "failed"
        expected_effects = 1 if run["agent"] == "safe" else 2
        assert status == "completed" and verdict == expected_verdict
        assert order_state == "cancelled"
        assert effects == expected_effects and events >= effects
        assert first_attempt is not None and finished is not None
        observed.append(
            {
                "run_id": run["id"],
                "agent": run["agent"],
                "verdict": verdict,
                "effects": effects,
                "events": events,
                "queue_wait_ms": round(
                    (first_attempt - created).total_seconds() * 1000, 2
                ),
                "active_ms": round(
                    (finished - first_attempt).total_seconds() * 1000, 2
                ),
            }
        )

wall = time.monotonic() - started
queue = [run["queue_wait_ms"] for run in observed]
active = [run["active_ms"] for run in observed]
summary = {
    "workload": {
        "runs": COUNT,
        "concurrency": CONCURRENCY,
        "agents": {"safe": 4, "duplicate": 4},
    },
    "wall_seconds": round(wall, 3),
    "throughput_runs_per_second": round(COUNT / wall, 3),
    "queue_wait_ms": {
        "p50": round(statistics.median(queue), 2),
        "p95": round(percentile(queue, 0.95), 2),
    },
    "active_ms": {
        "p50": round(statistics.median(active), 2),
        "p95": round(percentile(active, 0.95), 2),
    },
    "results": observed,
}
assert summary["queue_wait_ms"]["p95"] <= MAX_P95_QUEUE_MS
assert summary["active_ms"]["p95"] <= MAX_P95_ACTIVE_MS
path = Path(".local/verification/bounded-workload.json")
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(summary, indent=2, sort_keys=True))
print(
    json.dumps(
        {key: value for key, value in summary.items() if key != "results"},
        sort_keys=True,
    )
)
