"""CHAPTER_PLAN_BUILD + NOVEL_PREFLIGHT + DOWNSTREAM_REPLAN.

Blueprints are generated in windows (never the whole novel in one prompt),
each window receiving the architecture, the allocation, the previous window's
blueprints and the setups/payoffs due in the window. Blueprints are validated
deterministically against the architecture and written as ``Chapter Outline``
cards in exactly the shape the Forge context compiler consumes (pov, beats,
allowed/forbidden outcomes, word target).

``replan_from`` regenerates the blueprints of chapters > N after chapter N
committed with deviations, protecting chapters already committed and the
ending contract.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import Session, select

from app.db.models import Card, CardType
from app.schemas.autonomous import AUTONOMOUS_SCHEMA_VERSION, ChapterBlueprintBatch
from app.schemas.card import CardCreate
from app.services.autonomous import failures as fail
from app.services.autonomous.architecture import stored_architecture
from app.services.autonomous.model_client import ModelClient
from app.services.bible.bible_service import BibleService
from app.services.card_service import CardService
from app.services.forge import provenance
from app.services.forge.compiler import ChapterContextCompiler, ContextCompileError
from app.services.forge.textmetrics import BEAT_FUNCTIONS

PLAN_PROMPT_VERSION = "autonomous-chapter-plan-1"
WINDOW = 8
DEFAULT_WORDS_PER_CHAPTER = 2500

SYSTEM_PROMPT = (
    "You are the chapter planner of an original novel. For each requested chapter produce a complete blueprint: purpose, POV (a listed character), location (a listed location), "
    "4-8 ordered beats with a function tag from the allowed list and 2-6 keywords each, reveals/setups/payoffs consistent with the architecture's setup-payoff schedule, "
    "state/relationship/knowledge transitions, target tension, a closing hook, allowed_outcomes (persistent facts this chapter establishes) and forbidden_outcomes "
    "(facts reserved for later chapters, especially every knowledge fact whose reveal chapter is later). Use only listed character and location names. "
    "Never reuse anything from the reference novel. Output must validate against the schema."
)


def _c(card: Optional[Card]) -> Dict[str, Any]:
    return card.content if card is not None and isinstance(card.content, dict) else {}


def _type(session: Session, name: str) -> CardType:
    ct = session.exec(select(CardType).where(CardType.name == name)).first()
    if ct is None:
        raise fail.StageFailure(fail.INTERNAL_ERROR, f"Card type '{name}' is not bootstrapped")
    return ct


def words_per_chapter(options: Dict[str, Any], chapter_count: int) -> int:
    if options.get("words_per_chapter"):
        return max(300, int(options["words_per_chapter"]))
    if options.get("total_words"):
        return max(300, int(int(options["total_words"]) / max(1, chapter_count)))
    return DEFAULT_WORDS_PER_CHAPTER


# --------------------------------------------------------------- validation

def validate_blueprints(chapters: Sequence[Dict[str, Any]], arch: Dict[str, Any], *, expected: Sequence[int]) -> List[Dict[str, Any]]:
    problems: List[Dict[str, Any]] = []
    names = {str(c.get("name") or "").strip().lower() for c in arch.get("characters") or []}
    for c in arch.get("characters") or []:
        for a in c.get("aliases") or []:
            names.add(str(a).strip().lower())
    locs = {str(l.get("name") or "").strip().lower() for l in arch.get("locations") or []}
    by_num = {int(ch.get("chapter_number") or 0): ch for ch in chapters}
    for n in expected:
        ch = by_num.get(n)
        if ch is None:
            problems.append({"code": "chapter_missing", "chapter": n, "message": f"No blueprint for chapter {n}"})
            continue
        if str(ch.get("pov") or "").strip().lower() not in names:
            problems.append({"code": "pov_invalid", "chapter": n, "message": f"POV '{ch.get('pov')}' is not a listed character"})
        for p in ch.get("participants") or []:
            if str(p).strip().lower() not in names:
                problems.append({"code": "participant_invalid", "chapter": n, "message": f"Participant '{p}' is not a listed character"})
        if ch.get("location") and str(ch["location"]).strip().lower() not in locs:
            problems.append({"code": "location_invalid", "chapter": n, "message": f"Location '{ch.get('location')}' is not a listed location"})
        beats = ch.get("beats") or []
        if len(beats) < 3:
            problems.append({"code": "beats_too_few", "chapter": n, "message": f"Chapter {n} has {len(beats)} beats (need >= 3)"})
        for b in beats:
            if str(b.get("function") or "") not in BEAT_FUNCTIONS:
                problems.append({"code": "beat_function_invalid", "chapter": n, "message": f"Beat function '{b.get('function')}' is not in the allowed list"})
            if not (b.get("description") or "").strip():
                problems.append({"code": "beat_empty", "chapter": n, "message": "Empty beat description"})
        if len(str(ch.get("overview") or "")) < 100:
            problems.append({"code": "overview_short", "chapter": n, "message": f"Chapter {n} overview is shorter than 100 characters"})
    # Setup/payoff schedule adherence.
    sched = {(int(s.get("payoff_chapter") or 0)): s for s in arch.get("setups_payoffs") or []}
    for pc, sp in sched.items():
        if pc in by_num and not (by_num[pc].get("payoffs") or []):
            problems.append({"code": "payoff_unplanned", "chapter": pc, "message": f"Chapter {pc} should pay off '{str(sp.get('setup'))[:60]}' but lists no payoffs"})
    # Knowledge boundaries: facts must not be revealed before their reveal chapter.
    for k in arch.get("knowledge_facts") or []:
        rc = int(k.get("reader_reveal_chapter") or 0)
        if not rc:
            continue
        fact = str(k.get("fact") or "").strip().lower()
        for n in expected:
            ch = by_num.get(n)
            if ch and n < rc and any(fact and fact[:40] in str(r).lower() for r in ch.get("reveals") or []):
                problems.append({"code": "early_reveal", "chapter": n, "message": f"Chapter {n} reveals '{fact[:60]}' planned for chapter {rc}"})
    return problems


# ------------------------------------------------------------------ prompt

def _arch_digest(arch: Dict[str, Any]) -> str:
    lines = ["[STORY CONTRACT]", json.dumps(arch.get("contract") or {}, ensure_ascii=False)]
    lines.append("\n[CHARACTERS] (use these exact names)")
    for c in arch.get("characters") or []:
        arc = "; ".join(f"{p.get('phase')}@{p.get('chapter_hint')}: {p.get('state')}" for p in c.get("arc") or [])
        lines.append(f"- {c.get('name')} ({c.get('role')}): goal={c.get('goal')}; flaw={c.get('flaw')}; secret={c.get('secret')}; must not know at start={c.get('knowledge_boundaries')}; arc={arc}; intro ch {c.get('introduction_chapter')}")
    lines.append("\n[LOCATIONS] " + ", ".join(str(l.get("name")) for l in arch.get("locations") or []))
    lines.append("\n[KNOWLEDGE FACTS] (reader_reveal_chapter is the earliest chapter the reader may learn it)")
    for k in arch.get("knowledge_facts") or []:
        lines.append(f"- {k.get('fact')} | knowers at start: {k.get('knowers_at_start')} | reader reveal ch {k.get('reader_reveal_chapter')}")
    lines.append("\n[PLOT THREADS]")
    for t in arch.get("plot_threads") or []:
        lines.append(f"- {t.get('name')} ({t.get('thread_type')}): {t.get('central_question')}; ch {t.get('opening_chapter')} -> {t.get('resolution_chapter')}")
    lines.append("\n[SETUP -> PAYOFF SCHEDULE]")
    for s in arch.get("setups_payoffs") or []:
        lines.append(f"- ch {s.get('setup_chapter')}: {s.get('setup')}  =>  ch {s.get('payoff_chapter')}: {s.get('payoff')}")
    lines.append("\n[RELATIONSHIPS]")
    for r in arch.get("relationships") or []:
        lines.append(f"- {r.get('character_a')} ↔ {r.get('character_b')}: {r.get('private_relationship')}; tension={r.get('unresolved_tension')}; arc={r.get('arc_summary')}")
    lines.append("\n[ACT ALLOCATION]")
    for a in arch.get("allocation") or []:
        lines.append(f"- {a.get('function')}: chapters {a.get('chapter_start')}-{a.get('chapter_end')}")
    lines += ["\n[ACT PLAN]"] + [f"- {x}" for x in arch.get("act_plan") or []]
    return "\n".join(lines)


def build_prompt(arch: Dict[str, Any], *, chapters: Sequence[int], total: int, word_target: int, previous: Sequence[Dict[str, Any]], committed_summaries: Sequence[Dict[str, Any]] = (), problems: Sequence[Dict[str, Any]] = (), drafts: Optional[Sequence[Dict[str, Any]]] = None) -> str:
    parts = [_arch_digest(arch), f"\n[ALLOWED BEAT FUNCTIONS]\n{', '.join(BEAT_FUNCTIONS)}"]
    if committed_summaries:
        parts.append("\n[ALREADY WRITTEN CHAPTERS — immutable; plan continuity from their actual state]")
        for s in committed_summaries:
            parts.append(f"- ch {s.get('chapter_number')}: {str(s.get('summary') or '')[:500]} | ends at {s.get('ending_location')} | open: {s.get('unresolved_immediate_action')}")
    if previous:
        parts.append("\n[PREVIOUS BLUEPRINTS — continue from these]")
        for p in previous[-3:]:
            parts.append(f"- ch {p.get('chapter_number')} '{p.get('title')}': {str(p.get('overview') or '')[:400]} | hook: {p.get('closing_hook')}")
    parts.append(f"\n[TASK]\nPlan chapters {chapters[0]}-{chapters[-1]} of {total}. Target length per chapter: about {word_target} words. Produce one blueprint per chapter, in order, each with 4-8 beats.")
    if problems and drafts is not None:
        parts += ["\n[PREVIOUS BLUEPRINTS FAILED VALIDATION — fix ONLY these problems, keep everything else]"] + [f"- ch {p.get('chapter')}: {p['message']}" for p in problems[:30]]
        parts += ["\n[PREVIOUS BLUEPRINTS]", json.dumps(list(drafts), ensure_ascii=False)[:50000]]
    return "\n".join(parts)


# -------------------------------------------------------------- outline cards

def blueprint_to_outline(bp: Dict[str, Any], *, arch: Dict[str, Any], word_target: int, total: int) -> Dict[str, Any]:
    n = int(bp["chapter_number"])
    alloc = next((a for a in arch.get("allocation") or [] if int(a.get("chapter_start") or 0) <= n <= int(a.get("chapter_end") or 0)), None)
    stage_no = (arch.get("allocation") or []).index(alloc) + 1 if alloc else 1
    participants = list(dict.fromkeys([bp.get("pov")] + list(bp.get("participants") or [])))
    entity_list = participants + ([bp["location"]] if bp.get("location") else [])
    forbidden = list(bp.get("forbidden_outcomes") or [])
    for k in arch.get("knowledge_facts") or []:
        rc = int(k.get("reader_reveal_chapter") or 0)
        if rc and n < rc and k.get("fact") and str(k["fact"]) not in forbidden:
            forbidden.append(str(k["fact"]))
    allowed = list(bp.get("allowed_outcomes") or []) + [str(x) for x in (bp.get("reveals") or []) + (bp.get("payoffs") or [])]
    overview = str(bp.get("overview") or "")
    if len(overview) < 100:
        overview = (overview + " " + " ".join(str(b.get("description") or "") for b in bp.get("beats") or [])).strip()
    return {
        "volume_number": 1, "stage_number": stage_no, "title": bp.get("title") or f"Chapter {n}", "chapter_number": n, "overview": overview, "entity_list": entity_list,
        "pov": bp.get("pov"), "participants": participants, "beats": [{"function": b.get("function"), "description": b.get("description"), "keywords": b.get("keywords") or []} for b in bp.get("beats") or []],
        "allowed_outcomes": allowed, "forbidden_outcomes": forbidden, "word_target": word_target,
        "purpose": bp.get("purpose"), "location": bp.get("location"), "story_time": bp.get("story_time"), "opening_state": bp.get("opening_state"), "goal": bp.get("goal"), "conflict": bp.get("conflict"),
        "reveals": bp.get("reveals") or [], "setups": bp.get("setups") or [], "payoffs": bp.get("payoffs") or [], "character_state_transition": bp.get("character_state_transition"), "relationship_transition": bp.get("relationship_transition"),
        "knowledge_transition": bp.get("knowledge_transition"), "target_tension": bp.get("target_tension"), "emotional_movement": bp.get("emotional_movement"), "closing_hook": bp.get("closing_hook"), "schema_version": AUTONOMOUS_SCHEMA_VERSION,
    }


def write_outline_cards(session: Session, project_id: int, outlines: Sequence[Dict[str, Any]]) -> Dict[int, int]:
    ct = _type(session, "Chapter Outline")
    existing = {int(_c(c).get("chapter_number") or 0): c for c in session.exec(select(Card).where(Card.project_id == project_id, Card.card_type_id == ct.id)).all()}
    ids: Dict[int, int] = {}
    for oc in outlines:
        n = int(oc["chapter_number"])
        card = existing.get(n)
        title = f"Ch {n:03d} · {oc.get('title') or ''}"[:200]
        if card is None:
            card = CardService(session).create(CardCreate(title=title, content=oc, card_type_id=ct.id), project_id, commit=False)
            existing[n] = card
        else:
            card.title = title
            card.content = oc
            flag_modified(card, "content")
            session.add(card)
        session.flush()
        provenance.record(session, project_id=project_id, artifact_kind="Chapter Outline", artifact_key=str(card.id), content=oc, upstream=[], producer=PLAN_PROMPT_VERSION, model_role="chapter_planner", prompt_version=PLAN_PROMPT_VERSION, schema_version=AUTONOMOUS_SCHEMA_VERSION, card_id=card.id)
        ids[n] = card.id
    return ids


def committed_summaries(session: Session, project_id: int, *, upto: int) -> List[Dict[str, Any]]:
    out = []
    for card in BibleService(session).cards_of_type(project_id, "Chapter Text"):
        c = _c(card)
        n = int(c.get("chapter_number") or 0)
        if 0 < n <= upto and c.get("sync_status") == "synchronized":
            out.append({"chapter_number": n, "summary": c.get("summary"), "ending_location": c.get("ending_location"), "unresolved_immediate_action": c.get("unresolved_immediate_action")})
    out.sort(key=lambda x: x["chapter_number"])
    return out


def existing_outlines(session: Session, project_id: int) -> Dict[int, Dict[str, Any]]:
    return {int(_c(c).get("chapter_number") or 0): _c(c) for c in BibleService(session).cards_of_type(project_id, "Chapter Outline") if _c(c).get("beats")}


# ------------------------------------------------------------------- stages

async def plan_range(session: Session, *, project_id: int, arch: Dict[str, Any], client: ModelClient, chapters: Sequence[int], total: int, word_target: int, previous: Sequence[Dict[str, Any]], committed: Sequence[Dict[str, Any]], stage: str, max_rounds: int = 3) -> List[Dict[str, Any]]:
    problems: List[Dict[str, Any]] = []
    drafts: Optional[List[Dict[str, Any]]] = None
    for _ in range(max_rounds):
        result = await client.structured(role="chapter_planner", schema=ChapterBlueprintBatch, system_prompt=SYSTEM_PROMPT, user_prompt=build_prompt(arch, chapters=chapters, total=total, word_target=word_target, previous=previous, committed_summaries=committed, problems=problems, drafts=drafts), prompt_version=PLAN_PROMPT_VERSION, stage=stage)
        drafts = [c.model_dump(mode="json") for c in result.chapters if int(c.chapter_number) in set(chapters)]
        problems = validate_blueprints(drafts, arch, expected=chapters)
        if not problems:
            return drafts
    raise fail.StageFailure(fail.PLANNING_IMPOSSIBILITY, f"Blueprints for chapters {chapters[0]}-{chapters[-1]} still invalid after {max_rounds} rounds", detail={"problems": problems[:30]})


async def stage_chapter_plan(session: Session, *, project_id: int, chapter_count: int, client: ModelClient, options: Dict[str, Any], progress=lambda m, p: None, from_chapter: int = 1) -> Dict[str, Any]:
    """Plan chapters ``from_chapter..chapter_count`` in windows, reusing valid existing outlines below ``from_chapter``."""
    arch = stored_architecture(session, project_id)
    if not arch:
        raise fail.StageFailure(fail.STALE_DEPENDENCY, "No stored architecture")
    word_target = words_per_chapter(options, chapter_count)
    have = existing_outlines(session, project_id)
    previous = [have[n] for n in sorted(have) if n < from_chapter]
    committed = committed_summaries(session, project_id, upto=from_chapter - 1)
    planned: Dict[int, int] = {}
    # Resume is idempotent: outlines that already exist are reused; a replan from N regenerates N..count.
    if from_chapter <= 1:
        pending = [n for n in range(1, chapter_count + 1) if n not in have]
    else:
        pending = list(range(from_chapter, chapter_count + 1))
    windows = [pending[i:i + WINDOW] for i in range(0, len(pending), WINDOW)]
    done = 0
    for w in windows:
        progress(f"Planning chapters {w[0]}-{w[-1]}", done / max(1, len(pending)))
        bps = await plan_range(session, project_id=project_id, arch=arch, client=client, chapters=w, total=chapter_count, word_target=word_target, previous=previous, committed=committed, stage=f"CHAPTER_PLAN_BUILD:{w[0]}-{w[-1]}")
        outlines = [blueprint_to_outline(bp, arch=arch, word_target=word_target, total=chapter_count) for bp in bps]
        planned.update(write_outline_cards(session, project_id, outlines))
        session.commit()
        previous = list(previous) + outlines
        done += len(w)
    # Remove stray outlines beyond the requested count (e.g. after a count change).
    ct = _type(session, "Chapter Outline")
    for card in session.exec(select(Card).where(Card.project_id == project_id, Card.card_type_id == ct.id)).all():
        if int(_c(card).get("chapter_number") or 0) > chapter_count:
            session.delete(card)
    session.commit()
    return {"planned_now": len(planned), "outline_count": len(existing_outlines(session, project_id)), "word_target": word_target}


def stage_preflight(session: Session, *, project_id: int, chapter_count: int) -> Dict[str, Any]:
    """Compile chapter 1 without calling a model; every fail-closed condition surfaces here."""
    outlines = existing_outlines(session, project_id)
    missing = [n for n in range(1, chapter_count + 1) if n not in outlines]
    if missing:
        raise fail.StageFailure(fail.PLANNING_IMPOSSIBILITY, f"{len(missing)} chapter(s) have no outline", detail={"missing": missing[:30]})
    manifest = provenance.get_manifest(session, project_id, create=True)
    next_ch = max(1, int(manifest.latest_committed_chapter) + 1)
    if next_ch > chapter_count:
        return {"ok": True, "next_chapter": next_ch, "complete": True}
    try:
        ctx = ChapterContextCompiler(session).compile(project_id=project_id, chapter_number=next_ch)
        session.rollback()
    except ContextCompileError as exc:
        session.rollback()
        cat = fail.STALE_DEPENDENCY if exc.code in ("stale_dependencies", "stale_canon_revision") else fail.PLANNING_IMPOSSIBILITY
        raise fail.StageFailure(cat, f"Preflight compile of chapter {next_ch} failed: {exc}", detail=exc.as_dict())
    return {"ok": True, "next_chapter": next_ch, "context_chars": len(ctx.prompt_text()), "examples": len(ctx.retrieved_examples)}


async def replan_from(session: Session, *, project_id: int, chapter_count: int, client: ModelClient, options: Dict[str, Any], after_chapter: int, reasons: Sequence[str]) -> Dict[str, Any]:
    """Regenerate blueprints for chapters > ``after_chapter`` using the committed state (DOWNSTREAM_REPLAN)."""
    if after_chapter >= chapter_count:
        return {"replanned": 0}
    arch = stored_architecture(session, project_id)
    arch = {**arch, "replan_reasons": list(reasons)}
    word_target = words_per_chapter(options, chapter_count)
    have = existing_outlines(session, project_id)
    previous = [have[n] for n in sorted(have) if n <= after_chapter]
    committed = committed_summaries(session, project_id, upto=after_chapter)
    # Replan only the next window; later windows are replanned when reached if still deviating.
    pending = list(range(after_chapter + 1, min(chapter_count, after_chapter + WINDOW) + 1))
    bps = await plan_range(session, project_id=project_id, arch=arch, client=client, chapters=pending, total=chapter_count, word_target=word_target, previous=previous, committed=committed, stage=f"DOWNSTREAM_REPLAN:{pending[0]}-{pending[-1]}")
    outlines = [blueprint_to_outline(bp, arch=arch, word_target=word_target, total=chapter_count) for bp in bps]
    write_outline_cards(session, project_id, outlines)
    session.commit()
    return {"replanned": len(outlines), "chapters": pending, "reasons": list(reasons)}


__all__ = ["DEFAULT_WORDS_PER_CHAPTER", "PLAN_PROMPT_VERSION", "WINDOW", "blueprint_to_outline", "build_prompt", "committed_summaries", "existing_outlines", "plan_range", "replan_from", "stage_chapter_plan", "stage_preflight", "validate_blueprints", "words_per_chapter", "write_outline_cards"]
