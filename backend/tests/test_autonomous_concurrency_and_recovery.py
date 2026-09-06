"""Concurrency-safe max_repair_calls and conservative orphan recovery regression suite.

Deterministic tests for:
Phase 4:
- Concurrency-safe repair reservations across parallel threads.
- Exactly one winner under max_repair_calls=1 race.
- Provider dispatch boundary reached at most once.
- Repair counter decrement on reconciliation and recovery rebuild.
- Categorization of repair vs non-repair calls, retries, and structured output.

Phase 5:
- Safe release of undispatched reservations on worker crash.
- Conservative charging of dispatched reservations when worker dies.
- Restoration prevention (replacement worker cannot reuse capacity consumed by uncertain attempt).
- Idempotent recovery (repeated recovery does not double charge).
- Late reconcile (success/failure/timeout) after recovery does not double charge.
- Dispatched lost repair attempts count against max_repair_calls.
- Known vs unknown price handling in conservative recovery.
- Dispatch transition state machine direct validation and error handling.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from typing import Any, List

import pytest
from sqlmodel import Session

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.db.models import AutonomousNovelJob, BudgetReservation
from app.db.session import engine
from app.services.autonomous import budget
from app.services.autonomous.model_client import ProviderResult
from tests.test_autonomous_durability import Tiny, _client, _job

pytestmark = pytest.mark.timeout(300)


# ==============================================================================
# Phase 4: Concurrency-Safe Repair Reservations
# ==============================================================================

def test_concurrent_repair_reservations_race_exactly_one_winner(app_client):
    """Two threads concurrently race to reserve repair_editor with max_repair_calls=1, max_calls=10.

    Proves:
    1. Exactly one reservation succeeds.
    2. Exactly one receives BudgetExceeded.
    3. The job's reserved_repair_calls is atomically 1, not 2.
    """
    with Session(engine) as s:
        job = _job(s, budget={"max_repair_calls": 1, "max_calls": 10})
        jid = job.id

    barrier = threading.Barrier(2)
    results: List[Any] = []
    errors: List[Exception] = []

    def attempt_reserve():
        with Session(engine) as s:
            barrier.wait()
            try:
                r = budget.reserve(
                    s,
                    jid,
                    role="repair_editor",
                    stage="GLOBAL_REPAIR:ch1",
                    estimated_input_tokens=10,
                    requested_output_tokens=10,
                    min_output_tokens=1,
                )
                results.append(r)
            except Exception as exc:
                errors.append(exc)

    t1 = threading.Thread(target=attempt_reserve)
    t2 = threading.Thread(target=attempt_reserve)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert len(results) == 1, f"Expected exactly 1 winner, got {len(results)}"
    assert len(errors) == 1, f"Expected exactly 1 BudgetExceeded error, got {len(errors)}"
    assert isinstance(errors[0], budget.BudgetExceeded)
    assert "max_repair_calls 1 reached" in str(errors[0])

    with Session(engine) as s:
        j = s.get(AutonomousNovelJob, jid)
        assert j.reserved_repair_calls == 1
        assert j.reserved_calls == 1

        # Reconciling the winner decrements reserved_repair_calls and increments repair_calls
        budget.reconcile(s, results[0], input_tokens=10, output_tokens=10, succeeded=True)
        s.refresh(j)
        assert j.reserved_repair_calls == 0
        assert j.repair_calls == 1

        # Subsequent repair attempt is refused even though max_calls (10) has plenty of room
        with pytest.raises(budget.BudgetExceeded) as exc_info:
            budget.reserve(
                s,
                jid,
                role="repair_editor",
                stage="GLOBAL_REPAIR:ch2",
                estimated_input_tokens=5,
                requested_output_tokens=5,
                min_output_tokens=1,
            )
        assert "max_repair_calls 1 reached" in str(exc_info.value)

        # Normal non-repair role can still reserve fine
        r_norm = budget.reserve(
            s,
            jid,
            role="drafter",
            stage="DRAFT:ch1",
            estimated_input_tokens=5,
            requested_output_tokens=5,
            min_output_tokens=1,
        )
        assert r_norm is not None
        s.refresh(j)
        assert j.reserved_calls == 1
        assert j.reserved_repair_calls == 0  # Non-repair calls do NOT increment reserved_repair_calls


def test_concurrent_client_repair_dispatch_boundary_reached_at_most_once(app_client):
    """When two concurrent client calls race with max_repair_calls=1, at most one provider dispatch occurs."""
    provider_dispatches = []
    lock = threading.Lock()

    async def fake_provider(**kw):
        with lock:
            provider_dispatches.append(time.monotonic())
        await asyncio.sleep(0.05)
        return ProviderResult(content="fixed prose", input_tokens=10, output_tokens=10)

    with Session(engine) as s:
        job = _job(s, budget={"max_repair_calls": 1, "max_calls": 10})
        jid = job.id

    barrier = threading.Barrier(2)
    outcomes: List[str] = []

    def worker_thread():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            with Session(engine) as s:
                j = s.get(AutonomousNovelJob, jid)
                client = _client(s, j, fake_provider)
                barrier.wait()
                loop.run_until_complete(
                    client.text(
                        role="repair_editor",
                        system_prompt="repair sys",
                        user_prompt="repair prompt",
                        prompt_version="p1",
                        stage="REPAIR",
                    )
                )
                outcomes.append("success")
        except budget.BudgetExceeded:
            outcomes.append("budget_refused")
        finally:
            loop.close()

    t1 = threading.Thread(target=worker_thread)
    t2 = threading.Thread(target=worker_thread)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert outcomes.count("success") == 1
    assert outcomes.count("budget_refused") == 1
    assert len(provider_dispatches) == 1, "Provider dispatch boundary must be reached at most once"


def test_repair_counters_rebuild_correctly(app_client):
    """Recovery recomputes reserved_repair_calls from active ledger rows."""
    with Session(engine) as s:
        job = _job(s, budget={"max_repair_calls": 5, "max_calls": 10})
        budget.reserve(s, job.id, role="repair_editor", stage="R1", estimated_input_tokens=10, requested_output_tokens=50)
        budget.reserve(s, job.id, role="whole_novel_editor", stage="R2", estimated_input_tokens=10, requested_output_tokens=50)
        budget.reserve(s, job.id, role="drafter", stage="D1", estimated_input_tokens=10, requested_output_tokens=50)

        s.refresh(job)
        assert job.reserved_repair_calls == 2
        assert job.reserved_calls == 3

        # Corrupt/zero out the cached job counter
        job.reserved_repair_calls = 0
        job.reserved_calls = 0
        s.add(job)
        s.commit()

        # Rebuild restores the correct count of repair reservations
        counters = budget.rebuild_reserved_counters(s, job.id)
        assert counters["reserved_repair_calls"] == 2
        assert counters["reserved_calls"] == 3

        s.refresh(job)
        assert job.reserved_repair_calls == 2
        assert job.reserved_calls == 3


def test_retries_and_structured_output_categorization(app_client):
    """Normal retries on schema error do not consume max_repair_calls; only repair roles do."""
    calls_made = []

    async def flaky_provider(**kw):
        calls_made.append(1)
        if len(calls_made) == 1:
            return ProviderResult(content="not json", input_tokens=10, output_tokens=10)
        return ProviderResult(content='{"ok": true, "text": "parsed"}', input_tokens=10, output_tokens=10)

    with Session(engine) as s:
        job = _job(s, budget={"max_repair_calls": 1, "max_calls": 10})
        client = _client(s, job, flaky_provider)

        # Structured retry for non-repair role succeeds even with a retry
        res = asyncio.run(
            client.structured(
                role="prose_writer",
                schema=Tiny,
                system_prompt="sys",
                user_prompt="usr",
                prompt_version="v1",
                stage="DRAFT",
            )
        )
        assert res.ok is True
        s.refresh(job)
        assert job.model_calls == 2  # 2 provider calls made (initial + retry)
        assert job.repair_calls == 0  # retry was for prose_writer, NOT a repair role
        assert job.reserved_repair_calls == 0

        # Now an actual repair role can make 1 repair call
        r1 = budget.reserve(s, job.id, role="repair_editor", stage="REPAIR", estimated_input_tokens=10, requested_output_tokens=50)
        budget.reconcile(s, r1, input_tokens=10, output_tokens=50, succeeded=True)
        s.refresh(job)
        assert job.repair_calls == 1

        # A 2nd repair attempt is refused because max_repair_calls (1) is reached
        with pytest.raises(budget.BudgetExceeded) as exc_info:
            budget.reserve(s, job.id, role="repair_editor", stage="REPAIR", estimated_input_tokens=10, requested_output_tokens=50)
        assert "max_repair_calls 1 reached" in str(exc_info.value)


# ==============================================================================
# Phase 5: Conservative Accounting for In-Flight / Abandoned Reservations
# ==============================================================================

def test_undispatched_reservation_is_safely_released_on_worker_crash(app_client):
    """When reservation is created but provider dispatch did not start, recovery safely releases it."""
    with Session(engine) as s:
        job = _job(s, budget={"max_calls": 2, "max_total_tokens": 200})
        r = budget.reserve(s, job.id, role="drafter", stage="D1", estimated_input_tokens=50, requested_output_tokens=50)
        assert not r.dispatched

        s.refresh(job)
        assert job.reserved_calls == 1
        assert job.reserved_tokens == 100

        # Simulate crash / stale recovery
        cleared = budget.clear_reservations(s, job.id)
        assert cleared == 1

        s.refresh(job)
        # Undispatched reservation is released: no charges to job model_calls or tokens
        assert job.reserved_calls == 0
        assert job.reserved_tokens == 0
        assert job.model_calls == 0
        assert job.input_tokens == 0
        assert job.output_tokens == 0

        row = s.get(BudgetReservation, r.id)
        assert row.status == "released"
        assert row.closed_at is not None


def test_dispatched_reservation_is_conservatively_charged_when_worker_dies(app_client):
    """When provider dispatch was marked and worker dies, recovery conservatively charges worst-case."""
    with Session(engine) as s:
        job = _job(
            s,
            budget={
                "max_calls": 2,
                "max_total_tokens": 500,
                "max_cost_usd": 10.0,
                "price_per_million": {"input": 2.0, "output": 8.0},
            },
        )
        r = budget.reserve(s, job.id, role="drafter", stage="D1", estimated_input_tokens=100, requested_output_tokens=200)
        budget.mark_dispatched(s, r)
        assert r.dispatched

        s.refresh(job)
        assert job.reserved_calls == 1
        assert job.reserved_tokens == 300
        expected_cost = round(100 / 1e6 * 2.0 + 200 / 1e6 * 8.0, budget.COST_PRECISION)

        # Worker dies during network flight; recovery runs
        cleared = budget.clear_reservations(s, job.id)
        assert cleared == 1

        s.refresh(job)
        # Dispatched reservation is conservatively charged
        assert job.reserved_calls == 0
        assert job.reserved_tokens == 0
        assert job.model_calls == 1
        assert job.input_tokens == 100
        assert job.output_tokens == 200  # full reserved output allowance charged
        assert job.cost_usd == expected_cost
        assert job.usage_estimated_calls == 1
        assert job.cost_unknown_calls == 0

        row = s.get(BudgetReservation, r.id)
        assert row.status == "uncertain_charged"
        assert row.charged_input_tokens == 100
        assert row.charged_output_tokens == 200
        assert row.charged_cost_usd == expected_cost
        assert row.succeeded is False


def test_replacement_worker_cannot_reuse_uncertain_charged_capacity(app_client):
    """A replacement worker cannot exceed budget after an uncertain attempt was charged."""
    with Session(engine) as s:
        # Budget strictly allows only 1 call
        job = _job(s, budget={"max_calls": 1, "max_total_tokens": 500})
        r = budget.reserve(s, job.id, role="drafter", stage="D1", estimated_input_tokens=100, requested_output_tokens=100)
        budget.mark_dispatched(s, r)

        # Worker dies and recovery charges the attempt
        budget.clear_reservations(s, job.id)
        s.refresh(job)
        assert job.model_calls == 1

        # Replacement worker attempts to reserve a call: must be refused!
        with pytest.raises(budget.BudgetExceeded) as exc_info:
            budget.reserve(s, job.id, role="drafter", stage="D1", estimated_input_tokens=50, requested_output_tokens=50)
        assert "max_calls 1 reached" in str(exc_info.value)


def test_late_response_after_recovery_does_not_double_charge_or_release(app_client):
    """Late reconcile (success or failure) on an already recovered reservation is a safe no-op."""
    with Session(engine) as s:
        job = _job(s, budget={"max_calls": 3, "max_total_tokens": 1000})
        r = budget.reserve(s, job.id, role="drafter", stage="D1", estimated_input_tokens=100, requested_output_tokens=200)
        budget.mark_dispatched(s, r)

        # Recovery marks uncertain_charged
        budget.clear_reservations(s, job.id)
        s.refresh(job)
        assert job.model_calls == 1
        assert job.input_tokens == 100
        assert job.output_tokens == 200

        # Late provider response arrives with actual usage (50 in, 80 out)
        budget.reconcile(s, r, input_tokens=50, output_tokens=80, succeeded=True)
        s.refresh(job)

        # Must not change job counters (no double charge, no reduction)
        assert job.model_calls == 1
        assert job.input_tokens == 100
        assert job.output_tokens == 200

        # Late failure arrival also does not alter state
        budget.reconcile(s, r, input_tokens=50, output_tokens=80, succeeded=False, output_known=False)
        s.refresh(job)
        assert job.model_calls == 1
        assert job.input_tokens == 100
        assert job.output_tokens == 200


def test_recovery_is_idempotent(app_client):
    """Calling recovery multiple times does not alter accounting or duplicate charges."""
    with Session(engine) as s:
        job = _job(s, budget={"max_calls": 5, "max_total_tokens": 1000})
        r1 = budget.reserve(s, job.id, role="drafter", stage="D1", estimated_input_tokens=50, requested_output_tokens=50)
        budget.reserve(s, job.id, role="drafter", stage="D2", estimated_input_tokens=50, requested_output_tokens=50)
        budget.mark_dispatched(s, r1)
        # r2 remains open (undispatched)

        c1 = budget.clear_reservations(s, job.id)
        assert c1 == 2
        s.refresh(job)
        calls_first = job.model_calls
        tokens_first = job.input_tokens + job.output_tokens

        # Second recovery call
        c2 = budget.clear_reservations(s, job.id)
        assert c2 == 0
        s.refresh(job)
        assert job.model_calls == calls_first
        assert (job.input_tokens + job.output_tokens) == tokens_first


def test_dispatched_repair_attempt_lost_counts_against_max_repair_calls(app_client):
    """If a repair call is lost after dispatch, it is conservatively counted as a spent repair call."""
    with Session(engine) as s:
        job = _job(s, budget={"max_repair_calls": 1, "max_calls": 5})
        r = budget.reserve(s, job.id, role="repair_editor", stage="R1", estimated_input_tokens=10, requested_output_tokens=50)
        budget.mark_dispatched(s, r)

        # Worker crashes
        budget.clear_reservations(s, job.id)
        s.refresh(job)
        assert job.repair_calls == 1

        # Subsequent repair attempt is refused
        with pytest.raises(budget.BudgetExceeded) as exc_info:
            budget.reserve(s, job.id, role="repair_editor", stage="R2", estimated_input_tokens=10, requested_output_tokens=50)
        assert "max_repair_calls 1 reached" in str(exc_info.value)


def test_unknown_price_lost_reservation_marks_unknown_cost_never_zero(app_client):
    """When pricing is not configured and a dispatched attempt is lost, cost is marked unknown, not zero."""
    with Session(engine) as s:
        # Job without price table
        job = _job(s, budget={"max_calls": 2, "max_total_tokens": 500})
        r = budget.reserve(s, job.id, role="drafter", stage="D1", estimated_input_tokens=50, requested_output_tokens=50)
        budget.mark_dispatched(s, r)

        budget.clear_reservations(s, job.id)
        s.refresh(job)
        assert job.cost_unknown_calls == 1

        snap = budget.usage_snapshot(s, job)
        assert snap["cost_usd"]["known"] is None
        assert snap["cost_usd"]["status"] == "unknown"
        assert snap["estimated_cost_usd"] is None


def test_dispatch_transition_state_machine_validation(app_client):
    """Direct testing of mark_dispatched and release: idempotency and invalid state transitions."""
    with Session(engine) as s:
        job = _job(s)
        r = budget.reserve(s, job.id, role="drafter", stage="D1", estimated_input_tokens=20, requested_output_tokens=20)

        # Valid transition: open -> dispatched
        budget.mark_dispatched(s, r)
        assert r.dispatched is True

        # Idempotent mark_dispatched
        budget.mark_dispatched(s, r)
        assert r.dispatched is True

        row = s.get(BudgetReservation, r.id)
        assert row.status == "dispatched"
        assert row.dispatched_at is not None

        # Cannot release a dispatched reservation directly via release()
        # release() is for reservations never dispatched
        # If attempted when row is dispatched, release gracefully checks row.status == 'open'
        # and ignores non-open rows
        budget.release(s, r)
        row = s.get(BudgetReservation, r.id)
        assert row.status == "dispatched"  # Not altered to released

        # Create another reservation to test release
        r2 = budget.reserve(s, job.id, role="drafter", stage="D2", estimated_input_tokens=20, requested_output_tokens=20)
        budget.release(s, r2)
        assert r2.released is True

        row2 = s.get(BudgetReservation, r2.id)
        assert row2.status == "released"

        # Cannot mark_dispatched an already released reservation
        r2.dispatched = False  # force check on DB status
        with pytest.raises(budget.BudgetAccountingError) as exc_info:
            budget.mark_dispatched(s, r2)
        assert "cannot dispatch reservation" in str(exc_info.value)
