import uuid

import psycopg
from agentproof.database import migrate
from conftest import ROOT, manifest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb


def test_legacy_data_upgrade_is_lossless(dsn):
    database = "agentproof_migration_" + uuid.uuid4().hex
    target = make_conninfo(dsn, dbname=database)
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
        try:
            saved = []
            with psycopg.connect(target, autocommit=True) as conn:
                conn.execute((ROOT / "db/migrations/001_initial.sql").read_text())
                for agent, count in [("safe", 1), ("duplicate", 2)]:
                    m = manifest(agent)
                    result = {
                        "status": "passed" if agent == "safe" else "failed",
                        "side_effect_count": count,
                    }
                    conn.execute(
                        "INSERT INTO runs(id,manifest,status,result) VALUES(%s,%s,'completed',%s)",
                        (m.run_id, Jsonb(m.model_dump()), Jsonb(result)),
                    )
                    conn.execute(
                        "INSERT INTO orders VALUES(%s,'order-001','cancelled')",
                        (m.run_id,),
                    )
                    for sequence in (1, 2):
                        committed = sequence <= count
                        conn.execute(
                            "INSERT INTO tool_events VALUES(%s,%s,%s)",
                            (
                                m.run_id,
                                sequence,
                                Jsonb({"side_effect_committed": committed}),
                            ),
                        )
                        if committed:
                            conn.execute(
                                "INSERT INTO side_effects VALUES(%s,%s,'cancellation')",
                                (m.run_id, sequence),
                            )
                    saved.append((m.run_id, m.model_dump(), result, count))
            migrate(target)
            migrate(target)
            with psycopg.connect(target) as conn:
                for run_id, m, result, count in saved:
                    row = conn.execute(
                        "SELECT manifest,result FROM runs WHERE id=%s", (run_id,)
                    ).fetchone()
                    assert row == (m, result)
                    assert conn.execute(
                        "SELECT operation_key FROM side_effects WHERE run_id=%s ORDER BY ordinal",
                        (run_id,),
                    ).fetchall() == [
                        (run_id + f":reference-v1:tool:{n}",)
                        for n in range(1, count + 1)
                    ]
                assert {
                    r[0] for r in conn.execute("SELECT version FROM schema_migrations")
                } == {
                    "migrations/" + path.name
                    for path in (ROOT / "db/migrations").glob("*.sql")
                }
        finally:
            admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(database)))
