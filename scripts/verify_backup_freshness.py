"""Read-only freshness check for an uninstalled local launchd backup schedule.

launchd calendar jobs do not guarantee catch-up after a host is asleep or shut
down. This check detects a missing or invalid current-period artifact once its
configured local calendar time and grace window have elapsed. It never creates,
uploads, removes, or decrypts an archive.
"""

import argparse
import importlib.util
import json
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SOURCE = Path(__file__).with_name("local_backup.py")
SPEC = importlib.util.spec_from_file_location("local_backup", SOURCE)
assert SPEC and SPEC.loader
backup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backup)

SCHEDULES = {
    "daily": time(hour=2, minute=15),
    "monthly": time(hour=3, minute=15),
}


def due_at(now, cadence):
    if cadence not in SCHEDULES:
        raise ValueError("invalid backup cadence")
    local = now.replace(
        hour=SCHEDULES[cadence].hour,
        minute=SCHEDULES[cadence].minute,
        second=0,
        microsecond=0,
    )
    if cadence == "monthly":
        local = local.replace(day=1)
    return local


def assess(config_path, cadence, now, grace):
    if not backup.private_path(config_path):
        raise ValueError(
            "backup configuration must be an existing private absolute file"
        )
    config, directory, _certificate = backup.load_config(config_path)
    expected = due_at(now, cadence)
    if now < expected:
        return {"status": "not_due", "cadence": cadence, "due_at": expected.isoformat()}
    stamp = backup.period(now.astimezone(UTC), cadence)
    archive = directory / f"agentproof-{cadence}-{stamp}.pgdump.cms"
    manifest = directory / f"agentproof-{cadence}-{stamp}.json"
    valid = backup.verify(archive, manifest)
    status = "fresh" if valid else "stale"
    return {
        "status": status,
        "cadence": cadence,
        "due_at": expected.isoformat(),
        "grace_seconds": int(grace.total_seconds()),
        "overdue": now >= expected + grace,
        "archive": str(archive),
        "manifest": str(manifest),
        "off_host_transfer": "not attempted",
        "schema_version": config["schema_version"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--cadence", choices=sorted(backup.CADENCES), required=True)
    parser.add_argument("--grace-seconds", type=int, default=3600)
    parser.add_argument(
        "--time-zone",
        help="IANA zone matching the host launchd calendar; defaults to the host local zone",
    )
    parser.add_argument(
        "--at", help="instant with a timezone for a deterministic check"
    )
    args = parser.parse_args()
    if args.grace_seconds < 0:
        parser.error("--grace-seconds must be non-negative")
    if args.time_zone:
        try:
            zone = ZoneInfo(args.time_zone)
        except ZoneInfoNotFoundError as error:
            parser.error(f"invalid --time-zone: {error}")
        now = datetime.fromisoformat(args.at) if args.at else datetime.now(zone)
        now = now.astimezone(zone)
    else:
        now = (
            datetime.fromisoformat(args.at) if args.at else datetime.now().astimezone()
        )
    if now.tzinfo is None:
        parser.error("--at must include a timezone")
    try:
        result = assess(
            args.config, args.cadence, now, timedelta(seconds=args.grace_seconds)
        )
    except (OSError, ValueError) as error:
        print(json.dumps({"status": "failed", "error": str(error)}, sort_keys=True))
        raise SystemExit(1) from error
    print(json.dumps(result, sort_keys=True))
    if result["status"] == "stale" and result["overdue"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
