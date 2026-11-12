"""Dry-run retention review classification; this module never deletes records."""

from calendar import monthrange
from datetime import timedelta

PROTECTED = {
    "published_dataset",
    "evaluation_result",
    "trial_result",
    "evidence",
    "provenance",
}
PROPOSED_EPHEMERAL_DAYS = {
    "application_log": 7,
    "trace": 7,
}


def twelve_months_after(start):
    year = start.year + 1
    day = min(start.day, monthrange(year, start.month)[1])
    return start.replace(year=year, day=day)


def dry_run(records, now):
    """Return records due for owner review, never a deletion instruction."""
    if now.tzinfo is None:
        raise ValueError("now must include a timezone")
    candidates = []
    for record in records:
        kind = record["kind"]
        created = record["created_at"]
        if created.tzinfo is None:
            raise ValueError("record time must include a timezone")
        if kind in PROTECTED:
            continue
        if kind in PROPOSED_EPHEMERAL_DAYS:
            due = created + timedelta(days=PROPOSED_EPHEMERAL_DAYS[kind])
        elif kind == "temporal_history":
            closed = record.get("closed_at")
            if closed is None:
                continue
            due = closed + timedelta(days=7)
        elif kind == "synthetic_database":
            reviewed = record.get("reviewed_at")
            if reviewed is None:
                continue
            due = reviewed + timedelta(days=30)
        elif kind == "temporary_export":
            reviewed = record.get("reviewed_at")
            if reviewed is None or not record.get("review_verified", False):
                continue
            due = reviewed + timedelta(hours=24)
        elif kind == "backup":
            cadence = record.get("cadence")
            if cadence == "daily":
                due = created + timedelta(days=30)
            elif cadence == "monthly":
                due = twelve_months_after(created)
            else:
                raise ValueError("backup cadence must be daily or monthly")
        else:
            raise ValueError("unknown retention category")
        if due.tzinfo is None:
            raise ValueError("retention boundary must include a timezone")
        if now > due:
            candidates.append(record["id"])
    return sorted(candidates)
