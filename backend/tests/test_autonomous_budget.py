"""Hard budgets: worst-case reservation, clamping, per-attempt accounting, cost caps, concurrency, recovery."""

from __future__ import annotations

import asyncio
import os
import sys
import threading
from typing import Any, Dict, List

import pytest
from sqlmodel import Session, select

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tests.test_autonomous_durability import Tiny, _client, _job  # noqa: E402

pytestmark = pytest.mark.timeout(300)


def _reserve(s, jid, **kw):
    from app.services.autonomous import budget

    kw.setdefault("role", "drafter")
    kw.setdefault("stage", "X")
    kw.setdefault("min_output_tokens", 1)
    return budget.reserve(s, jid, **kw)


class _Counter:
    def __init__(self):
        self.n = 0


# ------------------------------------------------------------ boundaries
def test_exact_boundary_accepted_and_one_token_over_rejected(app_client):
    from app.db.session import engine
    from app.services.autonomous import budget

    with Session(engine) as s:
        job = _job(s, budget={"max_total_tokens": 1000, "max_input_tokens": 600, "max_output_tokens": 500})
        r = _reserve(s, job.id, estimated_input_tokens=600, requested_output_tokens=400)  # 600 + 400 == 1000 exactly
        assert r.input_tokens == 600 and r.max_output_tokens == 400
        budget.reconcile(s, r, input_tokens=600, output_tokens=400, succeeded=True)
        with pytest.raises(budget.BudgetExceeded):
            _reserve(s, job.id, estimated_input_tokens=0, requested_output_tokens=1)
        job2 = _job(s, budget={"max_input_tokens": 100})
        with pytest.raises(budget.BudgetExceeded) as exc:
            _reserve(s, job2.id, estimated_input_tokens=101, requested_output_tokens=10)
        assert "max_input_tokens" in str(exc.value)
        r2 = _reserve(s, job2.id, estimated_input_tokens=100, requested_output_tokens=10)
        assert r2.input_tokens == 100


def test_output_clamped_to_remaining_allowance(app_client):
    from app.db.session import engine
    from app.services.autonomous import budget

    with Session(engine) as s:
        job = _job(s, budget={"max_output_tokens": 300})
        r1 = _reserve(s, job.id, estimated_input_tokens=50, requested_output_tokens=1000)
        assert r1.max_output_tokens == 300
        budget.reconcile(s, r1, input_tokens=50, output_tokens=120, succeeded=True)
        r2 = _reserve(s, job.id, estimated_input_tokens=50, requested_output_tokens=1000)
        assert r2.max_output_tokens == 180
        budget.reconcile(s, r2, input_tokens=50, output_tokens=180, succeeded=True)
        with pytest.raises(budget.BudgetExceeded) as exc:
            _reserve(s, job.id, estimated_input_tokens=50, requested_output_tokens=1000)
        assert "output tokens" in str(exc.value)
        job2 = _job(s, budget={"max_total_tokens": 500})
        r3 = _reserve(s, job2.id, estimated_input_tokens=200, requested_output_tokens=10_000)
        assert r3.max_output_tokens == 300
        with pytest.raises(budget.BudgetExceeded):
            budget.reserve(s, job2.id, role="drafter", stage="Y", estimated_input_tokens=100, requested_output_tokens=100, min_output_tokens=64)


def test_unlimited_is_zero_and_snapshot_shape(app_client):
    from app.db.session import engine
    from app.services.autonomous import budget

    with Session(engine) as s:
        job = _job(s, budget={"max_calls": 0, "max_total_tokens": 0, "max_output_tokens": 0, "max_input_tokens": 0, "max_cost_usd": 0})
        r = _reserve(s, job.id, estimated_input_tokens=10_000_000, requested_output_tokens=10_000_000)
        assert r.max_output_tokens == 10_000_000
        budget.reconcile(s, r, input_tokens=5, output_tokens=5, succeeded=True)
        snap = budget.usage_snapshot(s, job)
        assert snap["calls"]["limit"] == 0 and snap["total_tokens"]["limit"] == 0 and snap["cost_usd"]["known"] is None and snap["estimated_cost_usd"] is None


