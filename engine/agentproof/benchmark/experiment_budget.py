"""Atomic durable reservations for the predeclared Groq study."""

import json
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from .experiment import digest, load_plan, verify_plan

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


class QuotaDeferred(BudgetExceeded):
    pass


def record_rate_limit(conn, trial_id, retry_after):
    """Persist a bounded provider cooldown; invalid headers cannot extend it."""
    try:
        seconds = Decimal(str(retry_after))
    except (InvalidOperation, ValueError, TypeError):
        seconds = Decimal(0)
    if not seconds.is_finite() or seconds < 0:
        seconds = Decimal(0)
    seconds = min(seconds, Decimal(180))
    with conn.transaction():
        binding = conn.execute(
            "SELECT study_id FROM benchmark_study_trials WHERE trial_id=%s",
            (trial_id,),
        ).fetchone()
        if binding is None:
            return "unbound"
        conn.execute(
            """UPDATE benchmark_study_budgets
               SET provider_not_before=greatest(coalesce(provider_not_before,clock_timestamp()),
                   clock_timestamp()+(%s::numeric * interval '1 second'))
               WHERE study_id=%s""",
            (seconds, binding[0]),
        )
    return "recorded"


def bind_trial(conn, trial_id, slot_id, study_id=STUDY_ID):
    """Bind one planned trial to one study slot before worker dispatch."""
    with conn.transaction():
        row = conn.execute(
            "SELECT t.result,t.scenario_id,t.trial_index,t.scenario,e.manifest FROM benchmark_trials t JOIN benchmark_evaluations e ON e.id=t.evaluation_id WHERE t.id=%s FOR UPDATE OF t",
            (trial_id,),
        ).fetchone()
        if row is None or row[0] is not None:
            raise ReservationConflict("study trial is missing or terminal")
        if study_id == STUDY_ID:
            plan = load_plan()
            verify_plan(plan)
            scenario_id, repeat, scenario, manifest = row[1:]
            profile = next(
                (p for p in plan["profiles"] if p["id"] == manifest["agent"]["id"]),
                None,
            )
            if (
                profile is None
                or manifest["agent"]["version"] != profile["version"]
                or manifest["agent"]["sha256"] != profile["sha256"]
                or manifest["dataset"]["sha256"] != plan["dataset"]["sha256"]
                or manifest["evaluator"]["sha256"] != plan["evaluator"]["sha256"]
                or manifest["tool_schema"]["sha256"] != plan["tool_schema"]["sha256"]
                or scenario_id not in plan["scenario_ids"]
                or not 1 <= repeat <= plan["trials_per_scenario"]
                or manifest["split"] != plan["split"]
                or manifest["trials"] != plan["trials_per_scenario"]
                or digest(
                    json.dumps(scenario, sort_keys=True, separators=(",", ":")).encode()
                )
                != plan["scenario_sha256"][scenario_id]
                or slot_id != f"{profile['id']}:{scenario_id}:{repeat}"
            ):
                raise ReservationConflict("trial differs from pinned study plan")
        prior = conn.execute(
            "SELECT study_id,slot_id FROM benchmark_study_trials WHERE trial_id=%s",
            (trial_id,),
        ).fetchone()
        if prior:
            if prior != (study_id, slot_id):
                raise ReservationConflict("trial is bound to another study slot")
            return "existing"
        reserve_slot(conn, slot_id, study_id)
        conn.execute(
            "INSERT INTO benchmark_study_trials(trial_id,study_id,slot_id) VALUES(%s,%s,%s)",
            (trial_id, study_id, slot_id),
        )
    return "created"


