"""Durable whole-study budget tests; no model calls are made."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from uuid import uuid4

import psycopg
import pytest
from agentproof.benchmark.execution import execute_trial, fail_trial
from agentproof.benchmark.experiment_budget import (
    BudgetExceeded,
    QuotaDeferred,
    ReservationConflict,
    admit_request,
    bind_trial,
    initialize,
    reconcile_request,
    record_rate_limit,
    reserve_slot,
    settle_slot,
)
from agentproof.benchmark.llm import Journal, ProviderFailure


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


def test_restart_and_duplicate_delivery_preserve_one_reservation(dsn, study_id):
    with psycopg.connect(dsn) as conn:
        assert reserve_slot(conn, "slot-1", study_id) == "created"
    with psycopg.connect(dsn) as reopened:
        assert reserve_slot(reopened, "slot-1", study_id) == "existing"
        row = reopened.execute(
            "SELECT reserved_slots,reserved_requests FROM benchmark_study_budgets WHERE study_id=%s",
            (study_id,),
        ).fetchone()
        assert row == (1, 6)


def test_cancelled_slot_with_unknown_usage_stays_reserved_after_restart(dsn, study_id):
    with psycopg.connect(dsn) as conn:
        reserve_slot(conn, "cancelled-slot", study_id)
    with psycopg.connect(dsn) as reopened:
        assert (
            settle_slot(reopened, "cancelled-slot", {}, known=False, study_id=study_id)
            == "unknown"
        )
    with psycopg.connect(dsn) as reopened:
        assert reserve_slot(reopened, "cancelled-slot", study_id) == "existing"
        assert reopened.execute(
            "SELECT state FROM benchmark_study_reservations WHERE study_id=%s AND slot_id='cancelled-slot'",
            (study_id,),
        ).fetchone() == ("unknown",)
        row = reopened.execute(
            "SELECT reserved_slots,reserved_requests,reserved_tokens FROM benchmark_study_budgets WHERE study_id=%s",
            (study_id,),
        ).fetchone()
        assert row == (1, 6, 64000)


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


@pytest.mark.parametrize(
    "change",
    [
        {"requests": -1},
        {"tokens": -100},
        {"active_ms": -1},
        {"tool_calls": 0.5},
        {"requests": True},
        {"cost_usd": "-0.01"},
        {"cost_usd": "NaN"},
        {"cost_usd": "Infinity"},
    ],
)
def test_invalid_observed_usage_cannot_release_budget(dsn, study_id, change):
    with psycopg.connect(dsn) as conn:
        reserve_slot(conn, "slot-1", study_id)
        invalid = usage()
        invalid.update(change)
        with pytest.raises(ValueError, match="observed"):
            settle_slot(conn, "slot-1", invalid, known=True, study_id=study_id)
        row = conn.execute(
            "SELECT reserved_requests,reserved_tokens,reserved_cost_usd FROM benchmark_study_budgets WHERE study_id=%s",
            (study_id,),
        ).fetchone()
        assert row == (6, 64000, Decimal("0.1"))
        assert conn.execute(
            "SELECT state FROM benchmark_study_reservations WHERE study_id=%s AND slot_id='slot-1'",
            (study_id,),
        ).fetchone() == ("reserved",)


@pytest.fixture
def bound_trial(dsn, study_id, benchmark_factory):
    _, trials, _ = benchmark_factory(version=5, ids=["cs-001"])
    trial_id = trials[0]
    with psycopg.connect(dsn) as conn:
        assert bind_trial(conn, trial_id, "slot-1", study_id) == "created"
        # This fixture tests request accounting, not elapsed study wall time.
        conn.execute(
            "UPDATE benchmark_study_budgets SET created_at=clock_timestamp() WHERE study_id=%s",
            (study_id,),
        )
    yield trial_id
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "DELETE FROM benchmark_study_requests WHERE trial_id=%s", (trial_id,)
        )
        conn.execute(
            "DELETE FROM benchmark_study_trials WHERE trial_id=%s", (trial_id,)
        )


def test_request_admission_is_durable_and_idempotent(dsn, bound_trial):
    with psycopg.connect(dsn) as conn:
        assert admit_request(conn, bound_trial, 1, 1000, "0.01") == "created"
    with psycopg.connect(dsn) as conn:
        assert admit_request(conn, bound_trial, 1, 1000, "0.01") == "existing"
        with pytest.raises(ReservationConflict, match="parameters differ"):
            admit_request(conn, bound_trial, 1, 1001, "0.01")
        assert (
            reconcile_request(conn, bound_trial, 1, tokens=30, estimated_cost="0.001")
            == "known"
        )
        assert (
            reconcile_request(conn, bound_trial, 1, tokens=30, estimated_cost="0.001")
            == "known"
        )
        assert conn.execute(
            "SELECT count(*) FROM benchmark_study_requests WHERE trial_id=%s",
            (bound_trial,),
        ).fetchone() == (1,)


def test_unknown_result_retains_request_bound(dsn, bound_trial):
    with psycopg.connect(dsn) as conn:
        admit_request(conn, bound_trial, 1, 64000, "0.1")
        assert reconcile_request(conn, bound_trial, 1) == "unknown"
        with pytest.raises(BudgetExceeded, match="token ceiling"):
            admit_request(conn, bound_trial, 2, 1, "0.001")


def test_known_usage_releases_only_proven_unused_capacity(dsn, bound_trial):
    with psycopg.connect(dsn) as conn:
        admit_request(conn, bound_trial, 1, 64000, "0.1")
        reconcile_request(conn, bound_trial, 1, tokens=30, estimated_cost="0.001")
        assert admit_request(conn, bound_trial, 2, 63970, "0.099") == "created"
        with pytest.raises(BudgetExceeded, match="token ceiling"):
            admit_request(conn, bound_trial, 3, 1, "0.001")


def test_concurrent_request_admission_respects_slot_limits(dsn, bound_trial):
    def attempt(i):
        with psycopg.connect(dsn) as conn:
            try:
                return admit_request(conn, bound_trial, i, 64000, "0.1")
            except BudgetExceeded:
                return "blocked"

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(attempt, range(1, 7)))
    assert results.count("created") == 1
    assert results.count("blocked") == 5


def test_rejected_request_has_zero_observed_usage(dsn, bound_trial):
    with psycopg.connect(dsn) as conn:
        admit_request(conn, bound_trial, 1, 1000, "0.01")
        assert reconcile_request(conn, bound_trial, 1, rejected=True) == "rejected"
        assert conn.execute(
            "SELECT observed_tokens,estimated_cost_usd FROM benchmark_study_requests WHERE trial_id=%s AND sequence=1",
            (bound_trial,),
        ).fetchone() == (0, Decimal(0))


def test_rate_limit_blocks_all_workers_in_study(dsn, bound_trial):
    with psycopg.connect(dsn) as conn:
        assert record_rate_limit(conn, bound_trial, 60) == "recorded"
    with psycopg.connect(dsn) as restarted:
        with pytest.raises(QuotaDeferred, match="cooldown"):
            admit_request(restarted, bound_trial, 1, 100, "0.001")
        assert restarted.execute(
            "SELECT count(*) FROM benchmark_study_requests WHERE trial_id=%s",
            (bound_trial,),
        ).fetchone() == (0,)


@pytest.mark.parametrize("header", ["NaN", "-1", "invalid", None])
def test_invalid_quota_header_cannot_extend_study(dsn, bound_trial, header):
    with psycopg.connect(dsn) as conn:
        assert record_rate_limit(conn, bound_trial, header) == "recorded"
        assert admit_request(conn, bound_trial, 1, 100, "0.001") == "created"


def test_journal_rate_limit_rejects_next_provider_dispatch(dsn, bound_trial):
    with psycopg.connect(dsn, autocommit=True) as conn:
        journal = Journal(conn, bound_trial)
        assert journal.reserve(1, 100, "0.001") is None
        journal.fail(1, "rate_limit", {"rate_limits": {"retry-after": "60"}})
        with pytest.raises(ProviderFailure, match="rate_limit"):
            journal.reserve(2, 100, "0.001")
        assert conn.execute(
            "SELECT count(*) FROM benchmark_study_requests WHERE trial_id=%s",
            (bound_trial,),
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT state,observed_tokens FROM benchmark_study_requests WHERE trial_id=%s AND sequence=1",
            (bound_trial,),
        ).fetchone() == ("unknown", None)


def test_terminal_trial_closes_pending_request_as_unknown(dsn, bound_trial):
    with psycopg.connect(dsn, autocommit=True) as conn:
        assert Journal(conn, bound_trial).reserve(1, 1000, "0.01") is None
    result = fail_trial(bound_trial, "cancelled", dsn=dsn)
    assert result["verdict"] == "INDETERMINATE"
    with psycopg.connect(dsn) as conn:
        assert conn.execute(
            "SELECT state,observed_tokens,estimated_cost_usd FROM benchmark_study_requests WHERE trial_id=%s AND sequence=1",
            (bound_trial,),
        ).fetchone() == ("unknown", None, None)
        assert conn.execute(
            "SELECT status,metadata->>'error' FROM benchmark_model_calls WHERE trial_id=%s AND sequence=1",
            (bound_trial,),
        ).fetchone() == ("failed", "interrupted_unknown_usage")


@pytest.mark.asyncio
async def test_completed_trial_closes_pending_request_as_unknown(dsn, bound_trial):
    with psycopg.connect(dsn, autocommit=True) as conn:
        assert Journal(conn, bound_trial).reserve(1, 1000, "0.01") is None
    result = await execute_trial(bound_trial, dsn)
    assert result["verdict"] == "PASS"
    with psycopg.connect(dsn) as conn:
        assert conn.execute(
            "SELECT state,observed_tokens FROM benchmark_study_requests WHERE trial_id=%s AND sequence=1",
            (bound_trial,),
        ).fetchone() == ("unknown", None)
        assert conn.execute(
            "SELECT status,metadata->>'error' FROM benchmark_model_calls WHERE trial_id=%s AND sequence=1",
            (bound_trial,),
        ).fetchone() == ("failed", "interrupted_unknown_usage")
