"""Back up a dedicated synthetic study database and restore it in a new PostgreSQL container."""

import argparse
import hashlib
import json
import os
import re
import secrets
import socket
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

import psycopg
from agentproof.benchmark.evidence import verify_stored
from agentproof.benchmark.registry import reference
from agentproof.bundle import export_trial, replay, validate_bundle
from agentproof.database import migrate
from agentproof.provenance import verify_binding
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[1]
TABLES = (
    "benchmark_studies",
    "benchmark_study_budgets",
    "benchmark_study_reservations",
    "benchmark_study_requests",
    "benchmark_study_evaluations",
    "benchmark_study_trials",
    "benchmark_evaluations",
    "benchmark_trials",
    "benchmark_events",
    "benchmark_effects",
    "benchmark_receipts",
    "benchmark_evidence_heads",
    "benchmark_execution_provenance",
    "benchmark_evaluation_provenance",
    "benchmark_external_sessions",
    "benchmark_datasets",
    "execution_source_snapshots",
    "schema_migrations",
)
SECRET = re.compile(
    rb"(?i)(?:gsk_[A-Za-z0-9]{16,}|sk-[A-Za-z0-9]{16,}|Bearer\s+[A-Za-z0-9._-]{12,})"
)


def run(args, **kwargs):
    return subprocess.run(args, check=False, capture_output=True, **kwargs)


def fingerprint(dsn):
    output = {}
    with psycopg.connect(dsn) as conn:
        conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
        for table in TABLES:
            rows = sorted(
                json.dumps(row[0], sort_keys=True, separators=(",", ":"), default=str)
                for row in conn.execute(f"SELECT to_jsonb(x) FROM {table} x")
            )
            output[table] = {
                "rows": len(rows),
                "sha256": hashlib.sha256(("\n".join(rows) + "\n").encode()).hexdigest(),
            }
    return output


def prepare_synthetic_source(dsn):
    """Add missing fixtures only to a dedicated synthetic study database."""
    published = reference("benchmarks/support.v5.json")
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "INSERT INTO benchmark_datasets(id,version,sha256,content) "
            "VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (
                published["id"],
                published["version"],
                published["sha256"],
                Jsonb(published["content"]),
            ),
        )
        if not conn.execute(
            "SELECT 1 FROM benchmark_external_sessions LIMIT 1"
        ).fetchone():
            trial = conn.execute(
                "SELECT id,evaluation_id,scenario_id FROM benchmark_trials ORDER BY id LIMIT 1"
            ).fetchone()
            assert trial, "synthetic study has no trial for session fixture"
            conn.execute(
                "INSERT INTO benchmark_external_sessions"
                "(trial_id,evaluation_id,scenario_id,attempt,state,credential_hash,"
                "expires_at,outcome,completed_at) "
                "VALUES(%s,%s,%s,1,'completed',%s,clock_timestamp()+interval '1 day',"
                "%s,clock_timestamp())",
                (
                    *trial,
                    hashlib.sha256(b"synthetic-restoration-only").hexdigest(),
                    Jsonb({"resolution": "completed"}),
                ),
            )


