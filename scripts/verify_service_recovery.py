"""Restart only the explicitly selected AgentProof Compose test services."""

import argparse
import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

parser = argparse.ArgumentParser()
parser.add_argument("--context", help="Docker context; default is the active context")
parser.add_argument("--project", required=True)
parser.add_argument("--env-file", default="infra/verification.env")
parser.add_argument("--base-url", default="http://127.0.0.1:8081")
args = parser.parse_args()
if not args.project.startswith("agentproof"):
    parser.error("Use a dedicated AgentProof test project")
control_token = os.environ.get("AGENTPROOF_API_TOKEN", "")
if len(control_token) < 32:
    parser.error("Set a local AGENTPROOF_API_TOKEN before recovery verification")
compose = [
    "docker",
    *(["--context", args.context] if args.context else []),
    "compose",
    "--env-file",
    args.env_file,
    "-p",
    args.project,
]
evidence = Path(".local/verification/recovery")
evidence.mkdir(parents=True, exist_ok=True)


def http(path, key=None):
    request = Request(
        args.base_url + path,
        data=b'{"agent":"safe"}' if key else None,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {control_token}",
            **({"Idempotency-Key": key} if key else {}),
        },
    )
    with urlopen(request, timeout=10) as response:
        return json.load(response)


def recover(run_id):
    for _ in range(100):
        run = http("/api/v1/runs/" + run_id)
        if run["result"]:
            assert run["result"]["status"] == "passed"
            assert run["result"]["side_effect_count"] == 1
            return run
        time.sleep(0.3)
    raise AssertionError("Run did not recover")


try:
    subprocess.run(compose + ["stop", "temporal"], check=True)
    key = "temporal-outage-" + str(uuid.uuid4())
    accepted = http("/api/v1/runs", key)
    queued = http("/api/v1/runs/" + accepted["id"])
    assert queued["status"] == "queued" and queued["result"] is None
    time.sleep(2)
finally:
    subprocess.run(compose + ["up", "-d", "--wait", "temporal"], check=True)
run = recover(accepted["id"])
try:
    subprocess.run(compose + ["stop", "postgres"], check=True)
    key = "postgres-outage-" + str(uuid.uuid4())
    try:
        http("/api/v1/runs", key)
        raise AssertionError("API accepted a request while PostgreSQL was stopped")
    except HTTPError as error:
        assert error.code == 503
finally:
    subprocess.run(compose + ["up", "-d", "--wait", "postgres"], check=True)
assert http("/api/v1/runs/" + run["id"]) == run
accepted_after = http("/api/v1/runs", key)
assert http("/api/v1/runs", key)["id"] == accepted_after["id"]
result = recover(accepted_after["id"])
(evidence / "service-recovery.json").write_text(
    json.dumps({"temporal_outage": run, "postgres_outage": result}, indent=2)
)
print("Temporal and PostgreSQL service recovery passed")