# ---------------------------------------------------- attempts are charged
def test_retries_schema_repair_and_fallback_each_count_as_attempts(app_client):
    from app.db.models import LLMConfig
    from app.db.session import engine
    from app.services.autonomous import budget
    from app.services.autonomous.model_client import ProviderResult

    calls: List[Dict[str, Any]] = []

    async def provider(**kw):
        calls.append(kw)
        n = len(calls)
        if n == 1:
            return ProviderResult(content={"ok": "nope"}, input_tokens=10, output_tokens=4)  # schema repair follows
        if n == 2:
            raise RuntimeError("HTTP 503 service unavailable")  # provider retry -> fallback
        return ProviderResult(content={"ok": True, "text": "fine"}, input_tokens=10, output_tokens=6)

    with Session(engine) as s:
        fb = LLMConfig(provider="openai_compatible", model_name="fallback-model", api_key="k", api_base="https://fb.example/v1")
        s.add(fb)
        s.commit()
        s.refresh(fb)
        job = _job(s, budget={"max_calls": 3})
        client = _client(s, job, provider, fallback_llm_config_id=fb.id)
        result = asyncio.run(client.structured(role="claim_extractor", schema=Tiny, system_prompt="sys", user_prompt="user", prompt_version="p1", stage="T"))
        assert result.ok is True and len(calls) == 3 and calls[2]["llm_config_id"] == fb.id
        s.refresh(job)
        assert job.model_calls == 3
        snap = budget.usage_snapshot(s, job)
        assert snap["calls"]["used"] == 3 and snap["calls"]["reserved"] == 0 and snap["total_tokens"]["reserved"] == 0
        n_before = len(calls)
        with pytest.raises(budget.BudgetExceeded):
            asyncio.run(client.text(role="claim_extractor", system_prompt="s", user_prompt="u", prompt_version="p1", stage="T"))
        assert len(calls) == n_before


def test_failed_and_timed_out_attempts_are_charged_conservatively(app_client):
    from app.db.models import BudgetReservation, ModelInvocationAttempt
    from app.db.session import engine
    from app.services.autonomous import budget
    from app.services.autonomous.model_client import ROLE_POLICIES

    async def timeout_provider(**kw):
        raise TimeoutError("request timed out")

    with Session(engine) as s:
        job = _job(s, budget={})
        client = _client(s, job, timeout_provider)
        with pytest.raises(Exception):
            asyncio.run(client.text(role="claim_extractor", system_prompt="s", user_prompt="u", prompt_version="p1", stage="T"))
        s.refresh(job)
        attempts = ROLE_POLICIES["claim_extractor"].max_retries + 1
        assert job.model_calls == attempts
        rows = s.exec(select(BudgetReservation).where(BudgetReservation.job_id == job.id)).all()
        assert len(rows) == attempts
        assert all(r.status == "closed" and r.usage_reported is False and r.charged_output_tokens == r.reserved_output_tokens for r in rows)
        assert job.output_tokens == sum(r.reserved_output_tokens for r in rows)
        assert job.usage_estimated_calls == attempts and job.reserved_calls == 0 and job.reserved_tokens == 0
        att = s.exec(select(ModelInvocationAttempt).where(ModelInvocationAttempt.job_id == job.id)).all()
        assert all(a.status == "timeout" and a.usage_reported is False and a.max_tokens is not None for a in att)

    with Session(engine) as s:
        job = _job(s, budget={"max_output_tokens": 1000})
        client = _client(s, job, timeout_provider)
        with pytest.raises(budget.BudgetExceeded):
            asyncio.run(client.text(role="claim_extractor", system_prompt="s", user_prompt="u", prompt_version="p1", stage="T"))
        s.refresh(job)
        assert job.model_calls == 1 and job.output_tokens == 1000
        att = s.exec(select(ModelInvocationAttempt).where(ModelInvocationAttempt.job_id == job.id).order_by(ModelInvocationAttempt.attempt)).all()
        assert [a.status for a in att] == ["timeout", "budget_refused"] and att[0].max_tokens == 1000

    async def failing_provider(**kw):
        raise RuntimeError("HTTP 500 internal error")

    with Session(engine) as s:
        job = _job(s, budget={})
        client = _client(s, job, failing_provider)
        with pytest.raises(Exception):
            asyncio.run(client.text(role="style_evaluator", system_prompt="s", user_prompt="u", prompt_version="p1", stage="T"))
        s.refresh(job)
        assert job.model_calls == ROLE_POLICIES["style_evaluator"].max_retries + 1 and job.input_tokens > 0


