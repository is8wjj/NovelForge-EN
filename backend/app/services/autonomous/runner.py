"""Durable, resumable state machine for one autonomous novel job.

Durability contract: **at-least-once stage execution with idempotent artifact
publication and fenced state-machine commits.**

- The runner executes one stage at a time (one chapter for the loop). Before a
  stage runs it acquires the job lease with a database compare-and-set
  (``lease.acquire``) which advances ``lease_generation``; an independent
  heartbeat renews the lease on its own session while the stage runs.
- Every job-state publication (stage advance, status, results, error, DONE) is
  a conditional UPDATE on ``(id, lease_owner, lease_generation)``. A worker whose
  lease was replaced gets ``JobLeaseLost`` and publishes nothing; that is not a
  stage failure and never consumes the stage's retry budget.
- Stage functions commit their own artifacts (cards, canon, exports) and are
  idempotent per manuscript id / chapter number / title, so a stage re-run after
  a crash or lease loss converges instead of duplicating. Model requests may
  therefore execute more than once; job state cannot be advanced twice.

Failures are classified (``failures``) and recovered through the typed ladder in
``recovery``; the ladder's PAUSE rung leaves the job resumable. Human gates:
``STORYLINE_SELECTION`` always waits; ``approval_gates`` also waits before
drafting (plan) and before export (manuscript); ``manual`` stops after the
example library and hands over to the Forge controls.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import socket
import uuid
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from loguru import logger
from sqlmodel import Session, select

from app.db.models import AutonomousNovelJob, JobStageAttempt, Project, StorylineCandidate
from app.schemas.project import ProjectCreate
from app.services import project_service
from app.services.autonomous import architecture as arch_mod
from app.services.autonomous import audit as audit_mod
from app.services.autonomous import budget as budget_mod
from app.services.autonomous import chapter_loop, chapter_plan, failpoints, recovery
from app.services.autonomous import export as export_mod
from app.services.autonomous import failures as fail
from app.services.autonomous import lease as lease_mod
from app.services.autonomous import source_stages as src
from app.services.autonomous import storylines as story_mod
from app.services.autonomous.lease import JobLeaseLost, Lease
from app.services.autonomous.model_client import InvocationRecorder, LLMModelClient, ModelClient
from app.services.forge import transfer

STAGES: List[str] = [
    "INGEST", "SOURCE_ANALYSIS", "ANALYSIS_VERIFICATION", "BOOK_STRUCTURE", "FINGERPRINT_BUILD", "EXAMPLE_LIBRARY_BUILD",
    "STORYLINE_GENERATION", "STORYLINE_SELECTION", "NOVEL_ARCHITECTURE", "BIBLE_BUILD", "CHAPTER_PLAN_BUILD", "NOVEL_PREFLIGHT",
    "CHAPTER_GENERATION_LOOP", "WHOLE_NOVEL_AUDIT", "GLOBAL_REPAIR", "EXPORT", "DONE",
]
# Progress weight of each stage (sums to 100); the chapter loop interpolates within its weight.
STAGE_WEIGHTS: Dict[str, float] = {
    "INGEST": 2, "SOURCE_ANALYSIS": 14, "ANALYSIS_VERIFICATION": 1, "BOOK_STRUCTURE": 6, "FINGERPRINT_BUILD": 1, "EXAMPLE_LIBRARY_BUILD": 1,
    "STORYLINE_GENERATION": 5, "STORYLINE_SELECTION": 0, "NOVEL_ARCHITECTURE": 4, "BIBLE_BUILD": 1, "CHAPTER_PLAN_BUILD": 5, "NOVEL_PREFLIGHT": 0,
    "CHAPTER_GENERATION_LOOP": 52, "WHOLE_NOVEL_AUDIT": 2, "GLOBAL_REPAIR": 4, "EXPORT": 2, "DONE": 0,
}
MODES = ("fully_automatic", "approval_gates", "manual")
GATES = {"fully_automatic": {"STORYLINE_SELECTION"}, "approval_gates": {"STORYLINE_SELECTION", "NOVEL_PREFLIGHT", "EXPORT"}, "manual": {"STORYLINE_GENERATION", "STORYLINE_SELECTION", "NOVEL_PREFLIGHT", "EXPORT"}}
LEASE_SECONDS = lease_mod.lease_seconds()
TERMINAL = ("completed", "failed", "cancelled")
QUALITY_STATUSES = ("completed", "completed_with_warnings", "quality_gate_failed", "manual_review_required")
WAIT_MESSAGES = {"STORYLINE_SELECTION": "Choose a storyline and chapter count", "NOVEL_PREFLIGHT": "Review the novel plan", "EXPORT": "Review the finished manuscript", "STORYLINE_GENERATION": "Manual mode: use the Forge controls to continue"}

ClientFactory = Callable[[Session, AutonomousNovelJob, InvocationRecorder], ModelClient]


def default_client_factory(session: Session, job: AutonomousNovelJob, recorder: InvocationRecorder) -> ModelClient:
    opts = job.options or {}
    roles = dict(job.role_llm_config_ids or {})
    fallback = opts.get("fallback_llm_config_id")
    default_cid = int(job.llm_config_id)
    if opts.get("force_fallback") and fallback:
        # Recovery rung: every role goes to the fallback until the job leaves this stage.
        default_cid = int(fallback)
        roles = {}
    client = LLMModelClient(session, default_llm_config_id=default_cid, role_llm_config_ids=roles, fallback_llm_config_id=fallback, recorder=recorder, job_id=job.id)
    scale = float(opts.get("max_tokens_scale") or 1.0)
    if scale < 1.0:
        from app.services.autonomous.model_client import ROLE_POLICIES

        client.max_tokens_override = {r: max(512, int(p.max_tokens * scale)) for r, p in ROLE_POLICIES.items()}
    return client


def progress_percent(job: AutonomousNovelJob, within: float = 0.0) -> float:
    done = 0.0
    for s in STAGES:
        if s == job.stage:
            done += STAGE_WEIGHTS.get(s, 0) * max(0.0, min(1.0, within))
            break
        done += STAGE_WEIGHTS.get(s, 0)
    return round(min(100.0, done), 1)


def waiting_for(job: AutonomousNovelJob) -> Optional[str]:
    """Why the job is not progressing, for the UI. ``None`` while it runs."""
    if job.status == "waiting_for_user":
        return {"STORYLINE_SELECTION": "storyline_selection", "NOVEL_PREFLIGHT": "plan_approval", "EXPORT": "manuscript_approval", "STORYLINE_GENERATION": "manual_mode"}.get(job.stage, "approval")
    if job.status == "paused":
        cat = (job.error or {}).get("category")
        if cat == fail.BUDGET_EXCEEDED:
            return "budget_exhausted"
        if cat == fail.PROVIDER_FAILURE:
            return "provider_unavailable"
        if cat:
            return "manual_review_required"
        return "paused"
    if job.status == "completed" and job.quality_status == "quality_gate_failed":
        return "quality_gate_failed"
    if job.status == "completed" and job.quality_status == "manual_review_required":
        return "manual_review_required"
    return None


def job_dict(session: Session, job: AutonomousNovelJob) -> Dict[str, Any]:
    attempts = session.exec(select(JobStageAttempt).where(JobStageAttempt.job_id == job.id).order_by(JobStageAttempt.id.desc()).limit(30)).all()
    return {
        "id": job.id, "status": job.status, "stage": job.stage, "mode": job.mode, "source_project_id": job.source_project_id, "original_project_id": job.original_project_id,
        "llm_config_id": job.llm_config_id, "source_filename": job.source_filename, "options": {k: v for k, v in (job.options or {}).items() if k not in ("schema_feedback_errors",)}, "selected_storyline_id": job.selected_storyline_id, "chapter_count": job.chapter_count,
        "chapters_committed": job.chapters_committed, "progress_percent": job.progress_percent, "progress_message": job.progress_message, "stage_results": job.stage_results, "warnings": job.warnings,
        "error": job.error, "model_calls": job.model_calls, "input_tokens": job.input_tokens, "output_tokens": job.output_tokens, "waiting_for": waiting_for(job),
        "quality_status": job.quality_status, "quality_summary": job.quality_summary, "budget": budget_mod.usage_snapshot(session, job), "lease": {"owner": job.lease_owner, "generation": job.lease_generation, "expires_at": job.lease_expires_at.isoformat() if job.lease_expires_at else None, "heartbeat_at": job.heartbeat_at.isoformat() if job.heartbeat_at else None},
        "created_at": job.created_at.isoformat() if job.created_at else None, "updated_at": job.updated_at.isoformat() if job.updated_at else None, "started_at": job.started_at.isoformat() if job.started_at else None, "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "attempts": [{"stage": a.stage, "attempt": a.attempt, "status": a.status, "failure_category": a.failure_category, "recovery_action": a.recovery_action, "started_at": a.started_at.isoformat(), "finished_at": a.finished_at.isoformat() if a.finished_at else None} for a in attempts],
        "recovery": recovery.history(session, int(job.id), limit=30),
        "stages": STAGES,
    }


def quality_verdict(audit: Dict[str, Any], repair: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Terminal quality status from the final audit (never 'completed' with blocking findings left)."""
    findings = list(audit.get("findings") or [])
    blocking = [f for f in findings if f.get("severity") in ("critical", "high")]
    repaired = list((repair or {}).get("repaired") or [])
    replan_kinds = {"main_plot_not_closed", "subplot_disappeared", "subplot_never_started", "unresolved_setup", "chapter_count_mismatch", "early_climax"}
    human_kinds = {"pov_contract_mismatch", "unused_major_character", "antagonist_underused"}
    needs_replan = [f for f in blocking if f.get("kind") in replan_kinds]
    needs_human = [f for f in findings if f.get("kind") in human_kinds or (f.get("severity") in ("critical", "high") and f.get("kind") not in replan_kinds and not str(f.get("kind", "")).startswith("source_") and f.get("kind") not in ("pov_drift", "tense_drift", "dead_character_acts"))]
    if not findings:
        status = "completed"
    elif blocking:
        status = "quality_gate_failed" if (needs_replan or any(str(f.get("kind", "")).startswith("source_") for f in blocking)) else "manual_review_required"
    else:
        status = "completed_with_warnings"
    return {
        "status": status, "total_findings": len(findings), "blocking_findings": len(blocking), "repaired_chapters": repaired, "repaired_findings": int((repair or {}).get("repaired_findings") or 0),
        "unresolved_findings": len(blocking), "auto_repairable_findings": len([f for f in blocking if f.get("kind") in ("pov_drift", "tense_drift", "dead_character_acts") or str(f.get("kind", "")).startswith("source_")]),
        "findings_requiring_replan": len(needs_replan), "findings_requiring_human_review": len(needs_human), "by_severity": dict(audit.get("counts") or {}),
        "by_kind": _count_by(findings, "kind"), "affected_chapters": sorted({int(f["chapter"]) for f in findings if f.get("chapter")})[:100],
    }


