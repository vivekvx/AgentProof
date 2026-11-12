"""Controlled threshold breaches for the local operator alert decisions."""

import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from agentproof.database import migrate
from psycopg.types.json import Jsonb

SOURCE = Path(__file__).resolve().parents[2] / "scripts/operational_checks.py"
SPEC = importlib.util.spec_from_file_location("operational_checks", SOURCE)
assert SPEC and SPEC.loader
ops = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ops)
DB_ENVELOPE_CONNECTIONS = ops.DB_ENVELOPE_CONNECTIONS
PROVIDER_ERROR_ALERT_COUNT = ops.PROVIDER_ERROR_ALERT_COUNT
QUEUE_WAIT_ALERT_SECONDS = ops.QUEUE_WAIT_ALERT_SECONDS
evaluate = ops.evaluate


@pytest.fixture
def isolated_dsn(dsn):
    name = "agentproof_t18_alert_" + uuid4().hex
    admin = dsn.rsplit("/", 1)[0] + "/postgres"
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute("CREATE DATABASE " + name)
    isolated = dsn.rsplit("/", 1)[0] + "/" + name
    try:
        migrate(isolated)
        yield isolated
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute("DROP DATABASE " + name + " WITH (FORCE)")


def healthy():
    return {
        "api_healthy": True,
        "worker_healthy": True,
        "temporal_healthy": True,
        "postgres_healthy": True,
        "telemetry_healthy": True,
        "collector_healthy": True,
        "telemetry_drops_recent": False,
        "stalled_studies": 0,
        "oldest_queue_seconds": 0,
        "db_connections": 1,
        "budget_exhausted": 0,
        "provider_errors_recent": 0,
        "evidence_failures": 0,
        "probe_failed": False,
        "backup_exit": 0,
        "restore_exit": 0,
    }


@pytest.mark.parametrize(
    ("code", "signal", "breach"),
    [
        ("api_unavailable", "api_healthy", False),
        ("worker_unavailable", "worker_healthy", False),
        ("temporal_unavailable", "temporal_healthy", False),
        ("postgres_unavailable", "postgres_healthy", False),
        ("telemetry_degraded", "telemetry_healthy", False),
        ("trace_export_dropped", "telemetry_drops_recent", True),
        ("study_stalled", "stalled_studies", 1),
        ("queue_wait_exceeded", "oldest_queue_seconds", QUEUE_WAIT_ALERT_SECONDS + 1),
        ("db_envelope_exceeded", "db_connections", DB_ENVELOPE_CONNECTIONS + 1),
        ("budget_exhausted", "budget_exhausted", 1),
        (
            "provider_errors_repeated",
            "provider_errors_recent",
            PROVIDER_ERROR_ALERT_COUNT,
        ),
        ("evidence_verification_failed", "evidence_failures", 1),
        ("probe_failed", "probe_failed", True),
        ("backup_failed", "backup_exit", 1),
        ("restoration_failed", "restore_exit", 1),
    ],
)
def test_alert_triggers_and_clears(code, signal, breach):
    baseline = healthy()
    assert evaluate(baseline) == []
    breached = {**baseline, signal: breach}
    alerts = evaluate(breached)
    assert [alert["code"] for alert in alerts] == [code]
    assert alerts[0]["action"] and alerts[0]["severity"]
    assert evaluate(baseline) == []


def test_alerts_do_not_trigger_at_measured_boundary():
    signals = healthy()
    signals["oldest_queue_seconds"] = QUEUE_WAIT_ALERT_SECONDS
    signals["db_connections"] = DB_ENVELOPE_CONNECTIONS
    signals["provider_errors_recent"] = PROVIDER_ERROR_ALERT_COUNT - 1
    assert evaluate(signals) == []


def test_committed_queue_and_study_stall_alerts_clear(isolated_dsn, monkeypatch):
    monkeypatch.setattr(ops, "http_ok", lambda *_: True)
    monkeypatch.setattr(ops, "tcp_ok", lambda *_: True)
    monkeypatch.setattr(ops, "container_running", lambda *_: True)
    monkeypatch.setattr(ops, "collector_drops_recent", lambda *_: False)
    monkeypatch.setattr(ops, "verify_stored", lambda *_: True)
    monkeypatch.setattr(ops, "verify_binding", lambda *_: True)
    study_id, evaluation_id, trial_id = str(uuid4()), str(uuid4()), str(uuid4())
    plan_id = "t18-alert-" + uuid4().hex
    args = SimpleNamespace(
        database_url=isolated_dsn,
        api_url="http://unused",
        worker_container="unused",
        collector_container="unused",
        temporal_address="127.0.0.1:1",
        jaeger_url="http://unused",
        backup_exit=0,
        restore_exit=0,
    )
    with psycopg.connect(isolated_dsn) as conn:
        conn.execute(
            "INSERT INTO benchmark_study_budgets(study_id,limits) VALUES(%s,'{}')",
            (study_id,),
        )
        conn.execute(
            "INSERT INTO benchmark_studies(id,plan_id,plan_sha256,plan,created_at) "
            "VALUES(%s,%s,%s,%s,clock_timestamp()-interval '181 seconds')",
            (
                study_id,
                plan_id,
                hashlib.sha256(plan_id.encode()).hexdigest(),
                json.dumps({"id": plan_id}),
            ),
        )
        conn.execute(
            "INSERT INTO benchmark_evaluations(id,created_at,manifest) "
            "VALUES(%s,clock_timestamp()-interval '11 seconds','{}')",
            (evaluation_id,),
        )
        conn.execute(
            "INSERT INTO benchmark_study_evaluations(study_id,evaluation_id,profile_id) VALUES(%s,%s,'synthetic')",
            (study_id, evaluation_id),
        )
        conn.execute(
            "INSERT INTO benchmark_trials(id,evaluation_id,scenario_id,trial_index,scenario,state) "
            "VALUES(%s,%s,'cs-001',1,'{}','{}')",
            (trial_id, evaluation_id),
        )
    try:
        signals = ops.probe(args)
        codes = {alert["code"] for alert in evaluate(signals)}
        assert {"study_stalled", "queue_wait_exceeded"} <= codes
        with psycopg.connect(isolated_dsn) as conn:
            conn.execute(
                "UPDATE benchmark_studies SET status='cancelled' WHERE id=%s",
                (study_id,),
            )
            conn.execute(
                "UPDATE benchmark_evaluations SET status='running' WHERE id=%s",
                (evaluation_id,),
            )
        cleared = {alert["code"] for alert in evaluate(ops.probe(args))}
        assert "study_stalled" not in cleared
        assert "queue_wait_exceeded" not in cleared
    finally:
        with psycopg.connect(isolated_dsn) as conn:
            conn.execute("DELETE FROM benchmark_trials WHERE id=%s", (trial_id,))
            conn.execute(
                "DELETE FROM benchmark_study_evaluations WHERE study_id=%s", (study_id,)
            )
            conn.execute(
                "DELETE FROM benchmark_evaluations WHERE id=%s", (evaluation_id,)
            )
            conn.execute("DELETE FROM benchmark_studies WHERE id=%s", (study_id,))
            conn.execute(
                "DELETE FROM benchmark_study_budgets WHERE study_id=%s", (study_id,)
            )


