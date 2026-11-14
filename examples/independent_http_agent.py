"""A standalone HTTP/JSON AgentProof client for the external-session protocol.

This example deliberately imports no AgentProof modules. It reads one JSON object
from stdin with ``base_url``, ``task``, and ``credential`` and prints a small
non-secret completion summary. Replace the decision logic with your own agent.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from uuid import uuid4


def call(base, path, credential, method="GET", body=None):
    headers = {"Authorization": f"Bearer {credential}"}
    data = None if body is None else json.dumps(body).encode()
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        base + path, data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


def run(config):
    task, credential = config["task"], config["credential"]
    base = (
        config.get("base_url") or os.environ["AGENTPROOF_EXTERNAL_BASE_URL"]
    ).rstrip("/")
    run_id, trial_id = task["run_id"], task["trial_id"]
    order = re.search(r"(?m)^Order ID: ([^\n]+)$", task["request"])
    if not order:
        raise ValueError("the public task has no order ID")
    order_id = order.group(1)
    allowed = {tool["name"] for tool in task["tools"]}
    sequence = 0
    mode = config.get("mode", "safe")

    def tool(name, arguments):
        nonlocal sequence
        sequence += 1
        return call(
            base,
            f"/agent/v1/sessions/{trial_id}/tools",
            credential,
            "POST",
            {
                "schema_version": "1.0",
                "run_id": run_id,
                "trial_id": trial_id,
                "sequence": sequence,
                "operation_id": str(uuid4()),
                "tool": name,
                "arguments": arguments,
            },
        )

    # This reference behavior is intentionally small. A real agent should make
    # decisions from the public task and permitted tool schemas only.
    if mode == "incomplete":
        return {"trial_id": trial_id, "status": "left_incomplete"}
    first_line = task["request"].splitlines()[0].lower()
    resolution = "blocked"
    if "cancel" in first_line and "cancel_order" in allowed:
        if "get_order" in allowed:
            tool("get_order", {"order_id": order_id})
        status, _ = tool("cancel_order", {"order_id": order_id})
        if mode == "duplicate" and status == 200:
            tool("cancel_order", {"order_id": order_id})
        resolution = "completed" if status == 200 else "blocked"
    elif "status" in first_line and "get_order" in allowed:
        status, _ = tool("get_order", {"order_id": order_id})
        resolution = "completed" if status == 200 else "blocked"

    status, result = call(
        base,
        f"/agent/v1/sessions/{trial_id}/complete",
        credential,
        "POST",
        {
            "schema_version": "1.0",
            "run_id": run_id,
            "trial_id": trial_id,
            "last_sequence": sequence,
            "resolution": resolution,
        },
    )
    if status != 202:
        raise RuntimeError(f"completion rejected: {result}")
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        status, result = call(base, f"/agent/v1/sessions/{trial_id}", credential)
        if status == 200 and result.get("trial_status") == "completed":
            evaluation = result.get("result") or {}
            return {
                "trial_id": trial_id,
                "verdict": evaluation.get("verdict"),
                "failure_category": evaluation.get("failure_category"),
                "session_state": result.get("session_state"),
            }
        time.sleep(0.2)
    raise TimeoutError("the trial did not terminalize before the session deadline")


if __name__ == "__main__":
    print(json.dumps(run(json.load(sys.stdin)), sort_keys=True))
