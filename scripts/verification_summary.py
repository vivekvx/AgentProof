"""Write a compact CI summary from the required verification log."""

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path


def main():
    if len(sys.argv) != 2:
        raise SystemExit("usage: verification_summary.py VERIFY_LOG")
    log_path = Path(sys.argv[1])
    log = log_path.read_text()
    python_result = re.search(
        r"(?m)^(\d+) passed(?:, (\d+) skipped)?(?:, (\d+) failed)? in ([^\n]+)$",
        log,
    )
    browser_result = re.search(r"(?m)^(\d+) passed \(([^\n]+)\)$", log)
    node_result = {
        name: re.search(rf"(?m)^ℹ {name} (\d+)$", log)
        for name in ("tests", "pass", "fail", "skipped")
    }
    if not python_result or not browser_result or not all(node_result.values()):
        raise SystemExit(
            "verification log is missing pytest, Playwright, or Node test totals"
        )

    commit = os.getenv("CI_COMMIT_SHA")
    tree = os.getenv("CI_COMMIT_TREE")
    repository = os.getenv("CI_REPOSITORY")
    run_id = os.getenv("CI_RUN_ID")
    try:
        checked_out_commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
        checked_out_tree = subprocess.check_output(
            ["git", "rev-parse", "HEAD^{tree}"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        checked_out_commit = checked_out_tree = None
    if not commit:
        if checked_out_commit:
            dirty = subprocess.check_output(
                ["git", "status", "--porcelain"],
                text=True,
                stderr=subprocess.DEVNULL,
            )
            if dirty:
                raise SystemExit(
                    "local evidence requires a clean committed source tree"
                )
        commit = checked_out_commit
    if not tree:
        tree = checked_out_tree
    if not re.fullmatch(r"[0-9a-f]{40}", commit or ""):
        raise SystemExit("CI_COMMIT_SHA must be a full commit hash")
    if not re.fullmatch(r"[0-9a-f]{40}", tree or ""):
        raise SystemExit("CI_COMMIT_TREE must be a full Git tree hash")
    if checked_out_commit and commit != checked_out_commit:
        raise SystemExit("CI_COMMIT_SHA does not match the checked-out commit")
    if checked_out_tree and tree != checked_out_tree:
        raise SystemExit("CI_COMMIT_TREE does not match the checked-out tree")

    def version(command):
        try:
            return subprocess.check_output(
                command, text=True, stderr=subprocess.STDOUT
            ).strip()
        except (OSError, subprocess.CalledProcessError) as exc:
            raise SystemExit(
                f"required tool version is unavailable: {command[0]}"
            ) from exc

    lock_paths = (Path("go.sum"), Path("package-lock.json"), Path("engine/uv.lock"))
    lock_hashes = {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in lock_paths
    }

    go_packages = []
    for line in log.splitlines():
        match = re.match(r"ok\s+([^\s]+)\s+([^\s]+)", line)
        if match:
            go_packages.append({"package": match.group(1), "status": "passed"})
        elif re.match(r"\?\s+[^\s]+\s+\[no test files\]", line):
            continue
    summary = {
        "commit": commit,
        "tree": tree,
        "workflow_run_id": run_id,
        "workflow_run_attempt": os.getenv("CI_RUN_ATTEMPT"),
        "workflow_url": (
            f"https://github.com/{repository}/actions/runs/{run_id}"
            if repository
            and run_id
            and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
            else None
        ),
        "verification": "passed",
        "toolchains": {
            "go": version(["go", "version"]),
            "node": version(["node", "--version"]),
            "python": version([sys.executable, "--version"]),
            "uv": version(["uv", "--version"]),
        },
        "dependency_lock_sha256": lock_hashes,
        "python": {
            "passed": int(python_result.group(1)),
            "skipped": int(python_result.group(2) or 0),
            "failed": int(python_result.group(3) or 0),
        },
        "go_test_packages_passed": go_packages,
        "playwright": {"passed": int(browser_result.group(1))},
        "node_proxy_tests": {
            name: int(match.group(1)) for name, match in node_result.items()
        },
        "builds": ["Go API", "React/Vite web application"],
        "static_checks": [
            "ruff check",
            "ruff format --check",
            "contract schema drift",
            "go vet ./...",
        ],
        "log": log_path.name,
    }
    if (
        summary["python"]["skipped"]
        or summary["python"]["failed"]
        or summary["node_proxy_tests"]["fail"]
        or summary["node_proxy_tests"]["skipped"]
        or summary["node_proxy_tests"]["pass"] != summary["node_proxy_tests"]["tests"]
    ):
        raise SystemExit("verification has skipped or failed Python/Node tests")
    output = log_path.with_name("verification-summary.json")
    output.write_text(json.dumps(summary, indent=2) + "\n")
    print(output)


if __name__ == "__main__":
    main()