def test_missing_usage_metadata_charges_estimates_and_reports_estimated(app_client):
    from app.db.session import engine
    from app.services.autonomous import budget
    from app.services.autonomous.model_client import ProviderResult

    async def provider(**kw):
        return ProviderResult(content="some generated words here", usage_reported=False)

    with Session(engine) as s:
        job = _job(s, budget={"price_per_million": {"input": 1.0, "output": 2.0}})
        client = _client(s, job, provider)
        asyncio.run(client.text(role="style_evaluator", system_prompt="s", user_prompt="u", prompt_version="p1", stage="T"))
        s.refresh(job)
        assert job.usage_estimated_calls == 1 and job.input_tokens > 0 and job.output_tokens > 0
        snap = budget.usage_snapshot(s, job)
        assert snap["cost_usd"]["status"] == "estimated" and snap["cost_usd"]["known"] is not None and snap["cost_usd"]["known"] > 0


# -------------------------------------------------------------------- cost
def test_cost_cap_reserved_before_call_with_distinct_prices(app_client):
    from app.db.models import LLMConfig
    from app.db.session import engine
    from app.services.autonomous import budget

    with Session(engine) as s:
        fb = LLMConfig(provider="openai_compatible", model_name="fallback-pricey", api_key="k", api_base="https://fb.example/v1")
        s.add(fb)
        s.commit()
        s.refresh(fb)
        primary_id = _job(s).llm_config_id
        job = _job(s, budget={"max_cost_usd": 1.0, "prices": {str(primary_id): {"input": 1.0, "output": 10.0}, str(fb.id): {"input": 10.0, "output": 100.0}}})
        r = _reserve(s, job.id, estimated_input_tokens=10_000, requested_output_tokens=1_000_000, llm_config_id=primary_id)
        assert r.max_output_tokens == 99_000 and abs(r.cost_usd - 1.0) < 1e-6  # (1.0 - 0.01) / 10 per M
        budget.reconcile(s, r, input_tokens=10_000, output_tokens=50_000, succeeded=True)  # 0.01 + 0.5
        s.refresh(job)
        assert abs(job.cost_usd - 0.51) < 1e-6 and job.reserved_cost_usd == 0
        r2 = _reserve(s, job.id, estimated_input_tokens=10_000, requested_output_tokens=1_000_000, llm_config_id=fb.id)
        assert r2.max_output_tokens == 3_900  # (0.49 - 0.1) / 100 per M
        budget.reconcile(s, r2, input_tokens=10_000, output_tokens=3_900, succeeded=True)
        s.refresh(job)
        assert abs(job.cost_usd - 1.0) < 1e-6
        with pytest.raises(budget.BudgetExceeded):
            _reserve(s, job.id, estimated_input_tokens=1, requested_output_tokens=1, llm_config_id=primary_id)
        snap = budget.usage_snapshot(s, job)
        assert snap["cost_usd"]["status"] == "reported" and abs(snap["cost_usd"]["known"] - 1.0) < 1e-6


def test_cost_cap_without_price_refuses_and_unknown_cost_is_never_zero(app_client):
    from app.db.session import engine
    from app.services.autonomous import budget

    with Session(engine) as s:
        job = _job(s, budget={"max_cost_usd": 5.0})
        with pytest.raises(budget.BudgetExceeded) as exc:
            _reserve(s, job.id, estimated_input_tokens=10, requested_output_tokens=10, llm_config_id=job.llm_config_id)
        assert "no price table" in str(exc.value)
        assert budget.validate_budget_spec({"max_cost_usd": 5.0}) and not budget.validate_budget_spec({"max_cost_usd": 5.0, "price_per_million": {"input": 1, "output": 2}})
        job2 = _job(s, budget={"prices": {str(job.llm_config_id): {"input": 1.0, "output": 1.0}}})
        r = _reserve(s, job2.id, estimated_input_tokens=10, requested_output_tokens=10, llm_config_id=job.llm_config_id)
        budget.reconcile(s, r, input_tokens=10, output_tokens=10, succeeded=True)
        r = _reserve(s, job2.id, estimated_input_tokens=10, requested_output_tokens=10, llm_config_id=999_999)
        budget.reconcile(s, r, input_tokens=10, output_tokens=10, succeeded=True)
        s.refresh(job2)
        assert job2.cost_unknown_calls == 1 and budget.estimate_cost(job2) is None
        assert budget.usage_snapshot(s, job2)["cost_usd"]["status"] == "unknown"


