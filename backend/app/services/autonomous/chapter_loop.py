"""CHAPTER_GENERATION_LOOP: one chapter per call so the runner can checkpoint.

Each call runs the Forge pipeline (compile -> draft -> extract -> validate ->
repair -> revalidate -> commit -> sync) for the manifest's next chapter, then
reforecasts: it compares the committed chapter with the blueprint and, when
the deviation is material, triggers ``replan_from`` for the next window.

Automatic policy for a rejected chapter (blockers remain after the pipeline's
in-run repairs): regenerate with ``regenerate=True`` up to
``MAX_CHAPTER_REGENERATIONS`` times, then fail the stage with the right
category so the runner's ladder (reduce scope -> fallback model -> pause)
applies.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from sqlmodel import Session, select

from app.db.models import Card, ChapterPipelineRun
from app.services.autonomous import failpoints
from app.services.autonomous import failures as fail
from app.services.autonomous.chapter_plan import existing_outlines, replan_from
from app.services.autonomous.model_client import ForgeDrafterAdapter, ModelClient
from app.services.forge import provenance
from app.services.forge.pipeline import PipelineOptions, PipelineResult, run_chapter

MAX_CHAPTER_REGENERATIONS = 2
WORD_DRIFT_TOLERANCE = 0.35
ACCEPTED_WARNING_SEVERITIES = ("medium", "low")


def _c(card: Optional[Card]) -> Dict[str, Any]:
    return card.content if card is not None and isinstance(card.content, dict) else {}


def _category_for(result: PipelineResult) -> str:
    err = result.error or {}
    blocking = err.get("blocking") or []
    codes = {b.get("code") for b in blocking if isinstance(b, dict)}
    layers = {b.get("layer") for b in blocking if isinstance(b, dict)}
    if result.status == "compile_failed":
        return fail.STALE_DEPENDENCY if err.get("code") in ("stale_dependencies", "stale_canon_revision") else fail.PLANNING_IMPOSSIBILITY
    if "originality" in layers or codes & {"source_entity_leak", "entity_overlap", "long_phrase_overlap", "dialogue_overlap", "accidental_quotation"}:
        return fail.ORIGINALITY_VIOLATION
    if layers & {"fact", "pov", "character", "temporal", "outline", "entity"}:
        return fail.CONTINUITY_VIOLATION
    if result.status == "error":
        return fail.classify_exception(RuntimeError(err.get("message") or "pipeline error"))
    return fail.CONTINUITY_VIOLATION


def deviation_report(session: Session, project_id: int, chapter_number: int, result: PipelineResult, *, word_target: int) -> Dict[str, Any]:
    """Compare the committed chapter with its blueprint."""
    outline = existing_outlines(session, project_id).get(chapter_number) or {}
    metrics = (result.validation or {}).get("metrics") or {}
    words = int(metrics.get("unit_count") or len((result.prose or "").split()))
    reasons: List[str] = []
    if word_target and abs(words - word_target) / float(word_target) > WORD_DRIFT_TOLERANCE:
        reasons.append(f"word count {words} deviates from target {word_target}")
    sync = result.sync or {}
    committed_facts = sync.get("committed") or sync.get("facts_committed") or []
    planned_payoffs = [str(p).lower() for p in outline.get("payoffs") or []]
    prose_l = (result.prose or "").lower()
    missed = [p for p in planned_payoffs if p and not any(tok in prose_l for tok in p.split()[:3] if len(tok) > 3)]
    if missed:
        reasons.append(f"planned payoff(s) not evidenced: {missed[:3]}")
    style = result.style or {}
    if style.get("adherence_score") is not None and float(style["adherence_score"]) < 0.5:
        reasons.append(f"style adherence {style['adherence_score']} below 0.5")
    warnings = [i for i in ((result.validation or {}).get("issues") or []) if i.get("severity") in ACCEPTED_WARNING_SEVERITIES]
    return {"chapter": chapter_number, "words": words, "word_target": word_target, "reasons": reasons, "material": bool(missed) or (word_target and abs(words - word_target) / float(word_target) > 0.6), "accepted_warnings": [{"code": w.get("code"), "severity": w.get("severity"), "rationale": "below blocking threshold; accepted by policy"} for w in warnings][:20], "facts_committed": len(committed_facts) if isinstance(committed_facts, list) else committed_facts}


async def generate_next_chapter(session: Session, *, project_id: int, chapter_count: int, client: ModelClient, options: Dict[str, Any], word_target: int, budget_chars: int = 16000, lease_check: Optional[Callable[[], None]] = None) -> Dict[str, Any]:
    check = lease_check or (lambda: None)
    manifest = provenance.get_manifest(session, project_id, create=True)
    n = int(manifest.latest_committed_chapter) + 1
    if n > chapter_count:
        return {"complete": True, "chapter": None}
    drafter = ForgeDrafterAdapter(client)
    pipeline_opts = PipelineOptions(max_repairs=int(options.get("max_repairs") or 2), budget_chars=budget_chars, word_target=word_target, regenerate=False)
    result: Optional[PipelineResult] = None
    for attempt in range(MAX_CHAPTER_REGENERATIONS + 1):
        if attempt:
            pipeline_opts = PipelineOptions(max_repairs=int(options.get("max_repairs") or 2) + 1, budget_chars=budget_chars, word_target=word_target, regenerate=True)
        check()
        result = await run_chapter(session, project_id=project_id, chapter_number=n, drafter=drafter, options=pipeline_opts)
        failpoints.hit("after_chapter_commit")
        if result.status == "committed":
            break
        if result.status in ("compile_failed",):
            break
    assert result is not None
    if result.status != "committed":
        raise fail.StageFailure(_category_for(result), f"Chapter {n} ended in status '{result.status}': {(result.error or {}).get('message') or (result.error or {}).get('code')}", detail={"chapter": n, "run_id": result.run_id, "error": result.error, "blocking": ((result.error or {}).get("blocking") or [])[:10]})
    check()
    dev = deviation_report(session, project_id, n, result, word_target=word_target)
    replan: Dict[str, Any] = {"replanned": 0}
    if dev["material"] and n < chapter_count:
        replan = await replan_from(session, project_id=project_id, chapter_count=chapter_count, client=client, options=options, after_chapter=n, reasons=dev["reasons"])
    return {"complete": n >= chapter_count, "chapter": n, "run_id": result.run_id, "chapter_card_id": result.chapter_card_id, "model_calls": result.model_calls, "repair_attempts": result.repair_attempts, "style_score": (result.style or {}).get("adherence_score"), "validation_passed": (result.validation or {}).get("passed"), "deviation": dev, "replan": replan}


def loop_status(session: Session, project_id: int, chapter_count: int) -> Dict[str, Any]:
    manifest = provenance.get_manifest(session, project_id, create=True)
    runs = session.exec(select(ChapterPipelineRun).where(ChapterPipelineRun.project_id == project_id).order_by(ChapterPipelineRun.id.desc()).limit(200)).all()
    return {"committed": int(manifest.latest_committed_chapter), "chapter_count": chapter_count, "runs": len(runs), "rejected_runs": sum(1 for r in runs if r.status == "rejected"), "model_calls": sum(r.model_calls for r in runs), "repair_attempts": sum(r.repair_attempts for r in runs)}


__all__ = ["ACCEPTED_WARNING_SEVERITIES", "MAX_CHAPTER_REGENERATIONS", "WORD_DRIFT_TOLERANCE", "deviation_report", "generate_next_chapter", "loop_status"]