def admit_request(conn, trial_id, sequence, tokens, estimated_cost):
    """Reserve a request under the study row lock; never reissue an existing ID."""
    if type(sequence) is not int or sequence < 1:
        raise ValueError("invalid request sequence")
    if type(tokens) is not int or not 0 <= tokens <= PER_SLOT["tokens"]:
        raise BudgetExceeded("request token bound exceeds slot")
    amount = Decimal(str(estimated_cost))
    if not amount.is_finite() or amount < 0 or amount > PER_SLOT["cost_usd"]:
        raise BudgetExceeded("request cost bound exceeds slot")
    with conn.transaction():
        binding = conn.execute(
            "SELECT study_id,slot_id FROM benchmark_study_trials WHERE trial_id=%s",
            (trial_id,),
        ).fetchone()
        if binding is None:
            return "unbound"
        study_id, slot_id = binding
        instance = conn.execute(
            "SELECT status FROM benchmark_studies WHERE id::text=%s",
            (study_id,),
        ).fetchone()
        if instance is not None and instance[0] != "running":
            raise BudgetExceeded("study is cancelled")
        budget = conn.execute(
            "SELECT created_at,provider_not_before FROM benchmark_study_budgets WHERE study_id=%s FOR UPDATE",
            (study_id,),
        ).fetchone()
        if budget is None:
            raise ReservationConflict("study budget is missing")
        existing = conn.execute(
            "SELECT state,reserved_tokens,reserved_cost_usd FROM benchmark_study_requests WHERE trial_id=%s AND sequence=%s",
            (trial_id, sequence),
        ).fetchone()
        if existing:
            if existing[1:] != (tokens, amount):
                raise ReservationConflict("request reservation parameters differ")
            return "existing"
        now = conn.execute("SELECT clock_timestamp()").fetchone()[0]
        if now >= budget[0] + timedelta(seconds=180):
            raise BudgetExceeded("study deadline exceeded")
        if budget[1] is not None and now < budget[1]:
            raise QuotaDeferred("provider quota cooldown is active")
        used = conn.execute(
            """SELECT count(*),
                      coalesce(sum(CASE WHEN state IN ('known','rejected')
                          THEN observed_tokens ELSE reserved_tokens END),0),
                      coalesce(sum(CASE WHEN state IN ('known','rejected')
                          THEN estimated_cost_usd ELSE reserved_cost_usd END),0)
               FROM benchmark_study_requests WHERE trial_id=%s""",
            (trial_id,),
        ).fetchone()
        if used[0] >= PER_SLOT["requests"]:
            raise BudgetExceeded("slot request ceiling reached")
        if used[1] + tokens > PER_SLOT["tokens"]:
            raise BudgetExceeded("slot token ceiling reached")
        if used[2] + amount > PER_SLOT["cost_usd"]:
            raise BudgetExceeded("slot cost ceiling reached")
        slot = conn.execute(
            "SELECT state FROM benchmark_study_reservations WHERE study_id=%s AND slot_id=%s",
            (study_id, slot_id),
        ).fetchone()
        if slot != ("reserved",):
            raise ReservationConflict("study slot is no longer open")
        conn.execute(
            "INSERT INTO benchmark_study_requests(trial_id,sequence,state,reserved_tokens,reserved_cost_usd) VALUES(%s,%s,'reserved',%s,%s)",
            (trial_id, sequence, tokens, amount),
        )
    return "created"


def require_study_binding(conn, trial_id, manifest):
    """Reject an unbound study trial before any model request can start."""
    if manifest.get("schema_version") != "5.0":
        return
    row = conn.execute(
        """SELECT s.status,s.plan_sha256,b.slot_id,e.profile_id,t.scenario_id,t.trial_index
           FROM benchmark_studies s
           JOIN benchmark_study_evaluations e ON e.study_id=s.id
           JOIN benchmark_trials t ON t.evaluation_id=e.evaluation_id
           JOIN benchmark_study_trials b ON b.trial_id=t.id AND b.study_id=s.id::text
           WHERE t.id=%s AND s.id::text=%s""",
        (trial_id, manifest["study_id"]),
    ).fetchone()
    if (
        row is None
        or row[1] != manifest["study_plan"]["sha256"]
        or row[3] != manifest["agent"]["id"]
        or row[2] != f"{row[3]}:{row[4]}:{row[5]}"
    ):
        raise ReservationConflict("study trial has no trusted binding")
    if row[0] != "running":
        raise BudgetExceeded("study is cancelled")


