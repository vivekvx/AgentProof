"""Verify the self-hosted Release 1 path against a running Compose installation.

The verification creates only new synthetic evaluations. It drives an external
HTTP/JSON agent as a separate process, checks trusted terminal results, restarts
the API and worker when requested, and exports/verifies/replays evidence without
provider credentials. It never deletes historical records or Compose volumes.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "examples" / "independent_http_agent.py"


def request(base, path, token, method="GET", body=None):
    headers = {"Authorization": f"Bearer {token}"}
    data = None if body is None else json.dumps(body).encode()
    if data is not None:
        headers["Content-Type"] = "application/json"
    message = urllib.request.Request(
        base + path, data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(message, timeout=10) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


def until(action, description, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = action()
        if value:
            return value
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {description}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--agent-id", default="preview-http-agent")
    parser.add_argument("--compose-project")
    args = parser.parse_args()
    control = os.environ.get("AGENTPROOF_API_TOKEN")
    if not control or len(control) < 32:
        parser.error("set a local AGENTPROOF_API_TOKEN with at least 32 characters")
    base = args.base_url.rstrip("/")

    def control_call(path, method="GET", body=None):
        return request(base, path, control, method, body)

    def healthy():
        return control_call("/healthz")[0] == 200

    assert until(healthy, "API health")
    code, agents = control_call("/api/v2/agents")
    assert code == 200 and any(item["id"] == args.agent_id for item in agents), (
        "external agent configuration is not mounted",
        code,
    )

    def run_case(mode, expected):
        body = {
            "dataset_id": "customer-support",
            "dataset_version": 5,
            "agent_id": args.agent_id,
            "agent_version": 1,
            "split": "development",
            "trials": 1,
            "scenario_ids": ["cs-001"],
        }
        code, created = control_call("/api/v2/evaluations", "POST", body)
        assert code == 202, created
        evaluation_id = created["id"]

        def ready_trial():
            status, evaluation = control_call(f"/api/v2/evaluations/{evaluation_id}")
            return (
                evaluation["trials"][0]["id"]
                if status == 200 and evaluation["trials"]
                else None
            )

        trial_id = until(ready_trial, "trial creation")

        def issue():
            status, value = control_call(
                f"/api/v2/external/sessions/{trial_id}", "POST"
            )
            return value if status == 201 else None

        session = until(issue, "external session")
        agent = subprocess.run(
            [sys.executable, str(AGENT)],
            input=json.dumps(
                {
                    "base_url": base,
                    "task": session["task"],
                    "credential": session["credential"],
                    "mode": mode,
                }
            ),
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert agent.returncode == 0, agent.stderr[-1000:]
        assert session["credential"] not in agent.stdout + agent.stderr

        def terminal():
            status, value = request(
                base,
                f"/agent/v1/sessions/{trial_id}",
                session["credential"],
            )
            if status == 200 and value.get("trial_status") == "completed":
                return value
            return None

        result = until(terminal, f"{mode} terminal result", timeout=90)
        verdict = result["result"]["verdict"]
        assert verdict == expected, (mode, verdict, result["result"])
        return {
            "evaluation_id": evaluation_id,
            "trial_id": trial_id,
            "verdict": verdict,
        }

    safe = run_case("safe", "PASS")
    restart_exit = None
    if args.compose_project:
        restarted = subprocess.run(
            [
                "docker",
                "compose",
                "-p",
                args.compose_project,
                "restart",
                "api",
                "worker",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        restart_exit = restarted.returncode
        assert restart_exit == 0, restarted.stderr[-1000:]
        assert until(healthy, "API after restart")
        code, persisted = control_call(f"/api/v2/evaluations/{safe['evaluation_id']}")
        assert code == 200
        terminal_trial = next(
            item for item in persisted["trials"] if item["id"] == safe["trial_id"]
        )
        assert terminal_trial["result"]["verdict"] == "PASS"

    defective = run_case("duplicate", "FAIL")
    incomplete = run_case("incomplete", "INDETERMINATE")
    exits = {}
    with tempfile.TemporaryDirectory(prefix="agentproof-preview-export-") as directory:
        bundle = Path(directory) / "safe-trial.json"
        env = {
            **os.environ,
            "DATABASE_URL": args.database_url,
            "AGENTPROOF_ALLOW_MODEL_REQUESTS": "false",
        }
        env.pop("GROQ_API_KEY", None)
        for name, command in (
            ("export", ["export", safe["trial_id"], str(bundle)]),
            ("verify", ["verify", str(bundle)]),
            ("replay", ["replay", str(bundle)]),
        ):
            completed = subprocess.run(
                [sys.executable, "-m", "agentproof.bundle", *command],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            exits[name] = completed.returncode
            assert completed.returncode == 0, completed.stderr[-1000:]
        replay = json.loads(completed.stdout)
        assert replay["verdict"] == "PASS"
    print(
        json.dumps(
            {
                "safe": safe,
                "defective": defective,
                "incomplete": incomplete,
                "restart_exit": restart_exit,
                "bundle_exits": exits,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
