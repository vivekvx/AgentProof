"""Local single-tenant operational probes and actionable alert decisions."""

import argparse
import json
import os
import socket
import subprocess
import urllib.error
import urllib.request
from datetime import UTC, datetime

import psycopg
from agentproof.benchmark.evidence import verify_stored
from agentproof.provenance import verify_binding

QUEUE_WAIT_ALERT_SECONDS = 10  # More than four times the measured T16 p95.
STUDY_STALL_SECONDS = 180  # The committed study wall ceiling.
DB_ENVELOPE_CONNECTIONS = 7  # T16 sampled peak; this is not database capacity.
PROVIDER_ERROR_WINDOW_MINUTES = 5
PROVIDER_ERROR_ALERT_COUNT = 2

ALERTS = {
    "api_unavailable": ("critical", "Restart or inspect the API and its health route."),
    "worker_unavailable": (
        "critical",
        "Restart the worker and inspect its Temporal queue.",
    ),
    "temporal_unavailable": (
        "critical",
        "Restore Temporal before accepting workflow recovery.",
    ),
    "postgres_unavailable": (
        "critical",
        "Restore PostgreSQL; do not replay uncertain effects.",
    ),
    "telemetry_degraded": (
        "warning",
        "Restore Jaeger/OTLP and retain database evidence.",
    ),
    "trace_export_dropped": (
        "critical",
        "Inspect the collector queue and disk; database evidence remains authoritative.",
    ),
    "study_stalled": (
        "critical",
        "Inspect pending trials and Temporal history before intervention.",
    ),
    "queue_wait_exceeded": (
        "warning",
        "Inspect worker pollers and backlog; stop new admission.",
    ),
    "db_envelope_exceeded": ("warning", "Inspect connections and stop new admission."),
    "budget_exhausted": (
        "warning",
        "Stop dispatch; inspect reserved and unknown usage.",
    ),
    "provider_errors_repeated": (
        "warning",
        "Inspect provider failure class and shared cooldown.",
    ),
    "evidence_verification_failed": (
        "critical",
        "Preserve records; inspect witness and provenance.",
    ),
    "probe_failed": ("critical", "Inspect the operational check and database schema."),
    "backup_failed": (
        "critical",
        "Create and verify a fresh backup before further changes.",
    ),
    "restoration_failed": (
        "critical",
        "Keep the copy isolated and investigate restore output.",
    ),
}


def evaluate(signals):
    """Map measured or injected signals to stable, identifier-free alert codes."""
    active = {
        "api_unavailable": not signals["api_healthy"],
        "worker_unavailable": not signals["worker_healthy"],
        "temporal_unavailable": not signals["temporal_healthy"],
        "postgres_unavailable": not signals["postgres_healthy"],
        "telemetry_degraded": not signals["telemetry_healthy"]
        or not signals.get("collector_healthy", True),
        "trace_export_dropped": signals.get("telemetry_drops_recent", False),
        "study_stalled": signals["stalled_studies"] > 0,
        "queue_wait_exceeded": signals["oldest_queue_seconds"]
        > QUEUE_WAIT_ALERT_SECONDS,
        "db_envelope_exceeded": signals["db_connections"] > DB_ENVELOPE_CONNECTIONS,
        "budget_exhausted": signals["budget_exhausted"] > 0,
        "provider_errors_repeated": signals["provider_errors_recent"]
        >= PROVIDER_ERROR_ALERT_COUNT,
        "evidence_verification_failed": signals["evidence_failures"] > 0,
        "probe_failed": signals["probe_failed"],
        "backup_failed": signals["backup_exit"] != 0,
        "restoration_failed": signals["restore_exit"] != 0,
    }
    return [
        {"code": code, "severity": ALERTS[code][0], "action": ALERTS[code][1]}
        for code, triggered in active.items()
        if triggered
    ]


def http_ok(url, token=None):
    headers = {"Authorization": "Bearer " + token} if token else {}
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, headers=headers), timeout=2
        ) as response:
            return response.status == 200
    except (OSError, urllib.error.HTTPError):
        return False


def tcp_ok(host, port):
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


