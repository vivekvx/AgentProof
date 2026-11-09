"""Durable credentials and state for external benchmark trial sessions."""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb


class SessionError(ValueError):
    pass


@dataclass(frozen=True)
class ExternalSession:
    trial_id: UUID
    evaluation_id: UUID
    scenario_id: str
    attempt: int
    state: str
    expires_at: datetime
    outcome: dict[str, Any] | None


@dataclass(frozen=True)
class CreatedSession:
    session: ExternalSession
    credential: str


def _session(row):
    return ExternalSession(*row)


def create(conn, trial_id, evaluation_id, scenario_id, attempt, ttl_seconds=300):
    if type(attempt) is not int or attempt < 1 or not 1 <= ttl_seconds <= 600:
        raise SessionError("invalid attempt or session lifetime")
    trial_id, evaluation_id = UUID(str(trial_id)), UUID(str(evaluation_id))
    row = conn.execute(
        "SELECT evaluation_id,scenario_id,scenario FROM benchmark_trials WHERE id=%s",
        (trial_id,),
    ).fetchone()
    if not row or row[0] != evaluation_id or row[1] != scenario_id:
        raise SessionError("trial identity mismatch")
    if row[2].get("id") != scenario_id:
        raise SessionError("stored scenario identity mismatch")
    credential = secrets.token_urlsafe(32)
    digest = hashlib.sha256(credential.encode()).hexdigest()
    with conn.transaction():
        existing = conn.execute(
            "SELECT 1 FROM benchmark_external_sessions WHERE trial_id=%s FOR UPDATE",
            (trial_id,),
        ).fetchone()
        if existing:
            raise SessionError("session already exists")
        row = conn.execute(
            "INSERT INTO benchmark_external_sessions(trial_id,evaluation_id,scenario_id,attempt,state,credential_hash,expires_at) "
            "VALUES(%s,%s,%s,%s,'waiting',%s,now()+(%s * interval '1 second')) "
            "RETURNING trial_id,evaluation_id,scenario_id,attempt,state,expires_at,outcome",
            (trial_id, evaluation_id, scenario_id, attempt, digest, ttl_seconds),
        ).fetchone()
    return CreatedSession(_session(row), credential)


def load(conn, trial_id, credential, attempt):
    row = conn.execute(
        "SELECT trial_id,evaluation_id,scenario_id,attempt,state,expires_at,outcome,credential_hash "
        "FROM benchmark_external_sessions WHERE trial_id=%s FOR UPDATE",
        (UUID(str(trial_id)),),
    ).fetchone()
    if not row or type(attempt) is not int or attempt != row[3]:
        raise SessionError("session identity mismatch")
    supplied = hashlib.sha256(credential.encode()).hexdigest()
    if not secrets.compare_digest(supplied, row[7]):
        raise SessionError("invalid session credential")
    if row[4] == "waiting" and row[5] <= datetime.now(row[5].tzinfo):
        raise SessionError("session expired")
    return _session(row[:7])


def load_authorized(conn, trial_id, attempt):
    """Load a session after the private API transport has authenticated its token."""
    row = conn.execute(
        "SELECT trial_id,evaluation_id,scenario_id,attempt,state,expires_at,outcome "
        "FROM benchmark_external_sessions WHERE trial_id=%s FOR UPDATE",
        (UUID(str(trial_id)),),
    ).fetchone()
    if not row or type(attempt) is not int or attempt != row[3]:
        raise SessionError("session identity mismatch")
    if row[4] == "waiting" and row[5] <= datetime.now(row[5].tzinfo):
        raise SessionError("session expired")
    return _session(row)


def _terminal(conn, trial_id, credential, attempt, state, outcome=None):
    current = load(conn, trial_id, credential, attempt)
    if current.state != "waiting":
        raise SessionError("session is terminal")
    row = conn.execute(
        "UPDATE benchmark_external_sessions SET state=%s,outcome=%s,completed_at=now() "
        "WHERE trial_id=%s AND state='waiting' AND attempt=%s AND credential_hash=%s AND expires_at>now() "
        "RETURNING trial_id,evaluation_id,scenario_id,attempt,state,expires_at,outcome",
        (
            state,
            Jsonb(outcome) if outcome is not None else None,
            current.trial_id,
            attempt,
            hashlib.sha256(credential.encode()).hexdigest(),
        ),
    ).fetchone()
    if not row:
        raise SessionError("session changed or expired")
    return _session(row)


def complete(conn, trial_id, credential, attempt, outcome):
    if not isinstance(outcome, dict) or set(outcome) - {
        "resolution",
        "message",
        "error_code",
        "last_sequence",
    }:
        raise SessionError("invalid completion outcome")
    return _terminal(conn, trial_id, credential, attempt, "completed", outcome)


def complete_authorized(conn, trial_id, attempt, outcome):
    if not isinstance(outcome, dict) or set(outcome) - {
        "resolution",
        "message",
        "error_code",
        "last_sequence",
    }:
        raise SessionError("invalid completion outcome")
    current = load_authorized(conn, trial_id, attempt)
    if current.state != "waiting":
        raise SessionError("session is terminal")
    row = conn.execute(
        "UPDATE benchmark_external_sessions SET state='completed',outcome=%s,completed_at=now() "
        "WHERE trial_id=%s AND state='waiting' AND attempt=%s AND expires_at>now() "
        "RETURNING trial_id,evaluation_id,scenario_id,attempt,state,expires_at,outcome",
        (Jsonb(outcome), current.trial_id, attempt),
    ).fetchone()
    if not row:
        raise SessionError("session changed or expired")
    return _session(row)


def cancel(conn, trial_id, credential, attempt):
    return _terminal(conn, trial_id, credential, attempt, "cancelled")


def expire(conn, trial_id):
    row = conn.execute(
        "UPDATE benchmark_external_sessions SET state='expired',completed_at=now() "
        "WHERE trial_id=%s AND state='waiting' AND expires_at<=now() "
        "RETURNING trial_id,evaluation_id,scenario_id,attempt,state,expires_at,outcome",
        (UUID(str(trial_id)),),
    ).fetchone()
    return _session(row) if row else None


def terminate_waiting(conn, trial_id, attempt, state):
    """Set an internally expired or cancelled session to one terminal state."""
    if state not in {"expired", "cancelled"}:
        raise SessionError("invalid internal terminal state")
    if type(attempt) is not int or attempt < 1:
        raise SessionError("invalid session attempt")
    row = conn.execute(
        "UPDATE benchmark_external_sessions SET state=%s,completed_at=now() "
        "WHERE trial_id=%s AND attempt=%s AND state='waiting' "
        "AND ((%s='expired' AND expires_at<=now()) OR %s='cancelled') "
        "RETURNING trial_id,evaluation_id,scenario_id,attempt,state,expires_at,outcome",
        (state, UUID(str(trial_id)), attempt, state, state),
    ).fetchone()
    if row:
        return _session(row)
    current = conn.execute(
        "SELECT trial_id,evaluation_id,scenario_id,attempt,state,expires_at,outcome "
        "FROM benchmark_external_sessions WHERE trial_id=%s",
        (UUID(str(trial_id)),),
    ).fetchone()
    if current and current[3] == attempt and current[4] == state:
        return _session(current)
    raise SessionError("session is not waiting for the requested terminal state")


def cancel_authorized(conn, trial_id, attempt):
    return terminate_waiting(conn, trial_id, attempt, "cancelled")
