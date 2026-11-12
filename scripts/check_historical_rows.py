"""Capture and compare canonical row hashes without storing row contents."""

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

import psycopg


def canonical(row):
    return json.dumps(row, sort_keys=True, separators=(",", ":"), default=str)


def aggregate(rows):
    return hashlib.sha256(("\n".join(sorted(rows)) + "\n").encode()).hexdigest()


def snapshot(dsn):
    tables = {}
    with psycopg.connect(dsn) as conn:
        conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
        names = [
            row[0]
            for row in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
            )
        ]
        for name in names:
            rows = [
                canonical(row[0])
                for row in conn.execute(f"SELECT to_jsonb(x) FROM {name} x")
            ]
            hashes = Counter(hashlib.sha256(row.encode()).hexdigest() for row in rows)
            tables[name] = {
                "rows": len(rows),
                "sha256": aggregate(rows),
                "row_hashes": dict(hashes),
            }
    return tables


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("capture", "compare"))
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    args = parser.parse_args()
    if args.mode == "capture":
        if args.baseline.exists():
            parser.error("Baseline file already exists")
        tables = snapshot(args.database_url)
        args.baseline.write_text(json.dumps(tables, sort_keys=True))
        print(
            json.dumps(
                {
                    "tables": {
                        name: {"rows": data["rows"], "sha256": data["sha256"]}
                        for name, data in tables.items()
                    }
                },
                sort_keys=True,
            )
        )
        return
    before = json.loads(args.baseline.read_text())
    current = snapshot(args.database_url)
    result = {}
    preserved = True
    for name, original in before.items():
        after = current.get(name)
        old_hashes = Counter(original["row_hashes"])
        new_hashes = Counter(after["row_hashes"]) if after else Counter()
        missing = sum((old_hashes - new_hashes).values())
        preserved &= missing == 0
        result[name] = {
            "protected_rows": original["rows"],
            "new_rows": after["rows"] - original["rows"] if after else 0,
            "missing_or_changed": missing,
            "baseline_sha256": original["sha256"],
            "protected_rows_match": missing == 0,
        }
    print(json.dumps({"preserved": preserved, "tables": result}, sort_keys=True))
    raise SystemExit(0 if preserved else 1)


if __name__ == "__main__":
    main()
