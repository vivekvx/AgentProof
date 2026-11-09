"""Durable whole-study budget tests; no model calls are made."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import psycopg
import pytest
from agentproof.benchmark.experiment_budget import (
    BudgetExceeded,
    ReservationConflict,
    initialize,
    reserve_slot,
    settle_slot,
)


@pytest.fixture
def study_id(dsn):
    study = "budget-test-" + uuid4().hex
    with psycopg.connect(dsn) as conn:
        initialize(conn, study)
    yield study
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "DELETE FROM benchmark_study_reservations WHERE study_id=%s", (study,)
        )
        conn.execute("DELETE FROM benchmark_study_budgets WHERE study_id=%s", (study,))


def usage(requests=1, tools=1, tokens=100, seconds=1, cost="0.001"):
    return {
        "slots": 0,
        "requests": requests,
        "tool_calls": tools,
        "tokens": tokens,
        "active_ms": seconds * 1000,
        "cost_usd": cost,
    }


def test_reservation_is_idempotent_and_settles_known_usage(dsn, study_id):
    study = study_id
    with psycopg.connect(dsn) as conn:
        assert reserve_slot(conn, "slot-1", study) == "created"
        assert reserve_slot(conn, "slot-1", study) == "existing"
        assert (
            settle_slot(conn, "slot-1", usage(), known=True, study_id=study)
            == "settled"
        )
        assert (
            settle_slot(conn, "slot-1", usage(), known=True, study_id=study)
            == "settled"
        )
        row = conn.execute(
            "SELECT reserved_slots,reserved_requests,reserved_tokens,reserved_cost_usd FROM benchmark_study_budgets WHERE study_id=%s",
            (study,),
        ).fetchone()
        assert row[:3] == (1, 1, 100)
        assert str(row[3]) == "0.001000"


def test_uncertain_usage_keeps_full_reservation_and_no_replacement_slot(dsn, study_id):
    study = study_id
    with psycopg.connect(dsn) as conn:
        reserve_slot(conn, "slot-1", study)
        settle_slot(conn, "slot-1", {}, known=False, study_id=study)
        with pytest.raises(ReservationConflict, match="reconciled differently"):
            settle_slot(conn, "slot-1", usage(), known=True, study_id=study)
        for i in range(2, 9):
            reserve_slot(conn, f"slot-{i}", study)
        with pytest.raises(BudgetExceeded, match="slots ceiling"):
            reserve_slot(conn, "replacement", study)


def test_concurrent_reservations_cannot_exceed_eight_slots(dsn, study_id):
    study = study_id

    def reserve(i):
        with psycopg.connect(dsn) as conn:
            try:
                return reserve_slot(conn, f"slot-{i}", study)
            except BudgetExceeded:
                return "blocked"

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(reserve, range(12)))
    assert results.count("created") == 8
    assert results.count("blocked") == 4


def test_late_observed_usage_cannot_exceed_reserved_limits(dsn, study_id):
    study = study_id
    with psycopg.connect(dsn) as conn:
        reserve_slot(conn, "slot-1", study)
        with pytest.raises(BudgetExceeded, match="observed requests"):
            settle_slot(
                conn,
                "slot-1",
                usage(requests=7),
                known=True,
                study_id=study,
            )
        row = conn.execute(
            "SELECT state FROM benchmark_study_reservations WHERE study_id=%s AND slot_id='slot-1'",
            (study,),
        ).fetchone()
        assert row == ("reserved",)
