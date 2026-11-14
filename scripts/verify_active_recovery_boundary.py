"""Exercise active-work interruption and state the supported recovery boundary.

This uses the existing isolated mock study. It verifies a restarted worker keeps
the durable study identity, reservations, and one-effect-per-trial invariant.
It intentionally does not claim that a copied Temporal volume can resume an
active workflow: that needs a coordinated snapshot protocol not provided by the
local development SQLite service.
"""

import json
import os
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    database = os.environ.get("AGENTPROOF_STUDY_TEST_DB")
    if not database:
        raise SystemExit("Set a fresh dedicated AGENTPROOF_STUDY_TEST_DB")
    env = dict(os.environ, AGENTPROOF_STUDY_MOCK_FAULT="restart_inflight")
    started = time.monotonic()
    completed = subprocess.run(
        [
            "uv",
            "run",
            "--project",
            "engine",
            "--frozen",
            "python",
            "scripts/verify_mocked_study.py",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode:
        raise SystemExit(completed.stderr[-2000:] or completed.stdout[-2000:])
    result = json.loads(completed.stdout.splitlines()[-1])
    assert result["fault"] == "restart_inflight"
    assert result["effect_trials"] <= 8
    assert result["unknown_request_count"] >= 1
    assert result["fail"] == 0 and result["indeterminate"] >= 1
    print(
        json.dumps(
            {
                "worker_restart_recovery_seconds": round(time.monotonic() - started, 3),
                "study_id": result["study_id"],
                "effect_trials": result["effect_trials"],
                "unknown_request_count": result["unknown_request_count"],
                "supported": "worker restart uses durable PostgreSQL and Temporal state",
                "unsupported": "restoring a copied active Temporal SQLite history or collector queue into a new stack",
                "data_loss_boundary": "an uncertain provider request remains reserved and terminalizes INDETERMINATE; no exactly-once provider claim",
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
