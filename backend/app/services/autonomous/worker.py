"""In-process background worker for autonomous jobs.

One asyncio task per active job, each with its own Session (never the request
session). The registry mirrors ``workflow_runtime``: the API starts a task on
create/resume, ``is_active`` prevents duplicate execution, and
``recover_on_startup`` requeues jobs a dead process left running so the state
machine continues from its persisted stage.

Fencing contract: this wrapper never mutates a job. Every state transition -
including the paused state after an internal crash - is published by
``JobRunner`` under the lease generation it holds. If the runner escapes with
an exception the wrapper cannot prove lease ownership, so it only logs; the row
stays ``running`` until its lease expires and startup recovery (or another
worker's acquisition) requeues it. ``JobLeaseLost`` is an informational exit,
not a failure: a newer worker owns the job.
"""

from __future__ import annotations

import asyncio
from typing import Dict

from loguru import logger
from sqlmodel import Session, select

from app.db.models import AutonomousNovelJob
from app.services.autonomous.lease import JobLeaseLost
from app.services.autonomous.runner import ClientFactory, JobRunner, default_client_factory, recover_stale_leases


class AutonomousWorker:
    def __init__(self, *, client_factory: ClientFactory = default_client_factory):
        self._tasks: Dict[int, asyncio.Task] = {}
        self.client_factory = client_factory
        # Sanitized outcome of the last run per job (for tests / diagnostics): ok | lease_lost | cancelled | crashed
        self.last_exit: Dict[int, str] = {}

    def is_active(self, job_id: int) -> bool:
        t = self._tasks.get(job_id)
        return t is not None and not t.done()

    def active_jobs(self) -> list[int]:
        return [jid for jid, t in self._tasks.items() if not t.done()]

    def _session(self) -> Session:
        from app.db.session import engine

        return Session(engine)

    async def _execute(self, job_id: int) -> None:
        with self._session() as session:
            runner = JobRunner(session, job_id, client_factory=self.client_factory)
            try:
                job = await runner.run()
                self.last_exit[job_id] = "ok"
                logger.info(f"[Autonomous] job {job_id} stopped at {job.stage} ({job.status})")
            except JobLeaseLost as lost:
                # Stale worker: a newer generation owns the job. Exit without touching it.
                self.last_exit[job_id] = "lease_lost"
                logger.info(f"[Autonomous] job {job_id}: worker {runner.owner} exited after lease loss (generation {lost.generation}); no state was changed")
            except asyncio.CancelledError:
                self.last_exit[job_id] = "cancelled"
                logger.info(f"[Autonomous] job {job_id} task cancelled")
            except Exception as exc:  # noqa: BLE001 - never mutate the job here: lease ownership is unproven
                self.last_exit[job_id] = "crashed"
                session.rollback()
                logger.error(f"[Autonomous] job {job_id} worker crashed outside the fenced runner ({type(exc).__name__}); the row stays leased until expiry and startup recovery requeues it")

    def start(self, job_id: int) -> bool:
        if self.is_active(job_id):
            return False
        self._tasks[job_id] = asyncio.create_task(self._execute(job_id))
        return True

    def stop(self, job_id: int) -> bool:
        t = self._tasks.get(job_id)
        if t is None or t.done():
            return False
        t.cancel()
        return True

    def recover_on_startup(self, session: Session) -> int:
        n = recover_stale_leases(session)
        for job in session.exec(select(AutonomousNovelJob).where(AutonomousNovelJob.status == "queued")).all():
            try:
                self.start(int(job.id))
            except RuntimeError:  # no running loop (tests)
                pass
        return n


autonomous_worker = AutonomousWorker()

__all__ = ["AutonomousWorker", "autonomous_worker"]
