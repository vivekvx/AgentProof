"""Atomic durable reservations for the predeclared Groq study."""

import json
from decimal import Decimal

STUDY_ID = "groq-gpt-oss-matched-development"
CEILING = {
    "slots": 8,
    "requests": 48,
    "tool_calls": 80,
    "tokens": 512000,
    "active_ms": 40000,
    "cost_usd": Decimal("0.8"),
}
PER_SLOT = {
    "slots": 1,
    "requests": 6,
    "tool_calls": 10,
    "tokens": 64000,
    "active_ms": 5000,
    "cost_usd": Decimal("0.1"),
}
COUNTERS = {
    "slots": "reserved_slots",
    "requests": "reserved_requests",
    "tool_calls": "reserved_tool_calls",
    "tokens": "reserved_tokens",
    "active_ms": "reserved_active_ms",
    "cost_usd": "reserved_cost_usd",
}


class BudgetExceeded(ValueError):
    pass


class ReservationConflict(ValueError):
    pass


def initialize(conn, study_id=STUDY_ID):
    """Create the one bounded study row. Existing limits are immutable."""
    limits = {k: str(v) if isinstance(v, Decimal) else v for k, v in CEILING.items()}
    with conn.transaction():
        row = conn.execute(
            """INSERT INTO benchmark_study_budgets(study_id,limits)
               VALUES(%s,%s) ON CONFLICT(study_id) DO NOTHING
               RETURNING limits""",
            (study_id, json.dumps(limits)),
        ).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT limits FROM benchmark_study_budgets WHERE study_id=%s FOR UPDATE",
                (study_id,),
            ).fetchone()
        if row is None or row[0] != limits:
            raise ReservationConflict("study limits differ from the immutable ceiling")
    return limits


def reserve_slot(conn, slot_id, study_id=STUDY_ID):
    """Reserve a full slot atomically; retries with the same ID are idempotent."""
    reserve = {k: str(v) if isinstance(v, Decimal) else v for k, v in PER_SLOT.items()}
    with conn.transaction():
        row = conn.execute(
            "SELECT limits FROM benchmark_study_budgets WHERE study_id=%s FOR UPDATE",
            (study_id,),
        ).fetchone()
        if row is None:
            raise ReservationConflict("study budget is not initialized")
        prior = conn.execute(
            "SELECT reserved FROM benchmark_study_reservations WHERE study_id=%s AND slot_id=%s",
            (study_id, slot_id),
        ).fetchone()
        if prior:
            if prior[0] != reserve:
                raise ReservationConflict("slot reservation parameters differ")
            return "existing"
        current = conn.execute(
            "SELECT reserved_slots,reserved_requests,reserved_tool_calls,reserved_tokens,reserved_active_ms,reserved_cost_usd FROM benchmark_study_budgets WHERE study_id=%s",
            (study_id,),
        ).fetchone()
        keys = tuple(COUNTERS)
        values = dict(zip(keys, current, strict=True))
        for key, amount in reserve.items():
            if Decimal(str(values[key])) + Decimal(str(amount)) > Decimal(
                str(CEILING[key])
            ):
                raise BudgetExceeded(f"study {key} ceiling reached")
        conn.execute(
            "INSERT INTO benchmark_study_reservations(study_id,slot_id,state,reserved) VALUES(%s,%s,'reserved',%s)",
            (study_id, slot_id, json.dumps(reserve)),
        )
        conn.execute(
            """UPDATE benchmark_study_budgets SET reserved_slots=reserved_slots+1,
               reserved_requests=reserved_requests+6,reserved_tool_calls=reserved_tool_calls+10,
               reserved_tokens=reserved_tokens+64000,reserved_active_ms=reserved_active_ms+5000,
               reserved_cost_usd=reserved_cost_usd+0.1 WHERE study_id=%s""",
            (study_id,),
        )
    return "created"


def settle_slot(conn, slot_id, observed, *, known, study_id=STUDY_ID):
    """Release unused reservation only when all billed usage is known.

    Unknown or late usage keeps the full reservation. This favors safety over
    additional throughput and prevents a missing usage record from freeing budget.
    """
    with conn.transaction():
        conn.execute(
            "SELECT 1 FROM benchmark_study_budgets WHERE study_id=%s FOR UPDATE",
            (study_id,),
        ).fetchone()
        row = conn.execute(
            "SELECT state,reserved,observed FROM benchmark_study_reservations WHERE study_id=%s AND slot_id=%s FOR UPDATE",
            (study_id, slot_id),
        ).fetchone()
        if row is None:
            raise ReservationConflict("unknown study slot")
        state, reserved, previous = row
        payload = {
            k: str(v) if isinstance(v, Decimal) else v for k, v in observed.items()
        }
        if state != "reserved":
            if previous != payload or state != ("settled" if known else "unknown"):
                raise ReservationConflict("slot already reconciled differently")
            return state
        if known and set(payload) != set(COUNTERS):
            raise ValueError("observed usage must include every budget dimension")
        if known:
            for key, amount in payload.items():
                if Decimal(str(amount)) > Decimal(str(reserved[key])):
                    raise BudgetExceeded(f"observed {key} exceeds its reservation")
        else:
            payload = {k: None for k in COUNTERS}
        final_state = "settled" if known else "unknown"
        if known:
            delta = {
                k: Decimal(str(reserved[k])) - Decimal(str(payload[k]))
                for k in COUNTERS
            }
            conn.execute(
                """UPDATE benchmark_study_budgets SET
                   reserved_requests=reserved_requests-%s,reserved_tool_calls=reserved_tool_calls-%s,
                   reserved_tokens=reserved_tokens-%s,reserved_active_ms=reserved_active_ms-%s,
                   reserved_cost_usd=reserved_cost_usd-%s WHERE study_id=%s""",
                tuple(
                    delta[k]
                    for k in (
                        "requests",
                        "tool_calls",
                        "tokens",
                        "active_ms",
                        "cost_usd",
                    )
                )
                + (study_id,),
            )
        conn.execute(
            "UPDATE benchmark_study_reservations SET state=%s,observed=%s,settled_at=now() WHERE study_id=%s AND slot_id=%s",
            (final_state, json.dumps(payload), study_id, slot_id),
        )
    return final_state