def _count_by(items: List[Dict[str, Any]], key: str) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for it in items:
        k = str(it.get(key) or "unknown")
        out[k] = out.get(k, 0) + 1
    return out


class JobRunner:
    """Executes stages for one job under an atomic, heartbeat-renewed lease."""

    def __init__(self, session: Session, job_id: int, *, client_factory: ClientFactory = default_client_factory, owner: Optional[str] = None, session_factory: Optional[Callable[[], Session]] = None, heartbeat_interval: Optional[float] = None):
        self.session = session
        self.job_id = job_id
        self.client_factory = client_factory
        self.owner = owner or f"{socket.gethostname()}:{uuid.uuid4().hex[:8]}"
        self.session_factory = session_factory or self._engine_session
        self.heartbeat_interval = heartbeat_interval
        self.lease: Optional[Lease] = None
        self.heartbeat: Optional[lease_mod.Heartbeat] = None

    @staticmethod
    def _engine_session() -> Session:
        from app.db.session import engine

        return Session(engine)

    # ---------------------------------------------------------------- utils
    @property
    def job(self) -> AutonomousNovelJob:
        job = self.session.get(AutonomousNovelJob, self.job_id)
        if job is None:
            raise RuntimeError(f"Job {self.job_id} not found")
        self.session.refresh(job)
        return job

    def _publish(self, **fields: Any) -> None:
        """Fenced job-state write; raises JobLeaseLost if another worker owns the job now."""
        assert self.lease is not None
        if self.heartbeat is not None and self.heartbeat.lost:
            raise JobLeaseLost(self.job_id, self.owner, self.lease.generation, "heartbeat reported lease loss")
        self.session.expire_all()
        lease_mod.fenced_update(self.session, self.lease, fields)

    def _check_lease(self) -> None:
        assert self.lease is not None
        if self.heartbeat is not None and self.heartbeat.lost:
            raise JobLeaseLost(self.job_id, self.owner, self.lease.generation, "heartbeat reported lease loss")
        lease_mod.check(self.session, self.lease)

    def _progress(self, message: str, within: float) -> None:
        """Progress callback for stages: fenced, and a lease-loss surfaces as an exception inside the stage."""
        job = self.job
        self._publish(progress_message=message[:400], progress_percent=progress_percent(job, within))

    def _client(self, job: AutonomousNovelJob) -> ModelClient:
        recorder = InvocationRecorder(job_id=job.id, project_id=job.original_project_id or job.source_project_id, session_factory=self.session_factory)
        return self.client_factory(self.session, job, recorder)

    def _source_ctx(self, job: AutonomousNovelJob, client: ModelClient) -> src.SourceContext:
        opts = job.options or {}
        return src.SourceContext(source_project_id=int(job.source_project_id), filename=job.source_filename, data=job.source_bytes or b"", client=client, options=opts, progress=self._progress, analysis_concurrency=int(opts.get("analysis_concurrency") or 4), window_size=int(opts.get("window_size") or 40), max_stage_count=int(opts.get("max_stage_count") or 24))

    def _storyline(self, job: AutonomousNovelJob) -> Dict[str, Any]:
        row = self.session.get(StorylineCandidate, int(job.selected_storyline_id or 0))
        if row is None:
            raise fail.StageFailure(fail.USER_INPUT_REQUIRED, "No storyline selected")
        return dict(row.content or {})

    def _preferences(self, job: AutonomousNovelJob) -> Dict[str, Any]:
        opts = job.options or {}
        return {k: opts.get(k) for k in ("genre_intensity", "content_rating", "ending_preference", "romance_level", "genre", "notes") if opts.get(k)}

    # --------------------------------------------------------------- stages
    async def _run_stage(self, job: AutonomousNovelJob, stage: str) -> Dict[str, Any]:
        client = self._client(job)
        opts = job.options or {}
        if stage == "INGEST":
            if not job.source_project_id:
                name = f"Reference · {job.source_filename or 'manuscript'} · {job.source_file_hash[:8]}"
                existing = self.session.exec(select(Project).where(Project.name == name)).first()
                if existing is None:
                    existing, _ = project_service.create_project(self.session, ProjectCreate(name=name, description="Reference manuscript analysed by the autonomous novel pipeline", template=None))
                self._publish(source_project_id=existing.id)
                job = self.job
            return src.stage_ingest(self.session, self._source_ctx(job, client))
        if stage == "SOURCE_ANALYSIS":
            return await src.stage_source_analysis(self.session, self._source_ctx(job, client))
        if stage == "ANALYSIS_VERIFICATION":
            return src.stage_analysis_verification(self.session, self._source_ctx(job, client))
        if stage == "BOOK_STRUCTURE":
            return await src.stage_book_structure(self.session, self._source_ctx(job, client))
        if stage == "FINGERPRINT_BUILD":
            return src.stage_fingerprint(self.session, self._source_ctx(job, client))
        if stage == "EXAMPLE_LIBRARY_BUILD":
            return src.stage_example_library(self.session, self._source_ctx(job, client))
        if stage == "STORYLINE_GENERATION":
            return await story_mod.stage_storyline_generation(self.session, job_id=job.id, source_project_id=int(job.source_project_id), client=client, preferences=self._preferences(job), count=int(opts.get("storyline_count") or story_mod.TARGET_OPTIONS))
        if stage == "STORYLINE_SELECTION":
            if not job.selected_storyline_id or not job.chapter_count:
                raise fail.StageFailure(fail.USER_INPUT_REQUIRED, "Select a storyline and a chapter count")
            return {"selected_storyline_id": job.selected_storyline_id, "chapter_count": job.chapter_count}
        if stage == "NOVEL_ARCHITECTURE":
            if not job.original_project_id:
                storyline = self._storyline(job)
                name = str(opts.get("title") or storyline.get("title") or "Original Novel")[:80] + f" · {job.source_file_hash[:6]}-{job.id}"
                existing = self.session.exec(select(Project).where(Project.name == name)).first()
                pid = existing.id if existing else transfer.create_original_project(self.session, source_project_id=int(job.source_project_id), name=name, description=str(storyline.get("hook") or ""), template=None).project_id
                self._publish(original_project_id=pid)
                job = self.job
            profile = story_mod.source_profile(self.session, int(job.source_project_id))
            return await arch_mod.stage_novel_architecture(self.session, original_project_id=int(job.original_project_id), source_project_id=int(job.source_project_id), storyline=self._storyline(job), chapter_count=job.chapter_count, client=client, brief=story_mod.source_brief(self.session, int(job.source_project_id), preferences=self._preferences(job)), preferences=self._preferences(job), profile=profile)
        if stage == "BIBLE_BUILD":
            return arch_mod.stage_bible_build(self.session, original_project_id=int(job.original_project_id), chapter_count=job.chapter_count, storyline=self._storyline(job))
        if stage == "CHAPTER_PLAN_BUILD":
            return await chapter_plan.stage_chapter_plan(self.session, project_id=int(job.original_project_id), chapter_count=job.chapter_count, client=client, options=opts, progress=self._progress)
        if stage == "NOVEL_PREFLIGHT":
            return chapter_plan.stage_preflight(self.session, project_id=int(job.original_project_id), chapter_count=job.chapter_count)
        if stage == "CHAPTER_GENERATION_LOOP":
            word_target = chapter_plan.words_per_chapter(opts, job.chapter_count)
            return await chapter_loop.generate_next_chapter(self.session, project_id=int(job.original_project_id), chapter_count=job.chapter_count, client=client, options=opts, word_target=word_target, budget_chars=int(opts.get("budget_chars") or 16000), lease_check=self._check_lease)
        if stage == "WHOLE_NOVEL_AUDIT":
            return audit_mod.whole_novel_audit(self.session, int(job.original_project_id), job.chapter_count)
        if stage == "GLOBAL_REPAIR":
            audit = (job.stage_results or {}).get("WHOLE_NOVEL_AUDIT") or audit_mod.whole_novel_audit(self.session, int(job.original_project_id), job.chapter_count)
            checkpoint = dict((job.stage_results or {}).get("checkpoint:GLOBAL_REPAIR") or {})

            def save_checkpoint(cp: Dict[str, Any]) -> None:
                results = dict(self.job.stage_results or {})
                results["checkpoint:GLOBAL_REPAIR"] = cp
                self._publish(stage_results=results)

            return await audit_mod.global_repair(self.session, project_id=int(job.original_project_id), chapter_count=job.chapter_count, client=client, audit=audit, lease_check=self._check_lease, checkpoint=checkpoint, save_checkpoint=save_checkpoint)
        if stage == "EXPORT":
            audit = ((job.stage_results or {}).get("GLOBAL_REPAIR") or {}).get("audit") or (job.stage_results or {}).get("WHOLE_NOVEL_AUDIT") or {}
            failpoints.hit("before_export_create")
            return export_mod.stage_export(self.session, job=job, project_id=int(job.original_project_id), audit=audit)
        raise fail.StageFailure(fail.INTERNAL_ERROR, f"Unknown stage {stage}")

    # ------------------------------------------------------------------ run
    async def _start_heartbeat(self) -> None:
        assert self.lease is not None
        self.heartbeat = lease_mod.Heartbeat(self.lease, session_factory=self.session_factory, interval=self.heartbeat_interval).start()

    async def _stop_heartbeat(self) -> None:
        if self.heartbeat is not None:
            await self.heartbeat.stop()
            self.heartbeat = None

    async def step(self) -> AutonomousNovelJob:
        """Execute exactly one stage transition (one chapter for the loop). Returns the refreshed job."""
        job = self.job
        if job.status in TERMINAL or job.status in ("waiting_for_user", "paused"):
            return job
        self.lease = lease_mod.acquire(self.session, self.job_id, self.owner)
        if self.lease is None:
            raise JobLeaseLost(self.job_id, self.owner, -1, f"job {self.job_id} is leased by another worker")
        job = self.job
        stage = job.stage
        try:
            if stage == "DONE":
                self._finish(job)
                return self.job
            gates = GATES.get(job.mode, GATES["fully_automatic"])
            if stage in gates and not (job.stage_results or {}).get(f"approved:{stage}"):
                if not (stage == "STORYLINE_SELECTION" and job.selected_storyline_id and job.chapter_count):
                    self._publish(status="waiting_for_user", progress_message=WAIT_MESSAGES.get(stage, "Waiting for approval"))
                    lease_mod.release(self.session, self.lease)
                    return self.job
            failed = self.session.exec(select(JobStageAttempt).where(JobStageAttempt.job_id == job.id, JobStageAttempt.stage == stage, JobStageAttempt.status == "failed")).all()
            attempt_no = len(failed) + 1
            attempt = JobStageAttempt(job_id=job.id, stage=stage, attempt=attempt_no, status="running", detail={"lease_generation": self.lease.generation, "owner": self.owner})
            self.session.add(attempt)
            self.session.commit()
            attempt_id = int(attempt.id)
            self._publish(status="running", started_at=job.started_at or datetime.now(), error=None, progress_message=f"{stage.replace('_', ' ').title()}…", progress_percent=progress_percent(job))
            await self._start_heartbeat()
            try:
                result = await self._run_stage(self.job, stage)
                failpoints.hit("after_stage_work")
                self._check_lease()
                self._advance(stage, result, attempt_id)
                return self.job
            except JobLeaseLost:
                raise
            except asyncio.CancelledError:
                self.session.rollback()
                self._mark_attempt(attempt_id, "paused")
                try:
                    self._publish(status="paused", progress_message="Cancelled by request")
                except JobLeaseLost:
                    pass
                raise
            except fail.StageFailure as exc:
                self.session.rollback()
                await self._handle_failure(stage, attempt_no, attempt_id, exc)
                return self.job
            except Exception as exc:  # noqa: BLE001 - internal crash: classified into the ladder under the fence
                self.session.rollback()
                await self._handle_failure(stage, attempt_no, attempt_id, exc)
                return self.job
            finally:
                await self._stop_heartbeat()
        except JobLeaseLost as lost:
            # Another worker owns the job now: publish nothing, leave its state alone.
            # Lease loss is not a pipeline failure and consumes no retry budget.
            self.session.rollback()
            logger.info(f"[Autonomous] job {self.job_id}: worker {self.owner} lost the lease (generation {lost.generation}); exiting without publication")
            await self._stop_heartbeat()
            raise

    def _mark_attempt(self, attempt_id: int, status: str, **fields: Any) -> None:
        attempt = self.session.get(JobStageAttempt, attempt_id)
        if attempt is None:
            return
        attempt.status = status
        attempt.finished_at = datetime.now()
        for k, v in fields.items():
            setattr(attempt, k, v)
        self.session.add(attempt)
        self.session.commit()

    def _advance(self, stage: str, result: Dict[str, Any], attempt_id: int) -> None:
        job = self.job
        results = dict(job.stage_results or {})
        fields: Dict[str, Any] = {}
        if stage == "CHAPTER_GENERATION_LOOP":
            loop = dict(results.get(stage) or {})
            if result.get("chapter"):
                loop[str(result["chapter"])] = {k: v for k, v in result.items() if k != "complete"}
            results[stage] = loop
            fields["chapters_committed"] = int(result.get("chapter") or job.chapters_committed)
            if result.get("deviation", {}).get("accepted_warnings"):
                fields["warnings"] = (job.warnings or []) + [{"stage": stage, "chapter": result["chapter"], "warnings": result["deviation"]["accepted_warnings"]}]
            next_stage = STAGES[STAGES.index(stage) + 1] if result.get("complete") else stage
            within = fields["chapters_committed"] / max(1, job.chapter_count)
        else:
            results[stage] = result
            next_stage = STAGES[STAGES.index(stage) + 1]
            within = 1.0
            if stage == "WHOLE_NOVEL_AUDIT" and result.get("passed"):
                next_stage = "EXPORT"
        if stage in ("WHOLE_NOVEL_AUDIT", "GLOBAL_REPAIR"):
            audit = result.get("audit") if stage == "GLOBAL_REPAIR" else result
            verdict = quality_verdict(audit or {}, result if stage == "GLOBAL_REPAIR" else None)
            fields["quality_status"] = verdict["status"]
            fields["quality_summary"] = verdict
        if stage == "STORYLINE_GENERATION" and (job.options or {}).get("schema_feedback"):
            fields["options"] = {k: v for k, v in (job.options or {}).items() if k not in ("schema_feedback", "schema_feedback_errors")}
        if stage != job.stage:
            pass  # a recovery handler moved the stage; the result still belongs to the stage that ran
        self._mark_attempt(attempt_id, "succeeded", detail={"summary": {k: v for k, v in result.items() if isinstance(v, (int, float, str, bool))}, "lease_generation": self.lease.generation if self.lease else None})
        failpoints.hit("before_stage_advance")
        job = self.job
        fields["progress_percent"] = progress_percent(job, 0.0 if next_stage != stage else within)
        self._publish(stage_results=results, stage=next_stage, status="queued", **fields)
        failpoints.hit("after_stage_advance")
        if next_stage == "DONE":
            self._finish(self.job)

    def _finish(self, job: AutonomousNovelJob) -> None:
        summary = dict(job.quality_summary or {})
        status = job.quality_status or "completed"
        msg = {"completed": "Finished", "completed_with_warnings": f"Finished with {summary.get('total_findings', 0)} non-blocking finding(s)", "quality_gate_failed": f"Finished: quality gate failed ({summary.get('blocking_findings', 0)} blocking finding(s) unresolved)", "manual_review_required": f"Finished: manual review required ({summary.get('blocking_findings', 0)} finding(s) need a human)"}.get(status, "Finished")
        self._publish(status="completed", stage="DONE", finished_at=datetime.now(), progress_percent=100.0, progress_message=msg, quality_status=status, lease_owner=None, lease_expires_at=None)

    async def _handle_failure(self, stage: str, attempt_no: int, attempt_id: int, exc: BaseException) -> None:
        job = self.job
        failure = exc if isinstance(exc, fail.StageFailure) else fail.StageFailure(fail.classify_exception(exc), f"{type(exc).__name__}: {exc}")
        logger.warning(f"[Autonomous] job {job.id} stage {stage} attempt {attempt_no} failed ({failure.category})")
        if failure.category == fail.USER_INPUT_REQUIRED:
            self._mark_attempt(attempt_id, "failed", failure_category=failure.category, detail=failure.as_dict(), recovery_action=fail.PAUSE)
            self._publish(status="waiting_for_user", error=None, progress_message=str(failure)[:400])
            lease_mod.release(self.session, self.lease)
            return
        policy = fail.POLICIES[failure.category]
        action = policy.action_for(attempt_no)
        ctx = recovery.RecoveryContext(session=self.session, job=job, stage=stage, stage_attempt=attempt_no, failure=failure, action=action)
        outcome = recovery.execute(ctx)
        # Handlers mutate the ORM job (options / next stage). Capture the intent, then discard the dirty
        # instance so the audit-row commit below cannot flush an unfenced job update; publish via the fence.
        fields: Dict[str, Any] = {"options": dict(job.options or {}), "stage": job.stage}
        self.session.expire(job)
        self._mark_attempt(attempt_id, "failed", failure_category=failure.category, detail=failure.as_dict(), recovery_action=action)
        if outcome.pause:
            self._publish(status="paused", error=failure.as_dict(), progress_message=f"Paused after {attempt_no} attempt(s) at {stage}: {str(failure)[:300]}", **fields)
            lease_mod.release(self.session, self.lease)
            return
        warnings = (job.warnings or []) + [{"stage": stage, "attempt": attempt_no, "category": failure.category, "message": str(failure)[:300], "recovery": action, "reason": outcome.reason[:200]}]
        self._publish(status="queued", error=None, warnings=warnings, progress_message=f"Recovering from {failure.category} at {stage} ({action})", **fields)
        if outcome.backoff_seconds:
            await asyncio.sleep(outcome.backoff_seconds)

    async def run(self, *, max_steps: int = 10_000, until_stage: Optional[str] = None) -> AutonomousNovelJob:
        """Run stages until the job waits for the user, pauses, completes, loses its lease, or reaches ``until_stage``."""
        job = self.job
        for _ in range(max_steps):
            job = await self.step()
            if job.status in TERMINAL or job.status in ("waiting_for_user", "paused"):
                break
            if until_stage and job.stage == until_stage:
                break
        return job


