"""Stale-worker fencing: competing acquisition, stale publication, stale crash handler, heartbeat, recovery.

Every scenario uses separate database sessions per worker; the contention
scenarios run the competitors as real concurrent threads / tasks.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import pytest
from sqlmodel import Session, select

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tests.test_autonomous_durability import _job  # noqa: E402

pytestmark = pytest.mark.timeout(300)


def _snapshot(session: Session, job_id: int) -> Dict[str, Any]:
    from app.db.models import AutonomousNovelJob

    job = session.get(AutonomousNovelJob, job_id)
    session.refresh(job)
    return {k: getattr(job, k) for k in ("status", "stage", "progress_message", "progress_percent", "stage_results", "warnings", "error", "chapters_committed", "quality_status", "lease_owner", "lease_generation", "reserved_calls", "model_calls")}


# ------------------------------------------------------- competing acquisition
def test_concurrent_acquisition_one_winner_monotonic_generation(app_client):
    from app.db.session import engine
    from app.services.autonomous import lease

    with Session(engine) as s:
        job = _job(s)
        jid = job.id
    results: List[Optional[lease.Lease]] = [None] * 8
    barrier = threading.Barrier(8)

    def worker(i: int) -> None:
        with Session(engine) as s:
            barrier.wait()
            results[i] = lease.acquire(s, jid, f"worker-{i}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    winners = [r for r in results if r is not None]
    assert len(winners) == 1 and winners[0].generation == 1
    with Session(engine) as s:
        snap = _snapshot(s, jid)
        assert snap["lease_owner"] == winners[0].owner and snap["lease_generation"] == 1
        again = lease.acquire(s, jid, winners[0].owner)
        assert again is not None and again.generation == 2
        assert lease.acquire(s, jid, "late-comer") is None


# ------------------------------------------------------------ stale publish
def test_stale_generation_publication_rejected_and_current_state_unchanged(app_client):
    from app.db.session import engine
    from app.services.autonomous import lease

    with Session(engine) as s:
        job = _job(s)
        jid = job.id
    with Session(engine) as sa_:
        a = lease.acquire(sa_, jid, "worker-a", ttl=1)
        assert a is not None and a.generation == 1
    with Session(engine) as sb:
        b = lease.acquire(sb, jid, "worker-b", now=datetime.now() + timedelta(seconds=5))
        assert b is not None and b.generation == 2
        lease.fenced_update(sb, b, {"stage": "SOURCE_ANALYSIS", "progress_message": "B owns this"})
        before = _snapshot(sb, jid)
    with Session(engine) as sa_:
        for values in ({"status": "paused"}, {"stage": "DONE"}, {"progress_percent": 99.0}, {"stage_results": {"x": 1}}, {"chapters_committed": 5}, {"quality_status": "completed"}, {"error": {"category": "internal_error"}}):
            with pytest.raises(lease.JobLeaseLost):
                lease.fenced_update(sa_, a, values)
        with pytest.raises(lease.JobLeaseLost):
            lease.renew(sa_, a)
        assert lease.release(sa_, a) is False
    with Session(engine) as s:
        assert _snapshot(s, jid) == before


# ---------------------------------------------------- stale crash handler
def test_stale_worker_crash_cannot_pause_current_worker(app_client):
    """Worker A loses its lease, B takes over and runs; A then throws inside its stage. B's job is untouched."""
    from app.db.session import engine
    from app.services.autonomous import lease
    from app.services.autonomous import runner as runner_mod
    from app.services.autonomous.worker import AutonomousWorker

    with Session(engine) as s:
        job = _job(s)
        jid = job.id

    async def scenario():
        with Session(engine) as sa_:
            runner_a = runner_mod.JobRunner(sa_, jid, client_factory=lambda *_: None, owner="worker-a", heartbeat_interval=60, session_factory=lambda: Session(engine))
            took_over = asyncio.Event()
            a_may_crash = asyncio.Event()

            async def stage_a(j, st):
                took_over.set()
                await a_may_crash.wait()
                raise RuntimeError("worker A internal crash after losing the lease")

            runner_a._run_stage = stage_a  # type: ignore[method-assign]
            task_a = asyncio.create_task(runner_a.step())
            await took_over.wait()
            with Session(engine) as s:
                b = lease.acquire(s, jid, "worker-b", now=datetime.now() + timedelta(seconds=lease.lease_seconds() + 5))
                assert b is not None and b.generation == runner_a.lease.generation + 1
                lease.fenced_update(s, b, {"status": "running", "progress_message": "B is running"})
                before = _snapshot(s, jid)
            a_may_crash.set()
            with pytest.raises(lease.JobLeaseLost):
                await task_a
            with Session(engine) as s:
                after = _snapshot(s, jid)
            assert after == before  # A's crash handler published nothing

            # The worker wrapper around a runner that escapes with an exception does not touch the job either.
            import app.services.autonomous.worker as worker_mod

            class Boom(runner_mod.JobRunner):
                async def run(self, **kw):
                    raise RuntimeError("escaped the fence")

            w = AutonomousWorker(client_factory=lambda *_: None)
            orig = worker_mod.JobRunner
            worker_mod.JobRunner = Boom  # type: ignore[assignment]
            try:
                await w._execute(jid)
            finally:
                worker_mod.JobRunner = orig  # type: ignore[assignment]
            assert w.last_exit[jid] == "crashed"
            with Session(engine) as s:
                assert _snapshot(s, jid) == before

    asyncio.run(scenario())


