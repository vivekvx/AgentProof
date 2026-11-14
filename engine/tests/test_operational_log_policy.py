"""Tests for non-destructive operational log review."""

import importlib.util
import os
from datetime import UTC, datetime
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2] / "scripts/operational_log_policy.py"
SPEC = importlib.util.spec_from_file_location("operational_log_policy", SOURCE)
assert SPEC and SPEC.loader
logs = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(logs)


def test_age_missing_and_investigation_boundaries(tmp_path):
    now = datetime(2026, 11, 13, tzinfo=UTC)
    old, recent, protected = (
        tmp_path / "old.log",
        tmp_path / "recent.log",
        tmp_path / "incident.investigation",
    )
    for path in (old, recent, protected):
        path.write_text("synthetic")
    os.utime(old, (now.timestamp() - 7 * 86400 - 1, now.timestamp() - 7 * 86400 - 1))
    os.utime(recent, (now.timestamp() - 7 * 86400, now.timestamp() - 7 * 86400))
    os.utime(protected, (now.timestamp() - 365 * 86400, now.timestamp() - 365 * 86400))
    result = logs.classify([old, recent, protected, tmp_path / "missing.log"], now)
    assert result == {
        "missing": [str(tmp_path / "missing.log")],
        "protected": [str(protected)],
        "within_period": [str(recent)],
        "review_candidates": [str(old)],
    }
    assert old.read_text() == "synthetic"  # dry run does not delete


def test_docker_rotation_is_bounded_but_not_time_guaranteed(tmp_path):
    compose = tmp_path / "compose.yaml"
    compose.write_text(
        "logging:\n  driver: local\n  options:\n    max-size: 10m\n    max-file: '7'\n"
    )
    result = logs.docker_policy(compose)
    assert result["size_rotation_configured"] is True
    assert result["time_retention_enforced"] is False
    assert result["seven_day_availability_guaranteed"] is False