# ----------------------------------------------------------------- service

def create_job(session: Session, *, filename: str, data: bytes, llm_config_id: int, mode: str = "fully_automatic", options: Optional[Dict[str, Any]] = None, role_llm_config_ids: Optional[Dict[str, int]] = None, budget: Optional[Dict[str, Any]] = None, idempotency_key: Optional[str] = None) -> AutonomousNovelJob:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    file_hash = hashlib.sha256(data).hexdigest()
    key = (idempotency_key or "").strip()[:64] or hashlib.sha256(f"{file_hash}|{llm_config_id}|{mode}|{json.dumps(options or {}, sort_keys=True)}".encode()).hexdigest()[:32]
    existing = session.exec(select(AutonomousNovelJob).where(AutonomousNovelJob.idempotency_key == key)).first()
    if existing and (existing.status not in TERMINAL or idempotency_key):
        return existing
    if existing:
        key = hashlib.sha256(f"{key}|{existing.id}".encode()).hexdigest()[:32]
    job = AutonomousNovelJob(idempotency_key=key, status="queued", stage="INGEST", mode=mode, llm_config_id=int(llm_config_id), role_llm_config_ids=role_llm_config_ids or {}, source_filename=filename, source_file_hash=file_hash, source_bytes=data, options=options or {}, budget=budget or {})
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def select_storyline(session: Session, job: AutonomousNovelJob, *, storyline_id: int, chapter_count: int, options: Optional[Dict[str, Any]] = None) -> AutonomousNovelJob:
    if job.selected_storyline_id == int(storyline_id) and job.chapter_count == int(chapter_count) and job.stage != "STORYLINE_SELECTION":
        return job  # idempotent repeat of the same selection
    if job.stage not in ("STORYLINE_SELECTION",) or job.status not in ("waiting_for_user", "queued", "paused"):
        raise ValueError(f"Job is at stage {job.stage} ({job.status}); storyline selection is not pending")
    row = session.get(StorylineCandidate, int(storyline_id))
    if row is None or row.job_id != job.id:
        raise ValueError("Storyline does not belong to this job")
    if row.rejected:
        raise ValueError(f"Storyline was rejected: {row.rejection_reason}")
    if chapter_count < 1 or chapter_count > 400:
        raise ValueError("chapter_count must be between 1 and 400")
    for other in session.exec(select(StorylineCandidate).where(StorylineCandidate.job_id == job.id)).all():
        other.selected = other.id == row.id
        session.add(other)
    job.selected_storyline_id = row.id
    job.chapter_count = int(chapter_count)
    job.options = {**(job.options or {}), **(options or {})}
    job.stage_results = {**(job.stage_results or {}), "approved:STORYLINE_SELECTION": True}
    job.status = "queued"
    job.error = None
    job.lease_owner = None
    job.lease_expires_at = None
    job.updated_at = datetime.now()
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def approve(session: Session, job: AutonomousNovelJob) -> AutonomousNovelJob:
    if job.status != "waiting_for_user":
        if (job.stage_results or {}).get(f"approved:{job.stage}") or job.status in ("queued", "running"):
            return job  # already approved: idempotent
        raise ValueError("Job is not waiting for approval")
    job.stage_results = {**(job.stage_results or {}), f"approved:{job.stage}": True}
    job.status = "queued"
    job.lease_owner = None
    job.lease_expires_at = None
    job.updated_at = datetime.now()
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def request_pause(session: Session, job: AutonomousNovelJob) -> AutonomousNovelJob:
    if job.status in TERMINAL or job.status == "paused":
        return job
    job.status = "paused"
    job.progress_message = "Paused by user"
    job.updated_at = datetime.now()
    session.add(job)
    session.commit()
    return job