def test_worker_wrapper_lease_loss_is_informational(app_client):
    from app.db.session import engine
    from app.services.autonomous import lease
    from app.services.autonomous.worker import AutonomousWorker

    with Session(engine) as s:
        job = _job(s)
        jid = job.id
        lease.acquire(s, jid, "someone-else")
        before = _snapshot(s, jid)

    async def scenario():
        w = AutonomousWorker(client_factory=lambda *_: None)
        await w._execute(jid)
        assert w.last_exit[jid] == "lease_lost"

    asyncio.run(scenario())
    with Session(engine) as s:
        assert _snapshot(s, jid) == before


# ---------------------------------------------------------------- heartbeat
def test_heartbeat_ownership_and_independent_session(app_client):
    from app.db.session import engine
    from app.services.autonomous import lease

    with Session(engine) as s:
        job = _job(s)
        jid = job.id
        good = lease.acquire(s, jid, "owner", ttl=2)
        stale = lease.Lease(job_id=jid, owner="owner", generation=good.generation - 1, expires_at=good.expires_at)
        wrong_owner = lease.Lease(job_id=jid, owner="impostor", generation=good.generation, expires_at=good.expires_at)
        exp1 = lease.renew(s, good)
        assert exp1 > datetime.now() + timedelta(seconds=1)
        with pytest.raises(lease.JobLeaseLost):
            lease.renew(s, stale)
        with pytest.raises(lease.JobLeaseLost):
            lease.renew(s, wrong_owner)

    opened: List[int] = []

    def factory() -> Session:
        opened.append(1)
        return Session(engine)

    async def scenario():
        hb = lease.Heartbeat(good, session_factory=factory, interval=0.05).start()
        await asyncio.sleep(0.25)
        assert hb.renewals >= 2 and len(opened) >= 2 and not hb.lost
        with Session(engine) as s:
            thief = lease.acquire(s, jid, "thief", now=datetime.now() + timedelta(seconds=lease.lease_seconds() + 10))
            assert thief is not None
        await asyncio.sleep(0.3)
        assert hb.lost and not hb.running
        await hb.stop()

    asyncio.run(scenario())


def test_heartbeat_failure_makes_runner_refuse_to_publish(app_client):
    from app.db.session import engine
    from app.services.autonomous import lease
    from app.services.autonomous import runner as runner_mod

    with Session(engine) as s:
        job = _job(s)
        jid = job.id

    async def scenario():
        with Session(engine) as s:
            runner = runner_mod.JobRunner(s, jid, client_factory=lambda *_: None, owner="hb-runner", heartbeat_interval=0.05, session_factory=lambda: Session(engine))

            async def stage(j, st):
                with Session(engine) as s2:
                    assert lease.acquire(s2, jid, "thief", now=datetime.now() + timedelta(seconds=lease.lease_seconds() + 10)) is not None
                for _ in range(40):
                    await asyncio.sleep(0.05)
                    if runner.heartbeat is not None and runner.heartbeat.lost:
                        break
                return {"ok": True}

            runner._run_stage = stage  # type: ignore[method-assign]
            with pytest.raises(lease.JobLeaseLost):
                await runner.step()
            snap = _snapshot(s, jid)
            assert snap["lease_owner"] == "thief" and snap["stage"] == "INGEST" and not snap["stage_results"]

    asyncio.run(scenario())


# ----------------------------------------------------------------- recovery
def test_lease_loss_consumes_no_retry_or_recovery_budget(app_client):
    from app.db.models import JobStageAttempt, RecoveryAction
    from app.db.session import engine
    from app.services.autonomous import lease
    from app.services.autonomous import runner as runner_mod

    with Session(engine) as s:
        job = _job(s)
        jid = job.id

    async def scenario():
        with Session(engine) as s:
            runner = runner_mod.JobRunner(s, jid, client_factory=lambda *_: None, owner="loser", heartbeat_interval=60, session_factory=lambda: Session(engine))

            async def stage(j, st):
                with Session(engine) as s2:
                    lease.acquire(s2, jid, "winner", now=datetime.now() + timedelta(seconds=lease.lease_seconds() + 10))
                return {"done": True}

            runner._run_stage = stage  # type: ignore[method-assign]
            with pytest.raises(lease.JobLeaseLost):
                await runner.step()
            failed = s.exec(select(JobStageAttempt).where(JobStageAttempt.job_id == jid, JobStageAttempt.status == "failed")).all()
            recov = s.exec(select(RecoveryAction).where(RecoveryAction.job_id == jid)).all()
            assert failed == [] and recov == []
            snap = _snapshot(s, jid)
            assert snap["error"] is None and snap["warnings"] == [] and snap["lease_owner"] == "winner"

    asyncio.run(scenario())