def api_read(dsn, study_id, evaluation_id, trial_id):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    token = secrets.token_urlsafe(36)
    env = os.environ.copy()
    env.update(
        DATABASE_URL=dsn,
        AGENTPROOF_MIGRATIONS_EXTERNAL="1",
        AGENTPROOF_API_TOKEN=token,
        API_ADDRESS=f"127.0.0.1:{port}",
        TEMPORAL_ADDRESS="127.0.0.1:7234",
        AGENTPROOF_TASK_QUEUE="agentproof-t18-restore-" + secrets.token_hex(6),
    )
    env.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
    with tempfile.TemporaryDirectory(prefix="agentproof-t18-api-") as directory:
        binary = Path(directory) / "api"
        built = run(
            ["go", "build", "-o", str(binary), "./apps/api/cmd/server"], cwd=ROOT
        )
        if built.returncode:
            raise AssertionError(f"API build failed: {built.returncode}")
        process = subprocess.Popen(
            [str(binary)],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        try:
            for _ in range(100):
                try:
                    request = urllib.request.Request(
                        f"http://127.0.0.1:{port}/healthz",
                        headers={"Authorization": "Bearer " + token},
                    )
                    with urllib.request.urlopen(request, timeout=2) as response:
                        assert response.status == 200
                    break
                except (OSError, TimeoutError):
                    if process.poll() is not None:
                        raise AssertionError("restored API stopped before health check")
                    time.sleep(0.1)
            else:
                raise AssertionError("restored API did not become healthy")
            for path, field, identity in (
                (f"/api/v2/studies/{study_id}", "id", study_id),
                (f"/api/v2/evaluations/{evaluation_id}", "id", evaluation_id),
                (f"/api/v2/trials/{trial_id}", "trial_id", trial_id),
            ):
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}{path}",
                    headers={"Authorization": "Bearer " + token},
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    body = json.load(response)
                    assert response.status == 200 and body[field] == identity, (
                        path,
                        body,
                    )
        finally:
            process.terminate()
            try:
                process.communicate(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", required=True)
    parser.add_argument(
        "--source-container", default="agentproof-verification-postgres-1"
    )
    parser.add_argument(
        "--model-config-dir", default=".local/verification/mock-study-config"
    )
    parser.add_argument("--prepare-synthetic", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"agentproof_study_test_[a-z0-9]{1,24}", args.source_db):
        parser.error("Source must be a dedicated synthetic study database")
    os.environ["AGENTPROOF_MODEL_CONFIG_DIR"] = str(
        (ROOT / args.model_config_dir).resolve()
    )
    source = f"postgresql://agentproof:agentproof@127.0.0.1:55433/{args.source_db}"
    if args.prepare_synthetic:
        prepare_synthetic_source(source)
    before = fingerprint(source)
    required = {
        "benchmark_studies": 1,
        "benchmark_evaluations": 2,
        "benchmark_trials": 8,
        "benchmark_external_sessions": 1,
        "benchmark_datasets": 1,
    }
    for table, minimum in required.items():
        assert before[table]["rows"] >= minimum, f"missing representative {table}"
    container = "agentproof-t18-" + secrets.token_hex(6)
    started = run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            container,
            "-e",
            "POSTGRES_USER=agentproof",
            "-e",
            "POSTGRES_PASSWORD=agentproof",
            "-e",
            "POSTGRES_DB=restored",
            "-p",
            "127.0.0.1::5432",
            "postgres:16.14-alpine",
            "-c",
            "max_connections=20",
            "-c",
            "superuser_reserved_connections=1",
        ]
    )
    assert started.returncode == 0, started.stderr.decode(errors="replace")
    try:
        for _ in range(100):
            ready = run(["docker", "exec", container, "pg_isready", "-U", "agentproof"])
            if ready.returncode == 0:
                break
            time.sleep(0.1)
        else:
            raise AssertionError("isolated PostgreSQL did not become ready")
        port_result = run(["docker", "port", container, "5432/tcp"])
        assert port_result.returncode == 0
        port = int(port_result.stdout.decode().strip().rsplit(":", 1)[1])
        restored = f"postgresql://agentproof:agentproof@127.0.0.1:{port}/restored"
        runtime = f"postgresql://agentproof_runtime:agentproof_runtime_local_only@127.0.0.1:{port}/restored"
        with tempfile.TemporaryDirectory(prefix="agentproof-t18-restore-") as directory:
            backup = Path(directory) / "synthetic.dump"
            with backup.open("wb") as output:
                dumped = subprocess.run(
                    [
                        "docker",
                        "exec",
                        args.source_container,
                        "pg_dump",
                        "-U",
                        "agentproof",
                        "-d",
                        args.source_db,
                        "-Fc",
                        "--no-owner",
                        "--no-acl",
                    ],
                    stdout=output,
                    stderr=subprocess.PIPE,
                    check=False,
                )
            assert dumped.returncode == 0, dumped.stderr.decode(errors="replace")
            raw = backup.read_bytes()
            assert raw.startswith(b"PGDMP") and not SECRET.search(raw)
            digest = hashlib.sha256(raw).hexdigest()
            with backup.open("rb") as source_file:
                restored_process = subprocess.run(
                    [
                        "docker",
                        "exec",
                        "-i",
                        container,
                        "pg_restore",
                        "-U",
                        "agentproof",
                        "-d",
                        "restored",
                        "--no-owner",
                        "--no-acl",
                        "--exit-on-error",
                    ],
                    stdin=source_file,
                    capture_output=True,
                    check=False,
                )
            assert restored_process.returncode == 0, restored_process.stderr.decode(
                errors="replace"
            )
            role_sql = (ROOT / "db/migrations/017_local_runtime_role.sql").read_bytes()
            role = subprocess.run(
                [
                    "docker",
                    "exec",
                    "-i",
                    container,
                    "psql",
                    "-U",
                    "agentproof",
                    "-d",
                    "restored",
                    "-v",
                    "ON_ERROR_STOP=1",
                ],
                input=role_sql,
                capture_output=True,
                check=False,
            )
            assert role.returncode == 0, role.stderr.decode(errors="replace")
            migrate(restored)
            after = fingerprint(restored)
            assert after == before, "canonical restored rows differ from backup source"
            with psycopg.connect(restored) as conn:
                migration_rows = conn.execute(
                    "SELECT count(*) FROM schema_migrations"
                ).fetchone()[0]
                assert migration_rows == before["schema_migrations"]["rows"]
                study_id = str(
                    conn.execute("SELECT id FROM benchmark_studies").fetchone()[0]
                )
                evaluation_id = str(
                    conn.execute(
                        "SELECT id FROM benchmark_evaluations ORDER BY id LIMIT 1"
                    ).fetchone()[0]
                )
                trial_ids = [
                    str(row[0])
                    for row in conn.execute(
                        "SELECT id FROM benchmark_trials ORDER BY id"
                    )
                ]
                for trial_id in trial_ids:
                    assert verify_stored(conn, trial_id) is True
                    assert verify_binding(conn, trial_id, "benchmark") is True
                assert verify_binding(conn, evaluation_id, "evaluation") is True
                bundle = export_trial(conn, trial_ids[0])
                validate_bundle(bundle, require_installed=True)
                assert replay(bundle)["verdict"] == "PASS"
                dataset = conn.execute(
                    "SELECT id,version,sha256 FROM benchmark_datasets"
                ).fetchone()
                assert dataset == (
                    "customer-support",
                    5,
                    "59f6e65240fe035c0c7e4ee629f3d2d8082471cdeb3201c4e8d119724a7f654b",
                )
            with psycopg.connect(runtime) as conn:
                permissions = conn.execute(
                    "SELECT has_table_privilege(current_user,'benchmark_trials','SELECT,INSERT,UPDATE,DELETE'),"
                    "has_schema_privilege(current_user,'public','CREATE'),"
                    "has_table_privilege(current_user,'schema_migrations','SELECT')"
                ).fetchone()
                assert permissions == (True, False, False), permissions
                assert conn.execute(
                    "SELECT count(*) FROM benchmark_trials"
                ).fetchone() == (8,)
            api_read(runtime, study_id, evaluation_id, trial_ids[0])
            held = []
            saturated = False
            try:
                for _ in range(25):
                    try:
                        held.append(psycopg.connect(runtime, connect_timeout=2))
                    except psycopg.OperationalError:
                        saturated = True
                        break
                assert saturated, (
                    "isolated database did not reach its connection ceiling"
                )
            finally:
                for connection in held:
                    connection.close()
            with psycopg.connect(runtime, connect_timeout=2) as recovered:
                assert recovered.execute("SELECT 1").fetchone() == (1,)
            # A damaged archive is restored only into a disposable database.
            created_bad = run(
                ["docker", "exec", container, "createdb", "-U", "agentproof", "damaged"]
            )
            assert created_bad.returncode == 0
            with tempfile.TemporaryFile() as bad:
                bad.write(raw[: max(100, len(raw) // 3)])
                bad.seek(0)
                corrupted = subprocess.run(
                    [
                        "docker",
                        "exec",
                        "-i",
                        container,
                        "pg_restore",
                        "-U",
                        "agentproof",
                        "-d",
                        "damaged",
                        "--exit-on-error",
                    ],
                    stdin=bad,
                    capture_output=True,
                    check=False,
                )
            assert corrupted.returncode != 0, "truncated backup unexpectedly restored"
            dropped_bad = run(
                ["docker", "exec", container, "dropdb", "-U", "agentproof", "damaged"]
            )
            assert dropped_bad.returncode == 0
            print(
                json.dumps(
                    {
                        "backup_sha256": digest,
                        "backup_bytes": len(raw),
                        "dump_exit": dumped.returncode,
                        "restore_exit": restored_process.returncode,
                        "role_bootstrap_exit": role.returncode,
                        "migration_compatibility_exit": 0,
                        "saturation_rejected_connection": saturated,
                        "saturation_held_connections": len(held),
                        "connection_recovery_exit": 0,
                        "api_read_exit": 0,
                        "corrupted_restore_exit": corrupted.returncode,
                        "counts": {
                            table: value["rows"] for table, value in before.items()
                        },
                        "fingerprints_match": True,
                        "evidence_verified": len(trial_ids),
                        "offline_replay": "PASS",
                    },
                    sort_keys=True,
                )
            )
    finally:
        run(["docker", "rm", "-f", "-v", container])


if __name__ == "__main__":
    main()
