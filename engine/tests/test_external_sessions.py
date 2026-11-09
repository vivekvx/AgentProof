import psycopg
import pytest
from agentproof.benchmark.external_sessions import (
    SessionError,
    cancel,
    complete,
    create,
    expire,
    load,
)


def test_session_credential_is_hashed_and_completion_is_durable(benchmark_factory, dsn):
    evaluation_id, trials, _ = benchmark_factory(ids=["cs-001"], version=5)
    trial_id = trials[0]
    with psycopg.connect(dsn) as conn:
        created = create(conn, trial_id, evaluation_id, "cs-001", 1)
        assert created.session.state == "waiting"
        with pytest.raises(SessionError, match="credential"):
            load(conn, trial_id, "wrong", 1)
        completed = complete(
            conn,
            trial_id,
            created.credential,
            1,
            {"resolution": "completed", "message": "Done"},
        )
        assert completed.state == "completed"
        with pytest.raises(SessionError, match="terminal"):
            complete(conn, trial_id, created.credential, 1, {"resolution": "completed"})
        stored = conn.execute(
            "SELECT credential_hash,state,outcome FROM benchmark_external_sessions WHERE trial_id=%s",
            (trial_id,),
        ).fetchone()
        assert stored[0] != created.credential
        assert stored[1] == "completed"
        assert stored[2]["resolution"] == "completed"
        conn.execute(
            "DELETE FROM benchmark_external_sessions WHERE trial_id=%s", (trial_id,)
        )


def test_session_expiry_cancellation_and_attempt_fencing(benchmark_factory, dsn):
    evaluation_id, trials, _ = benchmark_factory(ids=["cs-001"], trials=2, version=5)
    with psycopg.connect(dsn) as conn:
        expiring = create(conn, trials[0], evaluation_id, "cs-001", 1, ttl_seconds=1)
        cancellable = create(conn, trials[1], evaluation_id, "cs-001", 1)
        with pytest.raises(SessionError, match="identity"):
            load(conn, trials[1], cancellable.credential, 2)
        conn.execute(
            "UPDATE benchmark_external_sessions SET expires_at=now()-interval '1 second' "
            "WHERE trial_id=%s",
            (trials[0],),
        )
        expired = expire(conn, trials[0])
        assert expired.state == "expired"
        with pytest.raises(SessionError, match="terminal"):
            complete(
                conn,
                trials[0],
                expiring.credential,
                1,
                {"resolution": "completed"},
            )
        cancelled = cancel(conn, trials[1], cancellable.credential, 1)
        assert cancelled.state == "cancelled"
        with pytest.raises(SessionError, match="terminal"):
            cancel(conn, trials[1], cancellable.credential, 1)