def test_recovery_after_crash_resumes_without_duplicates(app_client):
    """Chapter-loop crash after a durable chapter commit -> lease expiry -> recover -> resume: exactly one committed run per chapter."""
    from app.db.models import AutonomousNovelJob, CanonFact, ChapterPipelineRun, ExportArtifact, LLMConfig, ModelInvocation, StorylineCandidate
    from app.db.session import engine
    from app.services.autonomous import lease
    from app.services.autonomous import runner as runner_mod
    from app.services.forge import provenance
    from tests.test_autonomous_pipeline import CHAPTERS, FakeClient, build_source_epub

    fake = FakeClient()
    epub = build_source_epub()
    with Session(engine) as s:
        cfg = LLMConfig(provider="authnd", model_name="moonshotai/kimi-k3", api_key="", display_name="fence-recovery")
        s.add(cfg)
        s.commit()
        s.refresh(cfg)
        job = runner_mod.create_job(s, filename="fence.epub", data=epub, llm_config_id=cfg.id, options={"genre": "mystery"}, idempotency_key="fence-recovery-1")
        jid = job.id

    def runner_for(session: Session, owner: str):
        return runner_mod.JobRunner(session, jid, client_factory=lambda s_, j, r: fake, owner=owner, heartbeat_interval=60)

    with Session(engine) as s:
        job = asyncio.run(runner_for(s, "gen-a").run())
        assert job.stage == "STORYLINE_SELECTION" and job.status == "waiting_for_user"
        options = [c for c in s.exec(select(StorylineCandidate).where(StorylineCandidate.job_id == jid)).all() if not c.rejected]
        job = runner_mod.select_storyline(s, job, storyline_id=options[0].id, chapter_count=CHAPTERS)
        runner = runner_for(s, "gen-a")
        job = asyncio.run(runner.run(until_stage="CHAPTER_GENERATION_LOOP"))
        job = asyncio.run(runner.step())
        job = asyncio.run(runner.step())
        assert job.chapters_committed == 2 and job.stage == "CHAPTER_GENERATION_LOOP"
        gen_before = job.lease_generation
        pid = int(job.original_project_id)
        storylines_before = len(s.exec(select(StorylineCandidate).where(StorylineCandidate.job_id == jid)).all())
        invocations_before = len(s.exec(select(ModelInvocation).where(ModelInvocation.job_id == jid)).all())
        job.status = "running"
        job.lease_owner = "gen-a-dead"
        job.lease_expires_at = datetime.now() - timedelta(seconds=1)
        s.add(job)
        s.commit()
    with Session(engine) as s:
        assert runner_mod.recover_stale_leases(s) == 1
        job = s.get(AutonomousNovelJob, jid)
        assert job.status == "queued" and job.chapters_committed == 2 and job.reserved_calls == 0
        job = asyncio.run(runner_for(s, "gen-b").run())
        assert job.status == "completed" and job.stage == "DONE", (job.status, job.stage, job.error)
        assert job.lease_generation > gen_before
        committed = s.exec(select(ChapterPipelineRun).where(ChapterPipelineRun.project_id == pid, ChapterPipelineRun.status == "committed")).all()
        assert sorted(r.chapter_number for r in committed) == list(range(1, CHAPTERS + 1))  # no duplicate chapter
        assert provenance.get_manifest(s, pid).latest_committed_chapter == CHAPTERS
        candidates = s.exec(select(StorylineCandidate).where(StorylineCandidate.job_id == jid)).all()
        assert len(candidates) == storylines_before and len([c for c in candidates if c.selected]) == 1
        arts = s.exec(select(ExportArtifact).where(ExportArtifact.job_id == jid)).all()
        assert len(arts) == len({a.kind for a in arts}) and len(arts) >= 5
        # Canon is append-only with supersession; the *current* facts must be unique and none may be superseded twice.
        current = [c.fact_id for c in s.exec(select(CanonFact).where(CanonFact.project_id == pid, CanonFact.superseded_by_id.is_(None))).all()]
        assert len(current) == len(set(current))
        assert len(s.exec(select(ModelInvocation).where(ModelInvocation.job_id == jid)).all()) >= invocations_before
        stale = lease.Lease(job_id=jid, owner="gen-a-dead", generation=gen_before, expires_at=datetime.now())
        with pytest.raises(lease.JobLeaseLost):
            lease.fenced_update(s, stale, {"status": "paused"})
        s.refresh(job)
        assert job.status == "completed"