# ------------------------------------------------------------- concurrency
def test_concurrent_reservations_cannot_jointly_overspend(app_client):
    from app.db.models import AutonomousNovelJob
    from app.db.session import engine
    from app.services.autonomous import budget

    with Session(engine) as s:
        job = _job(s, budget={"max_total_tokens": 1000, "max_calls": 0})
        jid = job.id
    granted: List[int] = []
    refused = _Counter()
    lock = threading.Lock()
    barrier = threading.Barrier(12)

    def worker() -> None:
        with Session(engine) as s:
            barrier.wait()
            try:
                r = budget.reserve(s, jid, role="drafter", stage="X", estimated_input_tokens=100, requested_output_tokens=200, min_output_tokens=50)
                with lock:
                    granted.append(r.tokens)
            except budget.BudgetExceeded:
                with lock:
                    refused.n += 1

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(granted) <= 1000 and refused.n >= 1 and len(granted) + refused.n == 12
    with Session(engine) as s:
        job = s.get(AutonomousNovelJob, jid)
        s.refresh(job)
        assert job.reserved_tokens == sum(granted) and job.reserved_calls == len(granted)


def test_concurrent_client_calls_share_one_ceiling(app_client):
    from app.db.session import engine
    from app.services.autonomous import budget
    from app.services.autonomous.model_client import ProviderResult

    started = _Counter()

    async def provider(**kw):
        started.n += 1
        await asyncio.sleep(0.05)
        return ProviderResult(content="ok", input_tokens=10, output_tokens=kw["max_tokens"])

    with Session(engine) as s:
        job = _job(s, budget={"max_calls": 4})
        client = _client(s, job, provider)

        async def many():
            tasks = [client.text(role="style_evaluator", system_prompt="s", user_prompt=f"u{i}", prompt_version="p", stage="T") for i in range(8)]
            return await asyncio.gather(*tasks, return_exceptions=True)

        results = asyncio.run(many())
        ok = [r for r in results if isinstance(r, str)]
        refused = [r for r in results if isinstance(r, budget.BudgetExceeded)]
        assert len(ok) == 4 and len(refused) == 4 and started.n == 4
        s.refresh(job)
        assert job.model_calls == 4 and job.reserved_calls == 0


# ------------------------------------------------------------ stage/chapter
def test_stage_and_chapter_limits_include_in_flight_reservations(app_client):
    from app.db.session import engine
    from app.services.autonomous import budget

    with Session(engine) as s:
        job = _job(s, budget={"stage_limits": {"CHAPTER_GENERATION_LOOP": {"max_calls": 3, "max_total_tokens": 600}}, "chapter_limits": {"max_calls_per_chapter": 2, "max_total_tokens_per_chapter": 400}})
        r1 = _reserve(s, job.id, stage="CHAPTER_GENERATION_LOOP:ch1", estimated_input_tokens=100, requested_output_tokens=250)
        assert r1.max_output_tokens == 250  # fits under the chapter token limit 400
        r2 = _reserve(s, job.id, stage="CHAPTER_GENERATION_LOOP:ch1", estimated_input_tokens=10, requested_output_tokens=1000)
        assert r2.max_output_tokens == 400 - 350 - 10  # clamped by the in-flight r1 reservation
        with pytest.raises(budget.BudgetExceeded) as exc:
            _reserve(s, job.id, stage="CHAPTER_GENERATION_LOOP:ch1", estimated_input_tokens=1, requested_output_tokens=1)
        assert "chapter limit" in str(exc.value)
        r3 = _reserve(s, job.id, stage="CHAPTER_GENERATION_LOOP:ch2", estimated_input_tokens=10, requested_output_tokens=1000)
        assert r3.max_output_tokens == 600 - 400 - 10  # stage token cap minus in-flight ch1 reservations (400)
        with pytest.raises(budget.BudgetExceeded) as exc:
            _reserve(s, job.id, stage="CHAPTER_GENERATION_LOOP:ch2", estimated_input_tokens=1, requested_output_tokens=1)
        assert "stage CHAPTER_GENERATION_LOOP max_calls" in str(exc.value)
        for r in (r1, r2, r3):
            budget.reconcile(s, r, input_tokens=1, output_tokens=1, succeeded=True)
        _reserve(s, job.id, stage="WHOLE_NOVEL_AUDIT", estimated_input_tokens=1, requested_output_tokens=1)


