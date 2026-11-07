"""Integrity witnesses for trusted, trial-local database evidence; not agent signatures."""

import hashlib
import json

from psycopg.types.json import Jsonb

ZERO = "0" * 64


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def append(conn, trial_id, event, state):
    count, previous = conn.execute(
        "SELECT event_count,digest FROM benchmark_evidence_heads WHERE trial_id=%s FOR UPDATE",
        (trial_id,),
    ).fetchone()
    if event["sequence"] != count + 1:
        raise RuntimeError("evidence witness sequence mismatch")
    effect = conn.execute(
        "SELECT operation_key,kind,payload FROM benchmark_effects WHERE trial_id=%s AND operation_key=%s",
        (trial_id, event["operation_key"]),
    ).fetchone()
    receipt = {
        "sequence": event["sequence"],
        "previous": previous,
        "event_sha256": digest(event),
        "state_sha256": digest(state),
        "effect_sha256": digest(
            {"operation_key": effect[0], "kind": effect[1], "payload": effect[2]}
            if effect
            else None
        ),
    }
    receipt["digest"] = digest(receipt)
    conn.execute(
        "INSERT INTO benchmark_receipts VALUES(%s,%s,%s)",
        (trial_id, count + 1, Jsonb(receipt)),
    )
    conn.execute(
        "UPDATE benchmark_evidence_heads SET event_count=%s,digest=%s WHERE trial_id=%s",
        (count + 1, receipt["digest"], trial_id),
    )


def load(conn, trial_id):
    events = [
        r[0]
        for r in conn.execute(
            "SELECT payload FROM benchmark_events WHERE trial_id=%s ORDER BY sequence",
            (trial_id,),
        )
    ]
    effects = [
        {"operation_key": r[0], "kind": r[1], "payload": r[2]}
        for r in conn.execute(
            "SELECT operation_key,kind,payload FROM benchmark_effects WHERE trial_id=%s ORDER BY operation_key",
            (trial_id,),
        )
    ]
    receipts = [
        r[0]
        for r in conn.execute(
            "SELECT payload FROM benchmark_receipts WHERE trial_id=%s ORDER BY sequence",
            (trial_id,),
        )
    ]
    head = conn.execute(
        "SELECT event_count,digest FROM benchmark_evidence_heads WHERE trial_id=%s",
        (trial_id,),
    ).fetchone()
    return (
        events,
        effects,
        receipts,
        {"event_count": head[0], "digest": head[1]} if head else None,
    )


def verify_stored(conn, trial_id):
    """Check present evidence against a frozen verdict; never overwrite that verdict."""
    from .registry import validate_snapshot

    row = conn.execute(
        "SELECT t.state,t.result,e.manifest,t.scenario FROM benchmark_trials t JOIN benchmark_evaluations e ON e.id=t.evaluation_id WHERE t.id=%s",
        (trial_id,),
    ).fetchone()
    if not row or not row[1] or not row[1].get("provenance"):
        return None  # Historical versions do not assert a v4 witness.
    try:
        validate_snapshot(row[2])
    except (ValueError, KeyError, OSError):
        return False
    provenance = row[1]["provenance"]
    for key in ("dataset", "evaluator", "agent"):
        if provenance[key] != {k: row[2][key][k] for k in ("id", "version", "sha256")}:
            return False
    if (
        provenance["scenario_sha256"] != digest(row[3])
        or provenance["scenario_version"] != row[3]["version"]
    ):
        return False
    events, effects, receipts, head = load(conn, trial_id)
    current = digest(
        {
            "state": row[0],
            "events": events,
            "effects": effects,
            "receipts": receipts,
            "head": head,
        }
    )
    return (
        row[1]["provenance"]["references_valid"]
        and current == row[1]["provenance"]["evidence_sha256"]
    )


def _require_intact(conn, trial_id, initial_state):
    """Fence dispatch/replay on the platform-owned journal, under the trial row lock."""
    from ..reliability import EvaluatorFailure

    # The indexed identities drive replay. The JSON identities drive grading.
    # Both must agree before a persisted response is trusted.
    identity_conflict = conn.execute(
        """SELECT EXISTS (
            SELECT 1 FROM benchmark_events WHERE trial_id=%s AND (
                sequence::text IS DISTINCT FROM payload->>'sequence'
                OR operation_key IS DISTINCT FROM payload->>'operation_key')
            UNION ALL
            SELECT 1 FROM benchmark_receipts WHERE trial_id=%s
                AND sequence::text IS DISTINCT FROM payload->>'sequence'
        )""",
        (trial_id, trial_id),
    ).fetchone()[0]
    if identity_conflict:
        raise EvaluatorFailure("broker evidence identity conflict")
    events, effects, receipts, head = load(conn, trial_id)
    state = conn.execute(
        "SELECT state FROM benchmark_trials WHERE id=%s", (trial_id,)
    ).fetchone()[0]
    if (
        head is None
        or head["event_count"] != len(events)
        or len(events) != len(receipts)
    ):
        raise EvaluatorFailure("broker evidence is incomplete")
    ledger = {e["operation_key"]: e for e in effects}
    previous = ZERO
    for index, (event, receipt) in enumerate(zip(events, receipts, strict=True), 1):
        key = f"{trial_id}:support-tools-v1:{index}"
        body = {k: v for k, v in receipt.items() if k != "digest"}
        effect = ledger.pop(key, None)
        if (
            event.get("sequence") != index
            or event.get("operation_key") != key
            or receipt.get("sequence") != index
            or receipt.get("previous") != previous
            or receipt.get("digest") != digest(body)
            or receipt.get("event_sha256") != digest(event)
            or receipt.get("effect_sha256") != digest(effect)
            or bool(effect) != bool(event.get("effect"))
            or (
                effect
                and (
                    effect["kind"] != event["effect"]
                    or effect["payload"]
                    != {"tool": event["tool"], "arguments": event["arguments"]}
                )
            )
        ):
            raise EvaluatorFailure("broker evidence integrity conflict")
        previous = receipt["digest"]
    expected_state = receipts[-1]["state_sha256"] if receipts else digest(initial_state)
    if ledger or head["digest"] != previous or digest(state) != expected_state:
        raise EvaluatorFailure("broker state or witness conflict")


def require_intact(conn, trial_id, initial_state):
    from ..reliability import EvaluatorFailure

    try:
        _require_intact(conn, trial_id, initial_state)
    except (KeyError, TypeError, AttributeError, ValueError) as error:
        raise EvaluatorFailure("malformed broker evidence") from error