def test_committed_budget_provider_and_evidence_alerts_clear(isolated_dsn, monkeypatch):
    monkeypatch.setattr(ops, "http_ok", lambda *_: True)
    monkeypatch.setattr(ops, "tcp_ok", lambda *_: True)
    monkeypatch.setattr(ops, "container_running", lambda *_: True)
    monkeypatch.setattr(ops, "collector_drops_recent", lambda *_: False)
    args = SimpleNamespace(
        database_url=isolated_dsn,
        api_url="http://unused",
        worker_container="unused",
        collector_container="unused",
        temporal_address="127.0.0.1:1",
        jaeger_url="http://unused",
        backup_exit=0,
        restore_exit=0,
    )
    study_id, evaluation_id = str(uuid4()), str(uuid4())
    trials = [str(uuid4()), str(uuid4())]
    plan_id = "t18-provider-" + uuid4().hex
    with psycopg.connect(isolated_dsn) as conn:
        conn.execute(
            "INSERT INTO benchmark_study_budgets(study_id,limits) VALUES(%s,'{}')",
            (study_id,),
        )
        conn.execute(
            "INSERT INTO benchmark_studies(id,plan_id,plan_sha256,plan) VALUES(%s,%s,%s,%s)",
            (
                study_id,
                plan_id,
                hashlib.sha256(plan_id.encode()).hexdigest(),
                Jsonb({"id": plan_id}),
            ),
        )
        conn.execute(
            "INSERT INTO benchmark_evaluations(id,manifest) VALUES(%s,'{}')",
            (evaluation_id,),
        )
        for index, trial_id in enumerate(trials, 1):
            result = {
                "schema_version": "2.0",
                "verdict": "INDETERMINATE",
                "failure_category": "provider",
                "assessment": {"issues": ["execution_limit"] if index == 1 else []},
            }
            if index == 1:
                result["provenance"] = {"invalid": True}  # Isolated damaged witness.
            conn.execute(
                "INSERT INTO benchmark_trials"
                "(id,evaluation_id,scenario_id,trial_index,status,scenario,state,result,completed_at)"
                " VALUES(%s,%s,'cs-001',%s,'completed','{}','{}',%s,clock_timestamp())",
                (trial_id, evaluation_id, index, Jsonb(result)),
            )
        conn.execute(
            "INSERT INTO benchmark_study_reservations(study_id,slot_id,state,reserved)"
            " VALUES(%s,'slot-1','reserved','{}')",
            (study_id,),
        )
        conn.execute(
            "INSERT INTO benchmark_study_trials(trial_id,study_id,slot_id) VALUES(%s,%s,'slot-1')",
            (trials[0], study_id),
        )
    codes = {alert["code"] for alert in evaluate(ops.probe(args))}
    assert {
        "budget_exhausted",
        "provider_errors_repeated",
        "evidence_verification_failed",
    } <= codes
    with psycopg.connect(isolated_dsn) as conn:
        conn.execute(
            "DELETE FROM benchmark_study_trials WHERE study_id=%s", (study_id,)
        )
        conn.execute(
            "DELETE FROM benchmark_study_reservations WHERE study_id=%s", (study_id,)
        )
        conn.execute(
            "DELETE FROM benchmark_trials WHERE evaluation_id=%s", (evaluation_id,)
        )
        conn.execute("DELETE FROM benchmark_evaluations WHERE id=%s", (evaluation_id,))
        conn.execute("DELETE FROM benchmark_studies WHERE id=%s", (study_id,))
        conn.execute(
            "DELETE FROM benchmark_study_budgets WHERE study_id=%s", (study_id,)
        )
    cleared = {alert["code"] for alert in evaluate(ops.probe(args))}
    assert (
        not {
            "budget_exhausted",
            "provider_errors_repeated",
            "evidence_verification_failed",
        }
        & cleared
    )