def reconcile_request(
    conn,
    trial_id,
    sequence,
    *,
    tokens=None,
    estimated_cost=None,
    reported_cost=None,
    rejected=False,
):
    """Keep full bounds for unknown outcomes; release only proven unused capacity."""
    with conn.transaction():
        binding = conn.execute(
            "SELECT study_id FROM benchmark_study_trials WHERE trial_id=%s",
            (trial_id,),
        ).fetchone()
        if binding is None:
            return "unbound"
        conn.execute(
            "SELECT 1 FROM benchmark_study_budgets WHERE study_id=%s FOR UPDATE",
            (binding[0],),
        ).fetchone()
        row = conn.execute(
            "SELECT state,reserved_tokens,reserved_cost_usd,observed_tokens,estimated_cost_usd,reported_cost_usd FROM benchmark_study_requests WHERE trial_id=%s AND sequence=%s FOR UPDATE",
            (trial_id, sequence),
        ).fetchone()
        if row is None:
            raise ReservationConflict("request was not admitted")
        if row[0] != "reserved":
            return row[0]
        if rejected:
            state, tokens, estimated_cost = "rejected", 0, Decimal(0)
        elif tokens is None or estimated_cost is None:
            state, tokens, estimated_cost = "unknown", None, None
        else:
            state = "known"
        if state == "known" and (
            type(tokens) is not int or tokens < 0 or tokens > row[1]
        ):
            raise BudgetExceeded("observed tokens exceed reservation")
        if estimated_cost is not None:
            estimated_cost = Decimal(str(estimated_cost))
            if (
                not estimated_cost.is_finite()
                or estimated_cost < 0
                or estimated_cost > row[2]
            ):
                raise BudgetExceeded("observed cost exceeds reservation")
        if reported_cost is not None:
            reported_cost = Decimal(str(reported_cost))
            if not reported_cost.is_finite() or reported_cost < 0:
                raise ValueError("invalid reported cost")
        conn.execute(
            "UPDATE benchmark_study_requests SET state=%s,observed_tokens=%s,estimated_cost_usd=%s,reported_cost_usd=%s,settled_at=now() WHERE trial_id=%s AND sequence=%s",
            (state, tokens, estimated_cost, reported_cost, trial_id, sequence),
        )
    return state


def close_pending_requests(conn, trial_id):
    """Keep full request bounds when terminalization interrupts model dispatch."""
    binding = conn.execute(
        "SELECT study_id FROM benchmark_study_trials WHERE trial_id=%s", (trial_id,)
    ).fetchone()
    if binding is None:
        return 0
    conn.execute(
        "SELECT 1 FROM benchmark_study_budgets WHERE study_id=%s FOR UPDATE",
        (binding[0],),
    )
    changed = conn.execute(
        """UPDATE benchmark_study_requests
           SET state='unknown',settled_at=clock_timestamp()
           WHERE trial_id=%s AND state='reserved'""",
        (trial_id,),
    )
    conn.execute(
        """UPDATE benchmark_model_calls
           SET status='failed',metadata='{"error":"interrupted_unknown_usage"}'::jsonb
           WHERE trial_id=%s AND status='pending'""",
        (trial_id,),
    )
    return changed.rowcount


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
            for key, amount in observed.items():
                if key == "cost_usd":
                    try:
                        value = Decimal(str(amount))
                    except (ValueError, TypeError, InvalidOperation) as error:
                        raise ValueError(
                            "observed cost must be a finite nonnegative number"
                        ) from error
                    if not value.is_finite() or value < 0:
                        raise ValueError(
                            "observed cost must be a finite nonnegative number"
                        )
                elif type(amount) is not int or amount < 0:
                    raise ValueError(f"observed {key} must be a nonnegative integer")
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
