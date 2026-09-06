"""Source-side stages: INGEST -> STRUCTURE_DETECTION -> SOURCE_ANALYSIS -> ANALYSIS_VERIFICATION
-> (structure / entities / bible / genome) -> FINGERPRINT_BUILD -> EXAMPLE_LIBRARY_BUILD.

Each stage is a plain function ``(session, job_ctx) -> dict`` that is
idempotent: re-running it on an already completed project reuses the stored
artifacts (chapter analysis is scoped to missing/failed chapters, the
fingerprint and example library are keyed by manuscript id). The stages write
exactly the cards the Lab workflow writes, so every existing Forge consumer
(fingerprint, example library, firewall, transfer) works unchanged.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from loguru import logger
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import Session, select

from app.db.models import Card, CardType
from app.schemas.bible import ChapterAnalysis, EntityResolutionPlan, LocalArcPlan, NarrativeArchitecture, NarrativeGenome, StoryStructureMap
from app.schemas.card import CardCreate
from app.services.autonomous import failures as fail
from app.services.autonomous.model_client import ModelClient
from app.services.bible.bible_service import BibleService
from app.services.card_service import CardService
from app.services.forge import analysis as forge_analysis
from app.services.forge.corpus import integrity_report, load_source_chapters, manuscript_meta
from app.services.lab.lab_helpers import (
    ANALYSIS_PROMPT_VERSION,
    fn_lab_analysis_digest,
    fn_lab_analysis_records,
    fn_lab_arc_candidates,
    fn_lab_bible_digest,
    fn_lab_chapter_items,
    fn_lab_emotional_rhythm,
    fn_lab_entity_mentions,
    fn_lab_relationship_items,
    fn_lab_windows,
)
from app.services.lab.manuscript_import import ManuscriptImportService, apply_corrections, detect_chapters, included_chapters
from app.services.prompt_service import get_prompt_by_name, render_prompt
from app.services.workflow.expressions.functions import fn_normalize_ranges

INGEST_VERSION = "ingest-1"
MIN_STORY_FRACTION = 0.5
MIN_ANALYSIS_COVERAGE = 0.9
MIN_EVIDENCE_COVERAGE = 0.5


@dataclass
class SourceContext:
    """Everything the source stages need from the job."""

    source_project_id: int
    filename: str
    data: bytes
    client: ModelClient
    options: Dict[str, Any] = field(default_factory=dict)
    progress: Callable[[str, float], None] = lambda msg, pct: None
    analysis_concurrency: int = 4
    window_size: int = 40
    max_stage_count: int = 24


def _c(card: Card) -> Dict[str, Any]:
    return card.content if isinstance(card.content, dict) else {}


def _type(session: Session, name: str) -> CardType:
    ct = session.exec(select(CardType).where(CardType.name == name)).first()
    if ct is None:
        raise fail.StageFailure(fail.INTERNAL_ERROR, f"Card type '{name}' is not bootstrapped")
    return ct


def _prompt(session: Session, name: str, variables: Dict[str, Any]) -> str:
    p = get_prompt_by_name(session, name)
    if p is None:
        raise fail.StageFailure(fail.INTERNAL_ERROR, f"Prompt '{name}' is missing; it is a required runtime dependency")
    return render_prompt(p.template, variables)


def _upsert_cards(session: Session, project_id: int, type_name: str, items: List[Dict[str, Any]], *, title_key: str, parent_id: Optional[int] = None) -> List[int]:
    """Match-by-title upsert (what ``Card.BatchUpsert`` does in the workflow)."""
    ct = _type(session, type_name)
    existing = {c.title: c for c in session.exec(select(Card).where(Card.project_id == project_id, Card.card_type_id == ct.id)).all()}
    ids: List[int] = []
    for item in items:
        title = str(item.get(title_key) or "").strip()[:200]
        if not title:
            continue
        card = existing.get(title)
        if card is None:
            card = CardService(session).create(CardCreate(title=title, content=item, card_type_id=ct.id, parent_id=parent_id), project_id, commit=False)
            existing[title] = card
        else:
            card.content = {**_c(card), **item}
            flag_modified(card, "content")
            session.add(card)
        ids.append(card.id)
    session.flush()
    return ids


def _create_or_replace_singleton_like(session: Session, project_id: int, type_name: str, title: str, content: Dict[str, Any]) -> int:
    ct = _type(session, type_name)
    card = session.exec(select(Card).where(Card.project_id == project_id, Card.card_type_id == ct.id, Card.title == title)).first()
    if card is None:
        card = CardService(session).create(CardCreate(title=title, content=content, card_type_id=ct.id), project_id, commit=False)
    else:
        card.content = content
        flag_modified(card, "content")
        session.add(card)
    session.flush()
    return card.id


# ------------------------------------------------------------------- INGEST

def ingestion_quality_report(chapters, result) -> Dict[str, Any]:
    """Deterministic quality report computed before any model call."""
    kept = included_chapters(chapters)
    total_words = sum(c.word_count for c in chapters) or 1
    story_words = sum(c.word_count for c in kept)
    counts = [c.word_count for c in kept]
    median = sorted(counts)[len(counts) // 2] if counts else 0
    short = [c.title for c in kept if median and c.word_count < max(120, median * 0.25)]
    long_ = [c.title for c in kept if median and c.word_count > median * 4]
    seen: Dict[str, str] = {}
    duplicates: List[str] = []
    for c in kept:
        key = c.text.strip()[:400]
        if key and key in seen:
            duplicates.append(f"{c.title} duplicates {seen[key]}")
        seen[key] = c.title
    uncertain = [c.title for c in chapters if c.section_type == "unknown"]
    numbered = [c for c in kept if c.number is not None]
    boundary_confidence = round(min(1.0, (len(numbered) / len(kept)) * 0.6 + (0.4 if result.pattern_name not in ("", "none") else 0.15)), 2) if kept else 0.0
    encoding_anomalies = sum(1 for c in kept if "\ufffd" in c.text)
    problems: List[str] = []
    if len(kept) < 3:
        problems.append(f"only {len(kept)} story chapters detected")
    if story_words / total_words < MIN_STORY_FRACTION:
        problems.append(f"only {round(100 * story_words / total_words)}% of the text was classified as story content")
    if duplicates and len(duplicates) > len(kept) * 0.2:
        problems.append(f"{len(duplicates)} duplicated chapters")
    return {
        "version": INGEST_VERSION, "extracted_word_count": story_words, "total_word_count": total_words, "detected_chapter_count": len(kept),
        "excluded_section_count": len(chapters) - len(kept), "suspiciously_short_chapters": short[:20], "suspiciously_long_chapters": long_[:20],
        "duplicated_text": duplicates[:20], "uncertain_sections": uncertain[:20], "story_content_fraction": round(story_words / total_words, 3),
        "encoding_anomalies": encoding_anomalies, "chapter_boundary_confidence": boundary_confidence, "pattern_name": result.pattern_name,
        "warnings": list(result.warnings)[:30], "book_meta": result.book_meta, "blocking_problems": problems, "ok": not problems,
    }


def stage_ingest(session: Session, ctx: SourceContext) -> Dict[str, Any]:
    """Parse + classify + import the EPUB as Chapter Analysis cards (idempotent by content hash)."""
    ctx.progress("Parsing manuscript", 0.0)
    try:
        result = detect_chapters(ctx.filename, ctx.data, exclude_front_matter=True, exclude_afterword=True, min_chapter_words=int(ctx.options.get("min_chapter_words") or 200))
    except Exception as exc:  # noqa: BLE001
        raise fail.StageFailure(fail.INSUFFICIENT_EVIDENCE, f"Manuscript could not be parsed: {exc}")
    chapters = result.chapters
    # Automatic repair: sections classified unknown but long enough are story content.
    corrections: List[Dict[str, Any]] = []
    median = sorted(c.word_count for c in chapters)[len(chapters) // 2] if chapters else 0
    for c in chapters:
        if c.section_type == "unknown" and c.word_count >= max(200, median * 0.5):
            corrections.append({"op": "set_type", "section_id": c.section_id, "section_type": "main_chapter"})
    if corrections:
        chapters = apply_corrections(chapters, corrections)
    report = ingestion_quality_report(chapters, result)
    report["auto_corrections"] = corrections
    if not report["ok"]:
        raise fail.StageFailure(fail.INSUFFICIENT_EVIDENCE, "Manuscript cannot be reliably recovered: " + "; ".join(report["blocking_problems"]), detail=report)
    ctx.progress("Storing chapters", 0.5)
    stored = ManuscriptImportService(session).store_manuscript(
        project_id=ctx.source_project_id, title=result.book_meta.get("title") or ctx.filename, author=result.book_meta.get("creator") or "",
        genre=str(ctx.options.get("genre") or ""), language=result.book_meta.get("language") or "", chapters=chapters, replace_existing=True,
        source_filename=ctx.filename, source_bytes=ctx.data, corrections=corrections,
    )
    session.commit()
    return {"import": stored, "quality": report}


# ----------------------------------------------------------- SOURCE_ANALYSIS

async def stage_source_analysis(session: Session, ctx: SourceContext) -> Dict[str, Any]:
    """Chapter-level analysis for every missing/failed/stale chapter, with bounded concurrency."""
    chapters = load_source_chapters(session, ctx.source_project_id)
    if not chapters:
        raise fail.StageFailure(fail.INSUFFICIENT_EVIDENCE, "No imported chapters to analyse")
    cards = [{"id": ch.card_id, "title": "", "content": {**(ch.analysis or {}), "source_text": ch.text, "chapter_number": ch.chapter_number, "source_text_hash": ch.text_hash, "title": (ch.analysis or {}).get("title") or ch.title, "manuscript_id": ch.manuscript_id, "chapter_id": ch.chapter_id, "language": ch.language, "word_count": (ch.analysis or {}).get("word_count") or 0}} for ch in chapters]
    items = fn_lab_chapter_items(cards, only_missing=True, only_stale=True, prompt_version=ANALYSIS_PROMPT_VERSION)
    # Failed chapters are re-sent (fn_lab_chapter_items already excludes only status == done).
    template = get_prompt_by_name(session, "Lab - Chapter Analysis")
    if template is None:
        raise fail.StageFailure(fail.INTERNAL_ERROR, "Prompt 'Lab - Chapter Analysis' is missing")
    total = len(items)
    done = 0
    results: List[Dict[str, Any]] = []
    sem = asyncio.Semaphore(max(1, int(ctx.analysis_concurrency)))

    async def one(item: Dict[str, Any]) -> Dict[str, Any]:
        nonlocal done
        prompt = template.template.replace("{{content}}", item["content"])
        for k, v in item.items():
            prompt = prompt.replace(f"{{{{item.{k}}}}}", str(v))
        async with sem:
            try:
                ai = await ctx.client.structured(role="source_analyst", schema=ChapterAnalysis, system_prompt="You extract evidence-backed structure from one chapter. Output must validate against the schema.", user_prompt=prompt, prompt_version=ANALYSIS_PROMPT_VERSION, stage=f"SOURCE_ANALYSIS:ch{item['chapter_no']}")
                out = {"ai_result": ai.model_dump(mode="json", exclude={"source_text", "source_chapter_label", "analysis_status", "card_id", "card_title", "word_count"}), "meta": item}
            except fail.StageFailure as exc:
                out = {"error": str(exc), "meta": item}
        done += 1
        ctx.progress(f"Analysed chapter {item['chapter_no']} ({done}/{total})", done / max(1, total))
        return out

    if items:
        results = list(await asyncio.gather(*[one(it) for it in items]))
    records = fn_lab_analysis_records(results)
    # Model output must never overwrite import-time identity fields (the workflow's
    # Card.BatchUpsert has the same rule: keep source_text, add structure).
    protected = ("source_text", "source_text_hash", "manuscript_id", "chapter_id", "included", "is_main_story", "section_type", "normalized_chapter_number", "original_chapter_number", "source_chapter_label", "language", "word_count", "char_count", "flags", "source_label", "volume")
    for rec in records:
        card = session.get(Card, rec.get("card_id"))
        if card is None:
            continue
        old = _c(card)
        merged = {**old, **{k: v for k, v in rec.items() if not (k in protected and not v) and k != "card_id"}}
        for k in protected:
            if k in old:
                merged[k] = old[k]
        card.content = merged
        flag_modified(card, "content")
        session.add(card)
    session.commit()
    status = forge_analysis.analysis_status(session, ctx.source_project_id)
    return {"analysed_now": len([r for r in records if r.get("analysis_status") == "done"]), "failed_now": [r["chapter_number"] for r in records if r.get("analysis_status") == "failed"], "status": status}


def stage_analysis_verification(session: Session, ctx: SourceContext) -> Dict[str, Any]:
    """Re-verify evidence, enforce coverage thresholds, and report what a re-analysis pass should target."""
    ver = forge_analysis.verify_all_analyses(session, ctx.source_project_id)
    status = forge_analysis.analysis_status(session, ctx.source_project_id)
    chapters = load_source_chapters(session, ctx.source_project_id)
    low_confidence = []
    for ch in chapters:
        an = ch.analysis or {}
        total = int(an.get("evidence_total") or 0)
        verified = int(an.get("evidence_verified") or 0)
        if an.get("analysis_status") == "done" and total and verified / total < MIN_EVIDENCE_COVERAGE:
            low_confidence.append(ch.chapter_number)
        if an.get("analysis_status") == "done" and not an.get("scenes"):
            low_confidence.append(ch.chapter_number)
    report = {"verification": ver, "status": status, "low_confidence_chapters": sorted(set(low_confidence)), "integrity": integrity_report(chapters)}
    coverage = status["completeness"]
    if coverage < MIN_ANALYSIS_COVERAGE:
        raise fail.StageFailure(fail.INSUFFICIENT_EVIDENCE, f"Only {round(coverage * 100)}% of chapters have a verified analysis (need {round(MIN_ANALYSIS_COVERAGE * 100)}%)", detail={"failed_chapters": status["failed_chapters"], "low_confidence": report["low_confidence_chapters"]})
    if not report["integrity"]["ok"]:
        raise fail.StageFailure(fail.INTERNAL_CONTRADICTION, "Manuscript integrity check failed", detail=report["integrity"])
    return report


def mark_chapters_for_reanalysis(session: Session, project_id: int, chapter_numbers: List[int]) -> int:
    """Recovery action: flag chapters so the next SOURCE_ANALYSIS run re-sends only them."""
    n = 0
    for ch in load_source_chapters(session, project_id):
        if ch.chapter_number in set(chapter_numbers):
            card = session.get(Card, ch.card_id)
            if card is None:
                continue
            content = _c(card)
            content["analysis_status"] = "failed"
            content["analysis_error"] = "re-analysis requested by verification"
            card.content = content
            flag_modified(card, "content")
            session.add(card)
            n += 1
    session.commit()
    return n


# ----------------------------------------------- whole-book structure + genome

def _verified_records(session: Session, project_id: int) -> List[Dict[str, Any]]:
    recs = []
    for ch in load_source_chapters(session, project_id):
        an = ch.analysis or {}
        if an.get("analysis_status") == "done" and int(an.get("evidence_verified") or 0) > 0:
            recs.append({**an, "chapter_number": ch.chapter_number})
    recs.sort(key=lambda r: r["chapter_number"])
    return recs


async def stage_book_structure(session: Session, ctx: SourceContext) -> Dict[str, Any]:
    """Act/stage detection, entity resolution, Bible reconstruction, emotional rhythm and Narrative Genome.

    Skips work whose cards already exist for the current manuscript (idempotent on resume).
    """
    pid = ctx.source_project_id
    bible = BibleService(session)
    records = _verified_records(session, pid)
    if not records:
        raise fail.StageFailure(fail.INSUFFICIENT_EVIDENCE, "No chapter analysis has verified evidence")
    total = len(records)
    manuscript_id = manuscript_meta(session, pid).get("manuscript_id") or ""
    out: Dict[str, Any] = {"chapters": total}

    def fresh(card: Optional[Card]) -> bool:
        return card is not None and _c(card).get("manuscript_id") == manuscript_id and not _c(card).get("stale")

    # 1. Stages
    structure_card = bible.find_card(pid, "Story Structure Map", "Story Structure Map")
    if fresh(structure_card):
        stages = _c(structure_card).get("stages") or []
    else:
        ctx.progress("Detecting act structure", 0.1)
        windows = fn_lab_windows(records, size=ctx.window_size)
        arc_prompt = get_prompt_by_name(session, "Lab - Local Arc Detection")
        if arc_prompt is None:
            raise fail.StageFailure(fail.INTERNAL_ERROR, "Prompt 'Lab - Local Arc Detection' is missing")
        carry = {"open_arc": "none"}
        arc_results = []
        for w in windows:
            prompt = arc_prompt.template.replace("{{content}}", w["content"])
            for k, v in {**w, **carry}.items():
                prompt = prompt.replace(f"{{{{item.{k}}}}}", str(v)).replace(f"{{{{carry.{k}}}}}", str(v))
            try:
                plan = await ctx.client.structured(role="source_analyst", schema=LocalArcPlan, system_prompt="Detect local narrative arcs in this window of chapter analyses.", user_prompt=prompt, prompt_version="Lab - Local Arc Detection@1", stage="STRUCTURE:arcs")
                arc_results.append({"ai_result": plan.model_dump(mode="json"), "meta": w})
                arcs = plan.arcs
                carry = {"open_arc": json.dumps({"name": arcs[-1].name, "chapter_start": arcs[-1].chapter_start, "summary": arcs[-1].summary[:400]}) if arcs and arcs[-1].open_at_end else "none"}
            except fail.StageFailure as exc:
                logger.warning(f"[Autonomous] arc window {w['chunk_index']} failed: {exc}")
        candidates = fn_lab_arc_candidates(arc_results)
        structure = await ctx.client.structured(role="source_analyst", schema=StoryStructureMap, system_prompt="Reconcile local arc candidates into the book's global stage structure.", user_prompt=_prompt(session, "Lab - Global Stage Reconciliation", {"total_chapters": total, "max_stage_count": ctx.max_stage_count, "arc_candidates": json.dumps(candidates, ensure_ascii=False)}), prompt_version="Lab - Global Stage Reconciliation@1", stage="STRUCTURE:stages")
        stages = fn_normalize_ranges([s.model_dump(mode="json") for s in structure.stages], start=1, end=total)
        if not stages:
            stages = [{"stage_number": 1, "name": "Whole book", "chapter_start": 1, "chapter_end": total, "confidence": 0.3}]
        _create_or_replace_singleton_like(session, pid, "Story Structure Map", "Story Structure Map", {"reconciliation_thinking": structure.reconciliation_thinking, "stages": stages, "volume_hints": structure.volume_hints, "manuscript_id": manuscript_id})
        session.commit()
    out["stages"] = len(stages)

    # 2. Entities
    ctx.progress("Resolving entities", 0.35)
    entity_card = bible.find_card(pid, "Source Analysis Record", "Entity Resolution")
    if fresh(entity_card):
        entities = _c(entity_card).get("entities") or []
    else:
        plan = await ctx.client.structured(role="source_extractor", schema=EntityResolutionPlan, system_prompt="Resolve aliases into canonical entities.", user_prompt=_prompt(session, "Lab - Entity Resolution", {"mentions": fn_lab_entity_mentions(records)}), prompt_version="Lab - Entity Resolution@1", stage="STRUCTURE:entities")
        entities = [e.model_dump(mode="json") for e in plan.entities]
        _create_or_replace_singleton_like(session, pid, "Source Analysis Record", "Entity Resolution", {"entities": entities, "manuscript_id": manuscript_id})
        for etype, type_name, extra in (("character", "Character Card", {"life_span": "Long Term", "role_type": "Supporting Character", "born_scene": "", "personality": "", "core_drive": "", "character_arc": "", "history": []}), ("organization", "Organization Card", {"life_span": "Long Term", "relationship": [], "dynamic_state": []}), ("scene", "Scene Card", {"life_span": "Long Term", "function_in_story": "", "dynamic_state": []}), ("item", "Item Card", {"life_span": "Long Term"})):
            items = [{"name": e["canonical"], "entity_type": etype, "description": e.get("note") or "", "aliases": e.get("aliases") or [], **extra} for e in entities if e.get("entity_type") == etype and float(e.get("confidence") or 0) >= 0.6]
            _upsert_cards(session, pid, type_name, items, title_key="name")
        session.commit()
    out["entities"] = len(entities)

    # 3. Bible reconstruction (threads, promises, facts, timeline, relationships)
    ctx.progress("Reconstructing source Bible", 0.55)
    if not bible.cards_of_type(pid, "Plot Thread") or not fresh(bible.find_card(pid, "Source Analysis Record", "Bible Reconstruction")):
        arch = await ctx.client.structured(role="source_analyst", schema=NarrativeArchitecture, system_prompt="Reconstruct the plot threads, promises, secrets, timeline and relationships with evidence.", user_prompt=_prompt(session, "Lab - Bible Reconstruction", {"total_chapters": total, "entities": json.dumps(entities, ensure_ascii=False)[:20000], "stages": json.dumps(stages, ensure_ascii=False), "analyses_digest": fn_lab_analysis_digest(records, max_chars=120000, per_chapter_chars=600)}), prompt_version="Lab - Bible Reconstruction@1", stage="STRUCTURE:bible")
        data = arch.model_dump(mode="json")
        _upsert_cards(session, pid, "Plot Thread", data.get("plot_threads") or [], title_key="name")
        _upsert_cards(session, pid, "Promise Payoff", data.get("promises") or [], title_key="setup")
        _upsert_cards(session, pid, "Knowledge Fact", data.get("knowledge_facts") or [], title_key="fact")
        _upsert_cards(session, pid, "Timeline Event", data.get("timeline_events") or [], title_key="title")
        _upsert_cards(session, pid, "Relationship Arc", fn_lab_relationship_items(data.get("relationship_arcs") or []), title_key="pair_title")
        _create_or_replace_singleton_like(session, pid, "Source Analysis Record", "Bible Reconstruction", {"digest": fn_lab_bible_digest(data), "manuscript_id": manuscript_id})
        _create_or_replace_singleton_like(session, pid, "Emotional Rhythm", "Emotional Rhythm", {**fn_lab_emotional_rhythm(records), "manuscript_id": manuscript_id})
        session.commit()
    bible_digest = _c(bible.find_card(pid, "Source Analysis Record", "Bible Reconstruction")).get("digest") or ""

    # 4. Genome
    ctx.progress("Extracting narrative genome", 0.8)
    genome_card = bible.singleton(pid, "Narrative Genome")
    if not fresh(genome_card):
        genome = await ctx.client.structured(role="fingerprint_synthesizer", schema=NarrativeGenome, system_prompt="Extract reusable, entity-free narrative mechanisms.", user_prompt=_prompt(session, "Lab - Narrative Genome", {"source_title": manuscript_meta(session, pid).get("title") or "Imported novel", "stages": json.dumps(stages, ensure_ascii=False), "bible_digest": bible_digest, "analyses_digest": fn_lab_analysis_digest(records, max_chars=60000, per_chapter_chars=300)}), prompt_version="Lab - Narrative Genome@1", stage="STRUCTURE:genome")
        _create_or_replace_singleton_like(session, pid, "Narrative Genome", "Narrative Genome", {**genome.model_dump(mode="json"), "manuscript_id": manuscript_id})
        session.commit()
    out["genome_patterns"] = len(_c(bible.singleton(pid, "Narrative Genome")).get("patterns") or [])
    return out


# ---------------------------------------------- FINGERPRINT + EXAMPLE LIBRARY

def stage_fingerprint(session: Session, ctx: SourceContext) -> Dict[str, Any]:
    status = forge_analysis.analysis_status(session, ctx.source_project_id)
    if status["completeness"] < MIN_ANALYSIS_COVERAGE:
        raise fail.StageFailure(fail.INSUFFICIENT_EVIDENCE, f"Source coverage {status['completeness']} is insufficient for a fingerprint")
    fp = status.get("fingerprint")
    if fp and not fp.get("stale") and fp.get("chapters_measured") == status["chapters"]:
        return {"reused": True, **fp}
    try:
        card = forge_analysis.build_fingerprint_card(session, ctx.source_project_id)
    except ValueError as exc:
        raise fail.StageFailure(fail.INSUFFICIENT_EVIDENCE, str(exc))
    c = _c(card)
    return {"reused": False, "card_id": card.id, "version": c.get("version"), "dependency_hash": c.get("dependency_hash"), "layers": len(c.get("layers") or {}), "chapters_measured": c.get("chapters_measured")}


def stage_example_library(session: Session, ctx: SourceContext) -> Dict[str, Any]:
    status = forge_analysis.analysis_status(session, ctx.source_project_id)
    lib = status.get("example_library") or {}
    manuscript_id = status.get("manuscript_id")
    if lib.get("examples") and manuscript_id in (lib.get("manuscripts") or []):
        return {"reused": True, **lib}
    try:
        return {"reused": False, **forge_analysis.build_example_library(session, ctx.source_project_id)}
    except ValueError as exc:
        raise fail.StageFailure(fail.INSUFFICIENT_EVIDENCE, str(exc))


__all__ = [
    "MIN_ANALYSIS_COVERAGE", "MIN_EVIDENCE_COVERAGE", "MIN_STORY_FRACTION", "SourceContext", "ingestion_quality_report", "mark_chapters_for_reanalysis",
    "stage_analysis_verification", "stage_book_structure", "stage_example_library", "stage_fingerprint", "stage_ingest", "stage_source_analysis",
]
