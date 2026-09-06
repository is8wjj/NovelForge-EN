"""Durability, concurrency, budget, telemetry and recovery-ladder tests for the autonomous pipeline.

Everything here runs against the shared SQLite test database with deterministic
fakes; no provider is contacted. SQLite semantics: writers are serialised, so a
conditional UPDATE's rowcount is authoritative exactly as on PostgreSQL.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta
from typing import Any, Dict, List

import pytest
from pydantic import BaseModel
from sqlmodel import Session, select

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

pytestmark = pytest.mark.timeout(300)


def _job(session: Session, **over: Any):
    from app.db.models import LLMConfig
    from app.services.autonomous import runner as runner_mod

    cfg = session.exec(select(LLMConfig)).first()
    if cfg is None:
        cfg = LLMConfig(provider="authnd", model_name="moonshotai/kimi-k3", api_key="")
        session.add(cfg)
        session.commit()
        session.refresh(cfg)
    payload = os.urandom(8)
    job = runner_mod.create_job(session, filename="x.epub", data=payload, llm_config_id=cfg.id, options=over.pop("options", {}), budget=over.pop("budget", None))
    for k, v in over.items():
        setattr(job, k, v)
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


class Tiny(BaseModel):
    ok: bool
    text: str = ""


# ------------------------------------------------------------------- leases
def test_two_runners_race_one_winner(app_client):
    from app.db.session import engine
    from app.services.autonomous import lease

    with Session(engine) as s:
        job = _job(s)
        a = lease.acquire(s, job.id, "worker-a")
        b = lease.acquire(s, job.id, "worker-b")
        assert a is not None and b is None
        assert a.generation == 1
        # Same owner re-acquires (idempotent) and advances the generation.
        a2 = lease.acquire(s, job.id, "worker-a")
        assert a2 is not None and a2.generation == 2
        # A stale generation cannot publish.
        with pytest.raises(lease.JobLeaseLost):
            lease.fenced_update(s, a, {"progress_message": "stale"})
        lease.fenced_update(s, a2, {"progress_message": "fresh"})
        s.refresh(job)
        assert job.progress_message == "fresh"


def test_expired_owner_is_replaced_and_cannot_publish(app_client):
    from app.db.session import engine
    from app.services.autonomous import lease

    with Session(engine) as s:
        job = _job(s)
        old = lease.acquire(s, job.id, "worker-old", ttl=1)
        assert old is not None
        assert lease.acquire(s, job.id, "worker-new", now=datetime.now()) is None  # still valid
        later = datetime.now() + timedelta(seconds=5)
        new = lease.acquire(s, job.id, "worker-new", now=later)
        assert new is not None and new.generation == old.generation + 1
        with pytest.raises(lease.JobLeaseLost):
            lease.renew(s, old)
        with pytest.raises(lease.JobLeaseLost):
            lease.fenced_update(s, old, {"stage": "DONE"})
        s.refresh(job)
        assert job.stage == "INGEST" and job.lease_owner == "worker-new"


def test_lease_not_granted_for_ineligible_status(app_client):
    from app.db.session import engine
    from app.services.autonomous import lease

    with Session(engine) as s:
        for status in ("waiting_for_user", "paused", "completed", "cancelled"):
            job = _job(s, status=status)
            assert lease.acquire(s, job.id, "w") is None


def test_startup_recovery_leaves_live_leases_alone(app_client):
    from app.db.session import engine
    from app.services.autonomous import lease
    from app.services.autonomous import runner as runner_mod

    with Session(engine) as s:
        live = _job(s)
        dead = _job(s)
        lease.acquire(s, live.id, "alive")
        lease.acquire(s, dead.id, "dead", ttl=1)
        for j in (live, dead):
            s.refresh(j)
            j.status = "running"
            s.add(j)
        s.commit()
        assert runner_mod.recover_stale_leases(s, now=datetime.now() + timedelta(seconds=3)) == 1
        s.refresh(live)
        s.refresh(dead)
        assert live.status == "running" and live.lease_owner == "alive"
        assert dead.status == "queued" and dead.lease_owner is None


def test_heartbeat_renews_then_stops_on_loss(app_client):
    from app.db.session import engine
    from app.services.autonomous import lease

    async def scenario():
        with Session(engine) as s:
            job = _job(s)
            l = lease.acquire(s, job.id, "hb", ttl=2)
            hb = lease.Heartbeat(l, session_factory=lambda: Session(engine), interval=0.05).start()
            await asyncio.sleep(0.3)
            assert hb.renewals >= 3 and hb.running and not hb.lost
            s.refresh(job)
            assert job.lease_expires_at > datetime.now() + timedelta(seconds=1)  # renewal prevented premature recovery
            # While the heartbeat renews, nobody can take the job (renewal window is lease_seconds).
            assert lease.acquire(s, job.id, "thief", now=datetime.now() + timedelta(seconds=10)) is None
            # Once the lease would have lapsed (no renewal reached the DB in time), a new owner takes over.
            assert lease.acquire(s, job.id, "thief", now=datetime.now() + timedelta(seconds=lease.lease_seconds() + 5)) is not None
            await asyncio.sleep(0.3)
            assert hb.lost and not hb.running
            await hb.stop()
            # Heartbeat stops when asked, too.
            l2 = lease.acquire(s, job.id, "thief")
            hb2 = lease.Heartbeat(l2, session_factory=lambda: Session(engine), interval=0.05).start()
            await asyncio.sleep(0.12)
            await hb2.stop()
            assert not hb2.running

    asyncio.run(scenario())


def test_runner_step_raises_lease_lost_and_publishes_nothing(app_client):
    """A runner whose lease was replaced mid-stage must not advance or overwrite the job."""
    from app.db.session import engine
    from app.services.autonomous import lease
    from app.services.autonomous.runner import JobLeaseLost, JobRunner

    async def scenario():
        with Session(engine) as s:
            job = _job(s)
            runner = JobRunner(s, job.id, client_factory=lambda a, b, c: None, owner="slow", heartbeat_interval=60)

            async def slow_stage(j, stage):
                # Another process takes over while we work.
                with Session(engine) as s2:
                    assert lease.acquire(s2, job.id, "fast", now=datetime.now() + timedelta(seconds=1000)) is not None
                    lease.fenced_update(s2, lease.acquire(s2, job.id, "fast", now=datetime.now() + timedelta(seconds=1000)), {"stage": "SOURCE_ANALYSIS", "progress_message": "owned by fast"})
                return {"ok": True}

            runner._run_stage = slow_stage  # type: ignore[method-assign]
            with pytest.raises(JobLeaseLost):
                await runner.step()
            s.expire_all()
            j = s.get(type(job), job.id)
            assert j.stage == "SOURCE_ANALYSIS" and j.progress_message == "owned by fast" and j.lease_owner == "fast"
            attempts = s.exec(select(__import__("app.db.models", fromlist=["JobStageAttempt"]).JobStageAttempt).where(__import__("app.db.models", fromlist=["JobStageAttempt"]).JobStageAttempt.job_id == job.id)).all()
            assert all(a.status == "running" for a in attempts)  # never marked succeeded by the loser

    asyncio.run(scenario())


def test_two_runners_same_job_only_one_advances(app_client):
    from app.db.session import engine
    from app.services.autonomous.runner import JobLeaseLost, JobRunner

    async def scenario():
        with Session(engine) as s1, Session(engine) as s2:
            job = _job(s1)
            r1 = JobRunner(s1, job.id, client_factory=lambda a, b, c: None, owner="r1", heartbeat_interval=60)
            r2 = JobRunner(s2, job.id, client_factory=lambda a, b, c: None, owner="r2", heartbeat_interval=60)
            gate = asyncio.Event()

            async def stage(j, stage):
                await gate.wait()
                return {"ok": True}

            r1._run_stage = stage  # type: ignore[method-assign]
            r2._run_stage = stage  # type: ignore[method-assign]
            t1 = asyncio.create_task(r1.step())
            await asyncio.sleep(0.1)
            t2 = asyncio.create_task(r2.step())
            await asyncio.sleep(0.1)
            gate.set()
            res = await asyncio.gather(t1, t2, return_exceptions=True)
            winners = [r for r in res if not isinstance(r, Exception)]
            losers = [r for r in res if isinstance(r, JobLeaseLost)]
            assert len(winners) == 1 and len(losers) == 1
            s1.expire_all()
            j = s1.get(type(job), job.id)
            assert j.stage == "SOURCE_ANALYSIS" and j.status == "queued"

    asyncio.run(scenario())


# ------------------------------------------------------------------- budget
def test_budget_boundary_and_reconcile(app_client):
    from app.db.session import engine
    from app.services.autonomous import budget

    with Session(engine) as s:
        job = _job(s, budget={"max_calls": 2, "max_total_tokens": 1000})
        r1 = budget.reserve(s, job.id, role="drafter", stage="X", estimated_input_tokens=300, requested_output_tokens=100)
        r2 = budget.reserve(s, job.id, role="drafter", stage="X", estimated_input_tokens=300, requested_output_tokens=100)
        assert r1.tokens == 400 and r2.tokens == 400  # worst case (input + full output) reserved, not half
        with pytest.raises(budget.BudgetExceeded):  # one over max_calls
            budget.reserve(s, job.id, role="drafter", stage="X", estimated_input_tokens=1, requested_output_tokens=1)
        budget.reconcile(s, r1, input_tokens=300, output_tokens=50, succeeded=True)
        budget.reconcile(s, r2, input_tokens=300, output_tokens=50, succeeded=False)  # failed attempt still charged
        s.refresh(job)
        assert job.model_calls == 2 and job.reserved_calls == 0 and job.reserved_tokens == 0 and job.input_tokens == 600 and job.output_tokens == 100
        with pytest.raises(budget.BudgetExceeded):
            budget.reserve(s, job.id, role="drafter", stage="X", estimated_input_tokens=1, requested_output_tokens=1)
        # Raising the budget on resume lets it continue.
        job.budget = {"max_calls": 3, "max_total_tokens": 1000}
        s.add(job)
        s.commit()
        r3 = budget.reserve(s, job.id, role="drafter", stage="X", estimated_input_tokens=200, requested_output_tokens=100, min_output_tokens=1)  # exactly at the token boundary
        assert r3.tokens == 300 and r3.max_output_tokens == 100
        with pytest.raises(budget.BudgetExceeded):
            budget.reserve(s, job.id, role="repair_editor", stage="X", estimated_input_tokens=1, requested_output_tokens=1)
        budget.reconcile(s, r3, input_tokens=1, output_tokens=1, succeeded=True)
        snap = budget.usage_snapshot(s, job)
        assert snap["calls"]["used"] == 3 and snap["estimated_cost_usd"] is None and snap["cost_usd"]["status"] == "unknown"  # cost unknown without a price table


def test_budget_repair_limit_and_concurrent_reservation(app_client):
    from app.db.session import engine
    from app.services.autonomous import budget

    with Session(engine) as s:
        job = _job(s, budget={"max_repair_calls": 1, "max_calls": 5})
        r = budget.reserve(s, job.id, role="repair_editor", stage="GLOBAL_REPAIR:ch1", estimated_input_tokens=5, requested_output_tokens=5, min_output_tokens=1)
        budget.reconcile(s, r, input_tokens=5, output_tokens=5, succeeded=True)
        with pytest.raises(budget.BudgetExceeded):
            budget.reserve(s, job.id, role="repair_editor", stage="GLOBAL_REPAIR:ch2", estimated_input_tokens=5, requested_output_tokens=5, min_output_tokens=1)
        budget.reserve(s, job.id, role="drafter", stage="CH", estimated_input_tokens=5, requested_output_tokens=5, min_output_tokens=1)  # non-repair role still allowed

    # Concurrent reservations from two sessions never exceed max_calls.
    with Session(engine) as s:
        job = _job(s, budget={"max_calls": 3})
        jid = job.id
    ok = 0
    refused = 0
    for _ in range(6):
        with Session(engine) as s:
            try:
                budget.reserve(s, jid, role="drafter", stage="X", estimated_input_tokens=1, requested_output_tokens=1, min_output_tokens=1)
                ok += 1
            except budget.BudgetExceeded:
                refused += 1
    assert ok == 3 and refused == 3


# ---------------------------------------------------------------- telemetry
def _client(session: Session, job, provider_call, **kw):
    from app.db.session import engine
    from app.services.autonomous.model_client import InvocationRecorder, LLMModelClient

    recorder = InvocationRecorder(job_id=job.id, session_factory=lambda: Session(engine))
    return LLMModelClient(session, default_llm_config_id=job.llm_config_id, recorder=recorder, job_id=job.id, provider_call=provider_call, budget_session_factory=lambda: Session(engine), **kw)


def test_attempts_recorded_retry_clarified_schema_and_fallback(app_client):
    from app.db.models import LLMConfig, ModelInvocation, ModelInvocationAttempt
    from app.db.session import engine
    from app.services.autonomous.model_client import CLARIFIED_SCHEMA_SUFFIX, ProviderResult

    calls: List[Dict[str, Any]] = []

    async def provider(**kw):
        calls.append(kw)
        n = len(calls)
        if n == 1:
            return ProviderResult(content={"ok": "not-a-bool-xx"}, input_tokens=10, output_tokens=5)  # malformed
        if n == 2:
            raise TimeoutError("request timed out")
        return ProviderResult(content={"ok": True, "text": "fine"}, input_tokens=12, output_tokens=6, provider_request_id="req-3")

    with Session(engine) as s:
        fb = LLMConfig(provider="openai_compatible", model_name="fallback-model", api_key="k")
        s.add(fb)
        s.commit()
        s.refresh(fb)
        job = _job(s)
        client = _client(s, job, provider, fallback_llm_config_id=fb.id)
        client.max_tokens_override = {}
        result = asyncio.run(client.structured(role="source_analyst", schema=Tiny, system_prompt="sys", user_prompt="user", prompt_version="p1", stage="T"))
        assert result.ok is True
        assert CLARIFIED_SCHEMA_SUFFIX.split("{")[0] in calls[1]["user_prompt"]  # clarified schema on the retry after malformed output
        assert calls[2]["llm_config_id"] == fb.id  # fallback after the provider failure
        inv = s.exec(select(ModelInvocation).where(ModelInvocation.job_id == job.id)).all()
        assert len(inv) == 1 and inv[0].total_attempts == 3 and inv[0].fallback_used and inv[0].validation_status == "ok" and inv[0].prompt_hash
        att = s.exec(select(ModelInvocationAttempt).where(ModelInvocationAttempt.invocation_id == inv[0].id).order_by(ModelInvocationAttempt.attempt)).all()
        assert [a.status for a in att] == ["invalid", "timeout", "ok"]
        assert att[1].error_category == "provider_failure" and att[2].fallback and att[2].provider_request_id == "req-3"
        assert att[0].diagnostic and "user" not in att[0].diagnostic  # no prompt text in diagnostics
        s.refresh(job)
        assert job.model_calls == 3 and job.input_tokens >= 22 and job.output_tokens >= 11  # every attempt charged, fallback included


def test_failed_attempts_survive_rollback_and_budget_refusal_recorded(app_client):
    from app.db.models import ModelInvocationAttempt
    from app.db.session import engine
    from app.services.autonomous import failures as fail

    async def provider(**kw):
        raise RuntimeError("HTTP 401 Unauthorized: invalid api key")

    with Session(engine) as s:
        job = _job(s)
        client = _client(s, job, provider)
        with pytest.raises(fail.StageFailure) as exc:
            asyncio.run(client.text(role="drafter", system_prompt="s", user_prompt="u", prompt_version="p", stage="T"))
        assert exc.value.category == fail.PROVIDER_FAILURE
        s.rollback()  # the stage rolls back; attempts must remain
        att = s.exec(select(ModelInvocationAttempt).where(ModelInvocationAttempt.job_id == job.id)).all()
        assert len(att) == 1 and att[0].provider_status == "401"  # auth errors are not retried
        # Budget refusal: no provider call, attempt row says budget_refused, category BUDGET_EXCEEDED.
        job2 = _job(s, budget={"max_calls": 0, "max_total_tokens": 1})
        called = []

        async def never(**kw):
            called.append(1)
            raise AssertionError("must not be called")

        client2 = _client(s, job2, never)
        with pytest.raises(fail.StageFailure) as exc2:
            asyncio.run(client2.text(role="drafter", system_prompt="s", user_prompt="u", prompt_version="p", stage="T"))
        assert exc2.value.category == fail.BUDGET_EXCEEDED and not called
        att2 = s.exec(select(ModelInvocationAttempt).where(ModelInvocationAttempt.job_id == job2.id)).all()
        assert [a.status for a in att2] == ["budget_refused"]


def test_runner_pauses_on_budget_and_resumes_after_raise(app_client):
    from app.db.session import engine
    from app.services.autonomous import failures as fail
    from app.services.autonomous import runner as runner_mod

    with Session(engine) as s:
        job = _job(s, budget={"max_calls": 0, "max_total_tokens": 1})
        runner = runner_mod.JobRunner(s, job.id, client_factory=lambda a, b, c: None, owner="b", heartbeat_interval=60)

        async def stage(j, st):
            raise fail.StageFailure(fail.BUDGET_EXCEEDED, "Budget exhausted: max_calls 0 reached")

        runner._run_stage = stage  # type: ignore[method-assign]
        job = asyncio.run(runner.run())
        assert job.status == "paused" and job.error["category"] == "budget_exceeded" and runner_mod.waiting_for(job) == "budget_exhausted"
        assert job.lease_owner is None
        job = runner_mod.resume(s, job, budget={"max_calls": 10, "max_total_tokens": 100000})
        assert job.status == "queued" and job.budget["max_calls"] == 10


# ----------------------------------------------------------- recovery ladder
def test_recovery_handlers_change_behavior(app_client):
    from app.db.models import LLMConfig, RecoveryAction
    from app.db.session import engine
    from app.services.autonomous import failures as fail
    from app.services.autonomous import recovery

    with Session(engine) as s:
        fb = LLMConfig(provider="openai_compatible", model_name="fb", api_key="k")
        s.add(fb)
        s.commit()
        s.refresh(fb)
        job = _job(s, options={"analysis_concurrency": 4, "fallback_llm_config_id": fb.id}, stage="SOURCE_ANALYSIS")

        def run(action, stage, category=fail.PROVIDER_FAILURE, detail=None):
            ctx = recovery.RecoveryContext(session=s, job=job, stage=stage, stage_attempt=1, failure=fail.StageFailure(category, "x", detail=detail), action=action)
            out = recovery.execute(ctx)
            s.commit()
            return out

        out = run(fail.REDUCE_SCOPE, "SOURCE_ANALYSIS", fail.TOKEN_OVERFLOW)
        assert out.success and job.options["analysis_concurrency"] == 2 and job.options["max_tokens_scale"] == 0.75
        out = run(fail.FALLBACK_MODEL, "SOURCE_ANALYSIS")
        assert out.success and job.options["force_fallback"] and out.selected_model == "fb"
        job.options = {k: v for k, v in job.options.items() if k != "fallback_llm_config_id"}
        out = run(fail.FALLBACK_MODEL, "SOURCE_ANALYSIS")
        assert not out.success and "no distinct fallback" in out.reason
        out = run(fail.RETRY_CLARIFIED, "STORYLINE_GENERATION", fail.MALFORMED_OUTPUT, {"problems": ["missing premise"]})
        assert out.success and job.options["schema_feedback"] and "premise" in job.options["schema_feedback_errors"]
        out = run(fail.INDEPENDENT_VERIFY, "ANALYSIS_VERIFICATION", fail.INTERNAL_CONTRADICTION)
        assert out.success and job.options["independent_verification"]
        out = run(fail.REBUILD_DOWNSTREAM, "NOVEL_PREFLIGHT", fail.STALE_DEPENDENCY)
        assert out.success and job.stage == "CHAPTER_PLAN_BUILD"
        out = run(fail.PAUSE, "CHAPTER_GENERATION_LOOP", fail.BUDGET_EXCEEDED)
        assert out.pause
        rows = s.exec(select(RecoveryAction).where(RecoveryAction.job_id == job.id).order_by(RecoveryAction.id)).all()
        assert [r.action for r in rows] == [fail.REDUCE_SCOPE, fail.FALLBACK_MODEL, fail.FALLBACK_MODEL, fail.RETRY_CLARIFIED, fail.INDEPENDENT_VERIFY, fail.REBUILD_DOWNSTREAM, fail.PAUSE]
        assert rows[0].parameters_before["analysis_concurrency"] == 4 and rows[0].parameters_after["analysis_concurrency"] == 2
        hist = recovery.history(s, job.id)
        assert hist[0]["action"] == fail.PAUSE and all("started_at" in h for h in hist)


def test_reanalyze_smallest_unit_targets_only_failed_chapters(app_client):
    """Runs the real ladder through the runner: verification fails -> reanalyze -> stage moves back to SOURCE_ANALYSIS for those chapters only."""
    from app.db.session import engine
    from app.services.autonomous import failures as fail
    from app.services.autonomous import runner as runner_mod

    with Session(engine) as s:
        job = _job(s, stage="ANALYSIS_VERIFICATION", source_project_id=1)
        runner = runner_mod.JobRunner(s, job.id, client_factory=lambda a, b, c: None, owner="r", heartbeat_interval=60)
        marked: List[int] = []

        async def stage(j, st):
            raise fail.StageFailure(fail.INSUFFICIENT_EVIDENCE, "coverage", detail={"failed_chapters": [3, 7], "low_confidence": [7, 9]})

        runner._run_stage = stage  # type: ignore[method-assign]
        import app.services.autonomous.source_stages as src

        orig = src.mark_chapters_for_reanalysis
        src.mark_chapters_for_reanalysis = lambda sess, pid, chs: marked.extend(chs) or len(chs)  # type: ignore[assignment]
        try:
            job = asyncio.run(runner.step())
        finally:
            src.mark_chapters_for_reanalysis = orig  # type: ignore[assignment]
        assert marked == [3, 7, 9] and job.stage == "SOURCE_ANALYSIS" and job.status == "queued"
        assert job.warnings[-1]["recovery"] == fail.REANALYZE_UNIT


def test_lease_loss_does_not_consume_retry_budget(app_client):
    from app.db.models import JobStageAttempt
    from app.db.session import engine
    from app.services.autonomous import lease
    from app.services.autonomous.runner import JobLeaseLost, JobRunner

    with Session(engine) as s:
        job = _job(s)
        runner = JobRunner(s, job.id, client_factory=lambda a, b, c: None, owner="r", heartbeat_interval=60)

        async def stage(j, st):
            with Session(engine) as s2:
                lease.acquire(s2, job.id, "other", now=datetime.now() + timedelta(seconds=1000))
            return {}

        runner._run_stage = stage  # type: ignore[method-assign]
        with pytest.raises(JobLeaseLost):
            asyncio.run(runner.step())
        failed = s.exec(select(JobStageAttempt).where(JobStageAttempt.job_id == job.id, JobStageAttempt.status == "failed")).all()
        assert failed == []
        s.expire_all()
        j = s.get(type(job), job.id)
        assert j.error is None and j.warnings == []
