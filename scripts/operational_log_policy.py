"""Inspect bounded local logs without deleting anything.

Docker's local driver bounds bytes but cannot promise seven-day availability.
This dry-run identifies age candidates and preserves investigation-marked files.
"""

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

RETENTION = timedelta(days=7)


def classify(paths, now, protected_suffix=".investigation"):
    if now.tzinfo is None:
        raise ValueError("now must include a timezone")
    result = {
        "missing": [],
        "protected": [],
        "within_period": [],
        "review_candidates": [],
    }
    for raw in paths:
        path = Path(raw)
        if not path.exists():
            result["missing"].append(str(path))
            continue
        if not path.is_file():
            raise ValueError("log path must be a file")
        modified = datetime.fromtimestamp(path.stat().st_mtime, UTC)
        if path.name.endswith(protected_suffix):
            result["protected"].append(str(path))
        elif now - modified > RETENTION:
            result["review_candidates"].append(str(path))
        else:
            result["within_period"].append(str(path))
    return {key: sorted(value) for key, value in result.items()}


def docker_policy(compose_file):
    text = Path(compose_file).read_text()
    has_driver = "driver: local" in text
    has_size = "max-size:" in text and "max-file:" in text
    return {
        "size_rotation_configured": has_driver and has_size,
        "time_retention_enforced": False,
        "seven_day_availability_guaranteed": False,
        "reason": "Docker local-driver rotation is size-based; old logs can rotate before seven days.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compose-file", type=Path, default=Path("compose.yaml"))
    parser.add_argument("--log", type=Path, action="append", default=[])
    parser.add_argument("--at", help="UTC instant for a deterministic dry run")
    args = parser.parse_args()
    now = datetime.fromisoformat(args.at) if args.at else datetime.now(UTC)
    if now.tzinfo is None:
        parser.error("--at must include a timezone")
    print(
        json.dumps(
            {
                "docker": docker_policy(args.compose_file),
                "retention": classify(args.log, now.astimezone(UTC)),
                "deletion": "not attempted",
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
