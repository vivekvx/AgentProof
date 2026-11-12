"""A separate HTTP-only test agent. It imports no AgentProof implementation code."""

import json
import re
import sys
import time
import urllib.error
import urllib.request
from functools import partial
from uuid import uuid4


def request(base, path, credential, method="GET", body=None, traceparent=None):
    data = None if body is None else json.dumps(body).encode()
    headers = {"Authorization": f"Bearer {credential}"}
    if traceparent:
        headers["traceparent"] = traceparent
    if data is not None:
        headers["Content-Type"] = "application/json"
    message = urllib.request.Request(
        base + path, data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(message, timeout=12) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


def run(config):
    task = config["task"]
    base = config["base_url"]
    trial = task["trial_id"]
    run_id = task["run_id"]
    credential = config["credential"]
    mode = config.get("mode", "safe")
    send = partial(request, traceparent=config.get("traceparent"))
    match = re.search(r"(?m)^Order ID: ([^\n]+)$", task["request"])
    if not match:
        raise ValueError("public task has no order ID")
    order_id = match.group(1)
    allowed = {tool["name"] for tool in task["tools"]}
    sequence = 0
    observations = []

    def tool(
        name, arguments, operation_id=None, sequence_override=None, run_override=None
    ):
        nonlocal sequence
        if sequence_override is None:
            sequence += 1
            current = sequence
        else:
            current = sequence_override
        operation_id = operation_id or str(uuid4())
        call = {
            "schema_version": "1.0",
            "run_id": run_override or run_id,
            "trial_id": trial,
            "sequence": current,
            "operation_id": operation_id,
            "tool": name,
            "arguments": arguments,
        }
        status, response = send(
            base, f"/agent/v1/sessions/{trial}/tools", credential, "POST", call
        )
        observations.append(
            {
                "tool": name,
                "sequence": current,
                "operation_id": operation_id,
                "status": status,
                "error_code": response.get("error_code"),
            }
        )
        return status, response, operation_id

    def finish(resolution):
        return send(
            base,
            f"/agent/v1/sessions/{trial}/complete",
            credential,
            "POST",
            {
                "schema_version": "1.0",
                "run_id": run_id,
                "trial_id": trial,
                "last_sequence": sequence,
                "resolution": resolution,
            },
        )

    if mode == "cancel_session":
        status, response = send(
            base, f"/agent/v1/sessions/{trial}", credential, "DELETE"
        )
    elif mode in {"disconnect", "expire"}:
        if mode == "expire":
            time.sleep(config.get("sleep_seconds", 4))
            tool("get_order", {"order_id": order_id})
        return {"mode": mode, "observations": observations, "finished": False}
    else:
        text = task["request"].splitlines()[0].lower()
        if mode == "false_claim":
            resolution = "completed"
        elif mode == "forbidden":
            tool("cancel_order", {"order_id": order_id})
            resolution = "blocked"
        elif mode == "invalid_arguments":
            tool("cancel_order", {"order_id": "wrong-order"})
            resolution = "completed"
        elif mode == "approval_bypass":
            tool("refund_order", {"order_id": order_id, "amount_cents": 2500})
            resolution = "completed"
        elif mode == "forged_identity":
            tool("get_order", {"order_id": order_id}, run_override=str(uuid4()))
            resolution = "unknown"
        elif mode == "cross_session":
            target = config["target_task"]
            status, response = send(
                base,
                f"/agent/v1/sessions/{target['trial_id']}/tools",
                credential,
                "POST",
                {
                    "schema_version": "1.0",
                    "run_id": target["run_id"],
                    "trial_id": target["trial_id"],
                    "sequence": 1,
                    "operation_id": str(uuid4()),
                    "tool": "get_order",
                    "arguments": {"order_id": order_id},
                },
            )
            observations.append(
                {
                    "tool": "get_order",
                    "status": status,
                    "error_code": response.get("error_code"),
                }
            )
            resolution = "unknown"
        elif mode in {"direct", "duplicate", "redelivery", "replay_changed"}:
            first, _, operation_id = tool("cancel_order", {"order_id": order_id})
            if mode == "duplicate":
                tool("cancel_order", {"order_id": order_id})
            elif mode == "redelivery":
                tool("cancel_order", {"order_id": order_id}, operation_id, 1)
            elif mode == "replay_changed":
                tool("cancel_order", {"order_id": "wrong-order"}, operation_id, 1)
            resolution = "completed" if first == 200 else "blocked"
        elif "cancel" in text:
            if "cancel_order" not in allowed:
                resolution = "blocked"
            else:
                if "get_order" in allowed and task["limits"]["max_tool_calls"] > 1:
                    tool("get_order", {"order_id": order_id})
                status, _, _ = tool("cancel_order", {"order_id": order_id})
                if status == 503 and "get_order" in allowed:
                    _, observed, _ = tool("get_order", {"order_id": order_id})
                    state = observed.get("result", {}).get("order", {}).get("status")
                    resolution = "completed" if state == "cancelled" else "blocked"
                else:
                    resolution = "completed" if status == 200 else "blocked"
        elif "refund" in text:
            resolution = "blocked" if "approve" not in text else "completed"
            if resolution == "completed":
                tool("get_order", {"order_id": order_id})
                tool("request_approval", {"order_id": order_id})
                status, _, _ = tool(
                    "refund_order", {"order_id": order_id, "amount_cents": 2500}
                )
                if status != 200:
                    resolution = "blocked"
        elif "status" in text:
            status, _, _ = tool("get_order", {"order_id": order_id})
            resolution = "completed" if status == 200 else "blocked"
        else:
            resolution = "unknown"
        status, response = finish(resolution)

    if status == 202:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            code, result = send(base, f"/agent/v1/sessions/{trial}", credential)
            if code == 200 and result.get("trial_status") == "completed":
                break
            time.sleep(0.1)
        else:
            raise TimeoutError("final evaluation did not complete")
    else:
        result = response
    return {
        "mode": mode,
        "observations": observations,
        "completion_status": status,
        "trial_status": result.get("trial_status"),
        "session_state": result.get("session_state"),
        "verdict": result.get("result", {}).get("verdict"),
        "unsafe_actions": result.get("result", {}).get("unsafe_actions"),
    }


if __name__ == "__main__":
    print(json.dumps(run(json.load(sys.stdin)), sort_keys=True))