def resume(session: Session, job: AutonomousNovelJob, *, budget: Optional[Dict[str, Any]] = None) -> AutonomousNovelJob:
    if job.status in TERMINAL:
        raise ValueError(f"Job is {job.status}")
    if job.status in ("queued", "running") and budget is None:
        return job  # idempotent
    if budget is not None:
        job.budget = {**(job.budget or {}), **budget}
    job.status = "queued"
    job.error = None
    job.lease_owner = None
    job.lease_expires_at = None
    job.updated_at = datetime.now()
    session.add(job)
    session.commit()
    budget_mod.clear_reservations(session, int(job.id))
    session.refresh(job)
    return job


def cancel(session: Session, job: AutonomousNovelJob) -> AutonomousNovelJob:
    if job.status in TERMINAL:
        return job
    job.status = "cancelled"
    job.finished_at = datetime.now()
    job.updated_at = datetime.now()
    session.add(job)
    session.commit()
    return job


def recover_stale_leases(session: Session, *, now: Optional[datetime] = None) -> int:
    """Startup recovery: jobs whose lease has expired go back to 'queued'.

    Jobs whose lease is still valid (another live process is heart-beating them)
    are left alone, so two processes sharing a database never race into duplicate
    execution. Abandoned budget reservations are cleared; actual usage stays.
    """
    now = now or datetime.now()
    n = 0
    for job in session.exec(select(AutonomousNovelJob).where(AutonomousNovelJob.status == "running")).all():
        if job.lease_expires_at and job.lease_expires_at > now:
            continue
        job.status = "queued"
        job.lease_owner = None
        job.lease_expires_at = None
        job.progress_message = "Recovered after restart"
        session.add(job)
        session.commit()
        budget_mod.clear_reservations(session, int(job.id))
        n += 1
    return n


__all__ = ["GATES", "JobLeaseLost", "JobRunner", "LEASE_SECONDS", "MODES", "QUALITY_STATUSES", "STAGES", "STAGE_WEIGHTS", "TERMINAL", "approve", "cancel", "create_job", "default_client_factory", "job_dict", "progress_percent", "quality_verdict", "recover_stale_leases", "request_pause", "resume", "select_storyline", "waiting_for"]
