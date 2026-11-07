import os
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[2]


def connect(dsn=None):
    return psycopg.connect(
        dsn or os.environ["DATABASE_URL"],
        autocommit=True,
        connect_timeout=5,
        options="-c statement_timeout=15000",
    )


def migrate(dsn):
    with connect(dsn) as conn, conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(810011)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations(version text PRIMARY KEY)"
        )
        for path in sorted((ROOT / "db/migrations").glob("*.sql")):
            version = "migrations/" + path.name
            if not conn.execute(
                "SELECT 1 FROM schema_migrations WHERE version=%s", (version,)
            ).fetchone():
                conn.execute(path.read_text())
                conn.execute("INSERT INTO schema_migrations VALUES(%s)", (version,))
