import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SUMMARY_SCRIPT = ROOT / "scripts" / "verification_summary.py"


def _run_summary(
    tmp_path: Path, pytest_totals: str
) -> subprocess.CompletedProcess[str]:
    log = tmp_path / "verify.log"
    log.write_text(
        f"{pytest_totals}\n"
        "ok\tgithub.com/vivekvx/AgentProof/apps/api/internal/server\t0.5s\n"
        "  7 passed (2.7m)\n"
        "ℹ tests 1\n"
        "ℹ pass 1\n"
        "ℹ fail 0\n"
        "ℹ skipped 0\n"
    )
    for relative_path in ("go.sum", "package-lock.json", "engine/uv.lock"):
        target = tmp_path / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative_path, target)
    env = os.environ.copy()
    env.update(
        {
            "CI_COMMIT_SHA": "a" * 40,
            "CI_COMMIT_TREE": "b" * 40,
            "CI_REPOSITORY": "vivekvx/AgentProof",
            "CI_RUN_ID": "local-summary-test",
            "CI_RUN_ATTEMPT": "1",
        }
    )
    return subprocess.run(
        [sys.executable, str(SUMMARY_SCRIPT), str(log)],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_summary_records_pytest_warnings(tmp_path: Path) -> None:
    result = _run_summary(tmp_path, "712 passed, 1 warning in 237.77s (0:03:57)")

    assert result.returncode == 0, result.stderr
    summary = (tmp_path / "verification-summary.json").read_text()
    assert '"passed": 712' in summary
    assert '"skipped": 0' in summary
    assert '"failed": 0' in summary
    assert '"warnings": 1' in summary


def test_summary_rejects_skipped_tests_even_with_warnings(tmp_path: Path) -> None:
    result = _run_summary(
        tmp_path, "712 passed, 1 skipped, 1 warning in 237.77s (0:03:57)"
    )

    assert result.returncode != 0
    assert "verification has skipped or failed Python/Node tests" in result.stderr
