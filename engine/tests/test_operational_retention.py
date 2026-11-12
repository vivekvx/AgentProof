"""Synthetic boundary checks for the proposal-only retention selector."""

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[2] / "scripts/retention_policy.py"
SPEC = importlib.util.spec_from_file_location("retention_policy", SOURCE)
assert SPEC and SPEC.loader
policy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(policy)


def test_authoritative_records_are_never_candidates():
    now = datetime(2026, 11, 12, tzinfo=UTC)
    old = now - timedelta(days=3650)
    records = [
        {"id": kind, "kind": kind, "created_at": old}
        for kind in sorted(policy.PROTECTED)
    ]
    assert policy.dry_run(records, now) == []


def test_ephemeral_boundaries_are_strict_and_only_dry_run():
    now = datetime(2026, 11, 12, tzinfo=UTC)
    records = []
    for kind, days in policy.PROPOSED_EPHEMERAL_DAYS.items():
        records.extend(
            [
                {
                    "id": kind + "-before",
                    "kind": kind,
                    "created_at": now - timedelta(days=days, seconds=1),
                },
                {
                    "id": kind + "-at",
                    "kind": kind,
                    "created_at": now - timedelta(days=days),
                },
                {
                    "id": kind + "-new",
                    "kind": kind,
                    "created_at": now - timedelta(days=days - 1),
                },
            ]
        )
    assert policy.dry_run(records, now) == sorted(
        kind + "-before" for kind in policy.PROPOSED_EPHEMERAL_DAYS
    )
    assert len(records) == 6  # The selector has not mutated the input.


def test_history_synthetic_backup_and_export_review_boundaries():
    now = datetime(2026, 11, 12, tzinfo=UTC)
    records = [
        {
            "id": "open-history",
            "kind": "temporal_history",
            "created_at": now - timedelta(days=365),
        },
        {
            "id": "closed-history",
            "kind": "temporal_history",
            "created_at": now - timedelta(days=9),
            "closed_at": now - timedelta(days=7, seconds=1),
        },
        {
            "id": "unreviewed-db",
            "kind": "synthetic_database",
            "created_at": now - timedelta(days=365),
        },
        {
            "id": "reviewed-db",
            "kind": "synthetic_database",
            "created_at": now - timedelta(days=100),
            "reviewed_at": now - timedelta(days=30, seconds=1),
        },
        {
            "id": "daily-backup",
            "kind": "backup",
            "cadence": "daily",
            "created_at": now - timedelta(days=30, seconds=1),
        },
        {
            "id": "monthly-backup",
            "kind": "backup",
            "cadence": "monthly",
            "created_at": datetime(2025, 11, 11, tzinfo=UTC),
        },
        {
            "id": "unverified-export",
            "kind": "temporary_export",
            "created_at": now - timedelta(days=10),
            "reviewed_at": now - timedelta(days=2),
            "review_verified": False,
        },
        {
            "id": "verified-export",
            "kind": "temporary_export",
            "created_at": now - timedelta(days=10),
            "reviewed_at": now - timedelta(days=1, seconds=1),
            "review_verified": True,
        },
        {
            "id": "exact-export",
            "kind": "temporary_export",
            "created_at": now - timedelta(days=10),
            "reviewed_at": now - timedelta(days=1),
            "review_verified": True,
        },
    ]
    assert policy.dry_run(records, now) == [
        "closed-history",
        "daily-backup",
        "monthly-backup",
        "reviewed-db",
        "verified-export",
    ]
    assert len(records) == 9


def test_retention_rejects_ambiguous_time_and_categories():
    now = datetime(2026, 11, 12, tzinfo=UTC)
    with pytest.raises(ValueError, match="timezone"):
        policy.dry_run([], now.replace(tzinfo=None))
    with pytest.raises(ValueError, match="unknown"):
        policy.dry_run([{"id": "x", "kind": "unclassified", "created_at": now}], now)