def container_running(name):
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Running}}", name],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def collector_drops_recent(name):
    result = subprocess.run(
        ["docker", "logs", "--since", "1m", name],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        return False
    logs = result.stdout + result.stderr
    return any(
        marker in logs
        for marker in (
            "sending queue is full",
            "no space left on device",
            "Exporting failed. Rejecting data.",
        )
    )


def probe(args):
    temporal_host, temporal_port = args.temporal_address.rsplit(":", 1)
    signals = {
        "api_healthy": http_ok(
            args.api_url + "/healthz", os.getenv("AGENTPROOF_API_TOKEN")
        ),
        "worker_healthy": container_running(args.worker_container),
        "temporal_healthy": tcp_ok(temporal_host, int(temporal_port)),
        "postgres_healthy": False,
        "telemetry_healthy": http_ok(args.jaeger_url + "/api/services"),
        "collector_healthy": container_running(args.collector_container),
        "telemetry_drops_recent": collector_drops_recent(args.collector_container),
        "stalled_studies": 0,
        "oldest_queue_seconds": 0,
        "db_connections": 0,
        "budget_exhausted": 0,
        "provider_errors_recent": 0,
        "evidence_failures": 0,
        "probe_failed": False,
        "backup_exit": args.backup_exit,
        "restore_exit": args.restore_exit,
    }
    try:
        with psycopg.connect(args.database_url, connect_timeout=2) as conn:
            signals["postgres_healthy"] = True
            signals["stalled_studies"] = conn.execute(
                """SELECT count(*) FROM benchmark_studies s
                   WHERE s.status='running'
                   AND s.created_at < clock_timestamp() - (%s * interval '1 second')
                   AND EXISTS (SELECT 1 FROM benchmark_study_evaluations b
                       JOIN benchmark_trials t ON t.evaluation_id=b.evaluation_id
                       WHERE b.study_id=s.id AND t.result IS NULL)""",
                (STUDY_STALL_SECONDS,),
            ).fetchone()[0]
            signals["oldest_queue_seconds"] = float(
                conn.execute(
                    "SELECT COALESCE(max(extract(epoch FROM clock_timestamp()-created_at)),0) "
                    "FROM benchmark_evaluations WHERE status='queued'"
                ).fetchone()[0]
            )
            signals["db_connections"] = conn.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database()"
            ).fetchone()[0]
            # A committed execution-limit issue is observable without treating a full
            # up-front reservation as exhaustion.
            signals["budget_exhausted"] = conn.execute(
                """SELECT count(*) FROM benchmark_trials t
                   JOIN benchmark_study_trials b ON b.trial_id=t.id
                   WHERE t.completed_at > clock_timestamp()-(%s * interval '1 minute')
                   AND t.result->'assessment'->'issues' ? 'execution_limit'""",
                (PROVIDER_ERROR_WINDOW_MINUTES,),
            ).fetchone()[0]
            signals["provider_errors_recent"] = conn.execute(
                """SELECT count(*) FROM benchmark_trials
                   WHERE completed_at > clock_timestamp()-(%s * interval '1 minute')
                   AND result->>'failure_category'='provider'""",
                (PROVIDER_ERROR_WINDOW_MINUTES,),
            ).fetchone()[0]
            recent = conn.execute(
                """SELECT id FROM benchmark_trials
                   WHERE completed_at > clock_timestamp()-(%s * interval '1 minute')
                   AND result IS NOT NULL ORDER BY completed_at DESC LIMIT 100""",
                (PROVIDER_ERROR_WINDOW_MINUTES,),
            ).fetchall()
            for (trial_id,) in recent:
                try:
                    witness = verify_stored(conn, trial_id)
                    binding = verify_binding(conn, str(trial_id), "benchmark")
                except (ValueError, KeyError):
                    signals["evidence_failures"] += 1
                    continue
                if witness is False or binding is False:
                    signals["evidence_failures"] += 1
    except psycopg.OperationalError:
        signals["postgres_healthy"] = False
    except psycopg.Error:
        signals["probe_failed"] = True
    return signals


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--api-url", default="http://127.0.0.1:8081")
    parser.add_argument(
        "--worker-container", default="agentproof-verification-worker-1"
    )
    parser.add_argument("--temporal-address", default="127.0.0.1:7234")
    parser.add_argument("--jaeger-url", default="http://127.0.0.1:16687")
    parser.add_argument(
        "--collector-container", default="agentproof-verification-otel-collector-1"
    )
    parser.add_argument("--backup-exit", type=int, required=True)
    parser.add_argument("--restore-exit", type=int, required=True)
    args = parser.parse_args()
    signals = probe(args)
    alerts = evaluate(signals)
    print(
        json.dumps(
            {
                "checked_at": datetime.now(UTC).isoformat(),
                "signals": signals,
                "alerts": alerts,
            },
            sort_keys=True,
        )
    )
    raise SystemExit(1 if alerts else 0)


if __name__ == "__main__":
    main()