# ---------------------------------------------------------------- recovery
def test_restart_with_stale_reservations_is_repaired_deterministically(app_client):
    from app.db.models import BudgetReservation
    from app.db.session import engine
    from app.services.autonomous import budget
    from app.services.autonomous import runner as runner_mod

    with Session(engine) as s:
        job = _job(s, budget={"max_calls": 2, "max_total_tokens": 100})
        r1 = _reserve(s, job.id, estimated_input_tokens=40, requested_output_tokens=40)
        _reserve(s, job.id, estimated_input_tokens=10, requested_output_tokens=10)
        s.refresh(job)
        assert job.reserved_calls == 2 and job.reserved_tokens == 100
        with pytest.raises(budget.BudgetExceeded):
            _reserve(s, job.id, estimated_input_tokens=1, requested_output_tokens=1)
        job.status = "running"
        job.lease_owner = "dead"
        job.lease_expires_at = None
        s.add(job)
        s.commit()
        assert runner_mod.recover_stale_leases(s) == 1
        s.refresh(job)
        assert job.reserved_calls == 0 and job.reserved_tokens == 0 and job.reserved_input_tokens == 0 and job.reserved_output_tokens == 0
        rows = s.exec(select(BudgetReservation).where(BudgetReservation.job_id == job.id)).all()
        assert sorted(r.status for r in rows) == ["released", "released"]
        budget.reconcile(s, r1, input_tokens=40, output_tokens=40, succeeded=True)  # late reconcile is a no-op
        s.refresh(job)
        assert job.model_calls == 0 and job.reserved_calls == 0 and job.input_tokens == 0
        r3 = _reserve(s, job.id, estimated_input_tokens=40, requested_output_tokens=40)
        budget.reconcile(s, r3, input_tokens=40, output_tokens=40, succeeded=True)
        budget.reconcile(s, r3, input_tokens=40, output_tokens=40, succeeded=True)  # idempotent
        s.refresh(job)
        assert job.model_calls == 1 and job.input_tokens == 40
        assert budget.rebuild_reserved_counters(s, job.id) == {"reserved_calls": 0, "reserved_repair_calls": 0, "reserved_input_tokens": 0, "reserved_output_tokens": 0, "reserved_cost_usd": 0.0, "reserved_tokens": 0}


def test_accounting_failure_is_surfaced_not_swallowed(app_client):
    from app.db.session import engine
    from app.services.autonomous import budget
    from app.services.autonomous.model_client import ProviderResult

    async def provider(**kw):
        return ProviderResult(content="ok", input_tokens=1, output_tokens=1)

    class BrokenSession(Session):
        def execute(self, *a, **kw):  # type: ignore[override]
            stmt = str(a[0]) if a else ""
            # Only the reconcile UPDATE (which increments model_calls) fails; reservation itself succeeds.
            if "UPDATE" in stmt.upper() and "model_calls + " in stmt.replace(":", " ").replace("autonomousnoveljob.", ""):
                raise RuntimeError("disk full")
            return super().execute(*a, **kw)

    with Session(engine) as s:
        job = _job(s)
        client = _client(s, job, provider)
        client._budget_factory = lambda: BrokenSession(engine)
        with pytest.raises(budget.BudgetAccountingError):
            asyncio.run(client.text(role="style_evaluator", system_prompt="s", user_prompt="u", prompt_version="p", stage="T"))
        s.refresh(job)
        assert job.reserved_calls == 1  # conservative: stays open until recovery abandons it
        assert budget.clear_reservations(s, job.id) == 1
        s.refresh(job)
        assert job.reserved_calls == 0


def test_no_provider_call_after_reservation_refusal(app_client):
    from app.db.models import ModelInvocationAttempt
    from app.db.session import engine
    from app.services.autonomous import budget

    hits = _Counter()

    async def provider(**kw):
        hits.n += 1
        raise AssertionError("must not be called")

    with Session(engine) as s:
        job = _job(s, budget={"max_calls": 1})
        r = _reserve(s, job.id, estimated_input_tokens=1, requested_output_tokens=1)
        client = _client(s, job, provider)
        with pytest.raises(budget.BudgetExceeded):
            asyncio.run(client.text(role="style_evaluator", system_prompt="s", user_prompt="u", prompt_version="p", stage="T"))
        assert hits.n == 0
        att = s.exec(select(ModelInvocationAttempt).where(ModelInvocationAttempt.job_id == job.id)).all()
        assert len(att) == 1 and att[0].status == "budget_refused"
        budget.reconcile(s, r, input_tokens=1, output_tokens=1, succeeded=True)
