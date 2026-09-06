"""NOVEL_ARCHITECTURE + BIBLE_BUILD.

Expands the selected storyline + chapter count into a complete original
architecture (Story Contract, Character/World Bibles, plot graph, timeline,
knowledge and relationship matrices), validates it deterministically, asks the
architect to repair it when validation fails, and finally writes the Bible
cards the Forge context compiler reads and seeds chapter-0 canon.

Chapter-count adaptation happens here: the act allocation is computed from
the requested count and given to the architect as hard chapter ranges.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import Session, select

from app.db.models import CanonFact, Card, CardType
from app.schemas.autonomous import AUTONOMOUS_SCHEMA_VERSION, NovelArchitecture
from app.schemas.card import CardCreate
from app.services.autonomous import failures as fail
from app.services.autonomous.model_client import ModelClient
from app.services.bible.bible_service import BibleService
from app.services.card_service import CardService
from app.services.forge import canon as canon_store
from app.services.forge import firewall as fw
from app.services.forge import provenance, transfer

ARCHITECTURE_PROMPT_VERSION = "autonomous-architecture-1"
ARCHITECTURE_CARD_TYPE = "Novel Architecture"
ARCHITECTURE_TITLE = "Novel Architecture"
MAX_ARCHITECT_ROUNDS = 3

SYSTEM_PROMPT = (
    "You are the architect of an original novel. Expand the selected storyline into a complete, internally consistent architecture that a chapter planner can "
    "turn into exactly the requested number of chapters. Invent every name, place, object and event; never reuse anything from the reference novel. "
    "Every setup must have a payoff chapter, every payoff an earlier setup chapter; character arcs need start/mid/end states with chapter hints; knowledge "
    "facts list who knows them at the start and when the reader learns them; relationships list both characters by exact name. Use only character names "
    "that appear in `characters`, and only locations that appear in `locations`. Output must validate against the schema."
)


def _c(card: Optional[Card]) -> Dict[str, Any]:
    return card.content if card is not None and isinstance(card.content, dict) else {}


def _type(session: Session, name: str) -> CardType:
    ct = session.exec(select(CardType).where(CardType.name == name)).first()
    if ct is None:
        raise fail.StageFailure(fail.INTERNAL_ERROR, f"Card type '{name}' is not bootstrapped")
    return ct


# --------------------------------------------------------- chapter allocation

ACT_TEMPLATE: Tuple[Tuple[str, float], ...] = (
    ("opening_setup", 0.08), ("inciting_incident", 0.06), ("first_act_turn", 0.10), ("rising_complications", 0.22),
    ("midpoint", 0.08), ("second_escalation", 0.18), ("crisis", 0.10), ("climax", 0.10), ("resolution", 0.08),
)


def allocate_chapters(chapter_count: int, *, source_proportions: Optional[Sequence[float]] = None) -> List[Dict[str, Any]]:
    """Distribute ``chapter_count`` chapters over the dramatic functions.

    Proportions default to the template; when the source fingerprint exposes
    stage proportions they are blended in (50/50) so the reference's pacing
    profile shapes the plan without dictating its content. Every function gets
    at least one chapter when the count allows it; small counts merge functions.
    """
    n = max(1, int(chapter_count))
    weights = [w for _, w in ACT_TEMPLATE]
    if source_proportions and len(source_proportions) == len(weights):
        total = sum(source_proportions) or 1.0
        weights = [0.5 * w + 0.5 * (p / total) for w, p in zip(weights, source_proportions)]
    names = [nm for nm, _ in ACT_TEMPLATE]
    if n < len(names):
        # Merge into fewer functions: setup, escalation, climax, resolution.
        merged = [("setup", weights[0] + weights[1] + weights[2]), ("escalation", weights[3] + weights[4] + weights[5]), ("crisis_climax", weights[6] + weights[7]), ("resolution", weights[8])]
        if n < 4:
            merged = [("setup", 0.34), ("escalation_climax", 0.5), ("resolution", 0.16)][:max(1, n)]
        names, weights = [m[0] for m in merged], [m[1] for m in merged]
    raw = [w * n for w in weights]
    counts = [max(1, int(x)) for x in raw]
    while sum(counts) > n:
        i = max(range(len(counts)), key=lambda k: (counts[k] - raw[k], counts[k]))
        counts[i] -= 1
    while sum(counts) < n:
        i = max(range(len(counts)), key=lambda k: raw[k] - counts[k])
        counts[i] += 1
    out: List[Dict[str, Any]] = []
    start = 1
    for name, cnt in zip(names, counts):
        out.append({"function": name, "chapter_start": start, "chapter_end": start + cnt - 1, "chapters": cnt})
        start += cnt
    return out


def source_proportions(session: Session, source_project_id: Optional[int]) -> Optional[List[float]]:
    if not source_project_id:
        return None
    structure = _c(BibleService(session).find_card(source_project_id, "Story Structure Map", "Story Structure Map"))
    stages = [s for s in (structure.get("stages") or []) if isinstance(s, dict)]
    if len(stages) < 3:
        return None
    total = max(int(s.get("chapter_end") or 0) for s in stages) or 1
    curve = []
    for s in stages:
        curve.append((int(s.get("chapter_end") or 0) - int(s.get("chapter_start") or 1) + 1) / total)
    # Resample the stage curve onto the 9 template slots.
    slots = len(ACT_TEMPLATE)
    out = []
    for i in range(slots):
        lo, hi = i / slots, (i + 1) / slots
        acc = 0.0
        pos = 0.0
        for frac in curve:
            s0, s1 = pos, pos + frac
            overlap = max(0.0, min(hi, s1) - max(lo, s0))
            acc += overlap
            pos = s1
        out.append(acc)
    return out


def fit_report(storyline: Dict[str, Any], chapter_count: int) -> Dict[str, Any]:
    """Whether the requested count fits the option, and which adaptations apply."""
    lo = int(storyline.get("chapter_suitability_min") or 0) or 8
    hi = int(storyline.get("chapter_suitability_max") or 0) or 40
    adaptations: List[str] = []
    severity = "none"
    if chapter_count < lo:
        ratio = chapter_count / float(lo)
        adaptations += ["compress minor subplots", "combine compatible beats", "reduce scene count per chapter"]
        if ratio < 0.7:
            adaptations += ["reduce cast size", "merge secondary characters"]
        severity = "warning" if ratio < 0.5 else "adapted"
    elif chapter_count > hi:
        ratio = chapter_count / float(hi)
        adaptations += ["add subplot depth", "expand complications across more chapters", "lower words per chapter"]
        if ratio > 1.6:
            adaptations += ["add a secondary POV or subplot thread"]
        severity = "warning" if ratio > 2.0 else "adapted"
    return {"requested": chapter_count, "recommended_min": lo, "recommended_max": hi, "adaptations": adaptations, "severity": severity}


# --------------------------------------------------------------- validation

def validate_architecture(arch: Dict[str, Any], *, chapter_count: int) -> List[Dict[str, Any]]:
    """Deterministic architecture checks. Returns problems (empty = valid)."""
    problems: List[Dict[str, Any]] = []
    chars = [c for c in arch.get("characters") or [] if isinstance(c, dict) and c.get("name")]
    names = {c["name"].strip().lower() for c in chars}
    alias_map = dict.fromkeys(names)
    for c in chars:
        for a in c.get("aliases") or []:
            alias_map[str(a).strip().lower()] = None
    locs = {str(l.get("name") or "").strip().lower() for l in arch.get("locations") or [] if isinstance(l, dict)}

    def known(name: Any) -> bool:
        return str(name or "").strip().lower() in alias_map

    if not chars:
        problems.append({"code": "no_characters", "message": "No characters"})
    roles = [str(c.get("role") or "").lower() for c in chars]
    if not any("protagonist" in r for r in roles):
        problems.append({"code": "no_protagonist", "message": "No character has the Protagonist role"})
    if not any("antagonist" in r for r in roles):
        problems.append({"code": "no_antagonist", "message": "No character has the Antagonist role"})
    if not locs:
        problems.append({"code": "no_locations", "message": "No locations"})
    for c in chars:
        phases = {str(p.get("phase")) for p in c.get("arc") or [] if isinstance(p, dict)}
        if "protagonist" in str(c.get("role") or "").lower() and not {"start", "mid", "end"} <= phases:
            problems.append({"code": "arc_incomplete", "message": f"Protagonist '{c['name']}' lacks start/mid/end arc phases", "subject": c["name"]})
        if c.get("home_location") and str(c["home_location"]).strip().lower() not in locs:
            problems.append({"code": "unknown_location", "message": f"Character '{c['name']}' home location '{c['home_location']}' is not a listed location", "subject": c["name"]})
        intro = int(c.get("introduction_chapter") or 1)
        if intro < 1 or intro > chapter_count:
            problems.append({"code": "chapter_out_of_range", "message": f"Character '{c['name']}' introduction chapter {intro} outside 1..{chapter_count}", "subject": c["name"]})
    for r in arch.get("relationships") or []:
        for key in ("character_a", "character_b"):
            if not known(r.get(key)):
                problems.append({"code": "unknown_character", "message": f"Relationship references unknown character '{r.get(key)}'", "subject": str(r.get(key))})
    for k in arch.get("knowledge_facts") or []:
        for who in k.get("knowers_at_start") or []:
            if not known(who):
                problems.append({"code": "unknown_character", "message": f"Knowledge fact '{str(k.get('fact'))[:60]}' lists unknown knower '{who}'", "subject": str(who)})
        rc = int(k.get("reader_reveal_chapter") or 0)
        if rc > chapter_count:
            problems.append({"code": "chapter_out_of_range", "message": f"Fact '{str(k.get('fact'))[:60]}' reveal chapter {rc} exceeds {chapter_count}", "subject": str(k.get("fact"))[:60]})
    setups = arch.get("setups_payoffs") or []
    if not setups:
        problems.append({"code": "no_setups", "message": "No setup/payoff pairs planned"})
    for sp in setups:
        s, p = int(sp.get("setup_chapter") or 0), int(sp.get("payoff_chapter") or 0)
        if not p:
            problems.append({"code": "payoff_missing", "message": f"Setup '{str(sp.get('setup'))[:60]}' has no payoff chapter", "subject": str(sp.get("setup"))[:60]})
        elif p <= s:
            problems.append({"code": "payoff_before_setup", "message": f"Payoff of '{str(sp.get('setup'))[:60]}' (ch {p}) is not after its setup (ch {s})", "subject": str(sp.get("setup"))[:60]})
        if s < 1 or p > chapter_count:
            problems.append({"code": "chapter_out_of_range", "message": f"Setup/payoff '{str(sp.get('setup'))[:60]}' uses chapters outside 1..{chapter_count}", "subject": str(sp.get("setup"))[:60]})
    threads = arch.get("plot_threads") or []
    if not any(str(t.get("thread_type")) == "main_plot" for t in threads):
        problems.append({"code": "no_main_plot", "message": "No plot thread of type main_plot"})
    for t in threads:
        res = int(t.get("resolution_chapter") or 0)
        if str(t.get("thread_type")) == "main_plot" and res and res < max(1, chapter_count - max(1, chapter_count // 5)):
            problems.append({"code": "main_plot_resolves_early", "message": f"Main plot resolves at chapter {res} of {chapter_count}", "subject": str(t.get("name"))})
        if res > chapter_count:
            problems.append({"code": "chapter_out_of_range", "message": f"Thread '{t.get('name')}' resolves after chapter {chapter_count}", "subject": str(t.get("name"))})
        for who in t.get("participants") or []:
            if not known(who):
                problems.append({"code": "unknown_character", "message": f"Thread '{t.get('name')}' lists unknown participant '{who}'", "subject": str(who)})
    contract = arch.get("contract") or {}
    if not contract.get("ending_contract"):
        problems.append({"code": "no_ending_contract", "message": "Story contract has no ending contract"})
    if not contract.get("pov"):
        problems.append({"code": "no_pov", "message": "Story contract has no POV system"})
    # Density: enough material for the chapter count.
    material = len(setups) + len(threads) * 3 + len(chars)
    if chapter_count >= 12 and material < chapter_count * 0.6:
        problems.append({"code": "density_low", "message": f"Planned material ({material} units) is thin for {chapter_count} chapters"})
    return problems


def firewall_architecture(arch: Dict[str, Any], profile: Optional[fw.SourceProfile]) -> List[Dict[str, Any]]:
    if profile is None:
        return []
    text = json.dumps(arch, ensure_ascii=False)
    names = {c.get("name", ""): "character" for c in arch.get("characters") or [] if isinstance(c, dict)}
    rep = fw.check_text(text, profile, character_roles=names, locations=[l.get("name", "") for l in arch.get("locations") or [] if isinstance(l, dict)], objects=[i.get("name", "") for i in arch.get("items") or [] if isinstance(i, dict)])
    return [{"code": f"source_leak:{f.check}", "message": f.detail, "subject": f.matched} for f in rep.findings if f.severity in ("critical", "high")]


# ------------------------------------------------------------------ prompt

def build_prompt(storyline: Dict[str, Any], *, chapter_count: int, allocation: List[Dict[str, Any]], fit: Dict[str, Any], brief: str, preferences: Dict[str, Any], problems: Sequence[Dict[str, Any]] = (), previous: Optional[Dict[str, Any]] = None) -> str:
    parts = ["[SELECTED STORYLINE]", json.dumps({k: v for k, v in storyline.items() if k != "schema_version"}, ensure_ascii=False, indent=1)]
    parts += ["\n[CHAPTER COUNT — HARD CONSTRAINT]", f"The novel has exactly {chapter_count} chapters. Allocation of dramatic functions to chapter ranges:"]
    parts += [f"- {a['function']}: chapters {a['chapter_start']}-{a['chapter_end']}" for a in allocation]
    if fit["adaptations"]:
        parts.append("Adapt the storyline to this count by: " + "; ".join(fit["adaptations"]) + ".")
    prefs = {k: v for k, v in preferences.items() if v not in (None, "", [], {})}
    if prefs:
        parts += ["\n[USER PREFERENCES]"] + [f"- {k}: {v}" for k, v in prefs.items()]
    parts += ["\n[REFERENCE STRUCTURE — abstract, entity-free]", brief]
    parts += ["\n[REQUIREMENTS]", "characters: 5-10 with full fields; the protagonist arc must have start/mid/end with chapter hints. locations: 4-8. knowledge_facts: 4-10 including every secret the plot depends on. relationships: every pair that matters. plot_threads: one main_plot plus 2-5 subplots with opening and resolution chapters. setups_payoffs: 6-15 with setup_chapter < payoff_chapter <= chapter count. timeline: 8-20 events. act_plan: one line per allocated function."]
    if problems and previous is not None:
        parts += ["\n[PREVIOUS ARCHITECTURE FAILED VALIDATION — fix ONLY these problems, keep everything else]"] + [f"- {p['code']}: {p['message']}" for p in problems[:30]]
        parts += ["\n[PREVIOUS ARCHITECTURE]", json.dumps(previous, ensure_ascii=False)[:60000]]
    return "\n".join(parts)


# --------------------------------------------------------------- card build

def _upsert(session: Session, project_id: int, type_name: str, title: str, content: Dict[str, Any]) -> Card:
    ct = _type(session, type_name)
    card = session.exec(select(Card).where(Card.project_id == project_id, Card.card_type_id == ct.id, Card.title == title[:200])).first()
    if card is None:
        card = CardService(session).create(CardCreate(title=title[:200], content=content, card_type_id=ct.id), project_id, commit=False)
    else:
        card.content = content
        flag_modified(card, "content")
        session.add(card)
    session.flush()
    return card


def write_bible_cards(session: Session, project_id: int, arch: Dict[str, Any], *, chapter_count: int, storyline: Dict[str, Any]) -> Dict[str, int]:
    """Write Story Foundation, Reader Contract, Theme Map, entity cards and ledgers from the architecture."""
    contract = arch.get("contract") or {}
    counts: Dict[str, int] = {}

    def bump(k: str) -> None:
        counts[k] = counts.get(k, 0) + 1

    _upsert(session, project_id, "Story Foundation", "Story Foundation", {
        "core_premise": contract.get("premise") or storyline.get("premise") or "", "story_promise": contract.get("genre_promise") or "", "reader_fantasy": contract.get("primary_fantasy") or "",
        "central_dramatic_question": storyline.get("central_mystery") or storyline.get("thematic_question") or "", "protagonist": storyline.get("protagonist") or "", "protagonist_goal": storyline.get("central_desire") or "",
        "main_opposition": storyline.get("antagonist") or "", "external_conflict": storyline.get("central_conflict") or "", "internal_conflict": storyline.get("internal_flaw") or "", "stakes": storyline.get("stakes") or "",
        "unique_mechanism": storyline.get("central_conflict") or "", "emotional_core": storyline.get("tone") or "", "thematic_argument": contract.get("thematic_question") or storyline.get("thematic_question") or "",
        "expected_ending_experience": contract.get("ending_contract") or storyline.get("ending") or "", "truth_status": "canon", "confidence": 1.0, "schema_version": AUTONOMOUS_SCHEMA_VERSION,
    })
    bump("Story Foundation")
    _upsert(session, project_id, "Reader Contract", "Reader Contract", {
        "primary_fantasy": contract.get("primary_fantasy") or "", "primary_emotional_reward": contract.get("primary_emotional_reward") or "", "expected_tone": contract.get("tone") or storyline.get("tone") or "",
        "expected_protagonist_behavior": contract.get("expected_protagonist_behavior") or [], "violations": (contract.get("violations") or []) + (contract.get("prohibited_deviations") or []),
        "pov": contract.get("pov") or storyline.get("pov_plan") or "", "tense": contract.get("tense") or "past", "target_audience": contract.get("target_audience") or "", "ending_contract": contract.get("ending_contract") or "",
        "truth_status": "canon", "confidence": 1.0,
    })
    bump("Reader Contract")
    _upsert(session, project_id, "Theme Map", "Theme Map", {"theme_question": contract.get("thematic_question") or storyline.get("thematic_question") or "", "protagonist_initial_belief": storyline.get("internal_flaw") or "", "planned_movement": storyline.get("ending") or "", "truth_status": "canon", "confidence": 1.0})
    bump("Theme Map")

    role_map = {"protagonist": "Protagonist", "deuteragonist": "Supporting Character", "antagonist": "Antagonist", "supporting character": "Supporting Character", "minor": "NPC"}
    for c in arch.get("characters") or []:
        role = role_map.get(str(c.get("role") or "").strip().lower(), "Supporting Character")
        arc = c.get("arc") or []
        arc_text = " -> ".join(f"{p.get('phase')}: {p.get('state')}" for p in arc if isinstance(p, dict))
        _upsert(session, project_id, "Character Card", c["name"], {
            "name": c["name"], "entity_type": "character", "life_span": "Long Term", "role_type": role, "born_scene": c.get("home_location") or "", "description": c.get("identity") or "",
            "personality": "; ".join(c.get("voice_tells") or []) or c.get("flaw") or "", "core_drive": c.get("goal") or "", "character_arc": arc_text, "aliases": c.get("aliases") or [],
            "dramatic_design": {"external_goal": c.get("goal") or "", "internal_need": c.get("motivation") or "", "wound": c.get("wound") or "", "fear": c.get("fear") or "", "flaw": c.get("flaw") or "", "secret": c.get("secret") or ""},
            "voice": {"sentence_tendency": c.get("voice_sentence_tendency") or "", "verbal_tells": c.get("voice_tells") or [], "forbidden_speech": c.get("forbidden_speech") or [], "forms_of_address": []},
            "competence": {"strengths": c.get("capabilities") or [], "limits_and_costs": c.get("limitations") or []},
            "consistency_rules": {"knowledge_restrictions": c.get("knowledge_boundaries") or []},
            "arc_milestones": [{"stage": {"start": "setup", "mid": "midpoint", "end": "resolution"}.get(str(p.get("phase")), "setup"), "description": p.get("state") or "", "chapter_hint": str(p.get("chapter_hint") or ""), "status": "planned"} for p in arc if isinstance(p, dict)],
            "dynamic_info": {"Possessions": [{"id": i + 1, "info": str(x), "weight": 1.0} for i, x in enumerate(c.get("possessions") or [])]} if c.get("possessions") else {},
            "appearance": c.get("appearance") or "", "introduction_chapter": c.get("introduction_chapter") or 1, "exit_chapter": c.get("exit_chapter") or 0, "truth_status": "canon", "confidence": 1.0,
        })
        bump("Character Card")
    for l in arch.get("locations") or []:
        _upsert(session, project_id, "Scene Card", l["name"], {"name": l["name"], "entity_type": "scene", "life_span": "Long Term", "description": l.get("description") or "", "function_in_story": l.get("function_in_story") or "", "dynamic_state": []})
        bump("Scene Card")
    for f in arch.get("factions") or []:
        _upsert(session, project_id, "Organization Card", f["name"], {"name": f["name"], "entity_type": "organization", "life_span": "Long Term", "description": f.get("description") or "", "influence": f.get("goal") or "", "relationship": [], "dynamic_state": []})
        bump("Organization Card")
    for it in arch.get("items") or []:
        _upsert(session, project_id, "Item Card", it["name"], {"name": it["name"], "entity_type": "item", "life_span": "Long Term", "category": "other", "description": it.get("description") or "", "owner_hint": it.get("owner") or None, "power_or_effect": it.get("significance") or None})
        bump("Item Card")
    for w in arch.get("world_rules") or []:
        _upsert(session, project_id, "World Rule", str(w.get("rule"))[:200], {"rule": w.get("rule"), "domain": w.get("domain") if w.get("domain") in ("geography", "politics", "social_hierarchy", "economy", "technology", "magic_power", "religion", "law", "history", "culture", "other") else "other", "costs": w.get("cost") or "", "explanation": w.get("limits") or "", "known_by": w.get("known_by") or [], "truth_status": "canon", "confidence": 1.0})
        bump("World Rule")
    for k in arch.get("knowledge_facts") or []:
        rc = int(k.get("reader_reveal_chapter") or 0)
        _upsert(session, project_id, "Knowledge Fact", str(k.get("fact"))[:200], {
            "fact": k.get("fact"), "reader_state": "knows" if rc == 0 and not k.get("knowers_at_start") else "unaware", "planned_reveal_chapter": rc or None, "sensitivity": k.get("sensitivity") if k.get("sensitivity") in ("low", "medium", "high") else "medium",
            "knowers": [{"entity": who, "state": "knows", "learned_chapter": 0} for who in (k.get("knowers_at_start") or [])], "truth_status": "canon" if k.get("is_true", True) else "disputed", "confidence": 1.0,
        })
        bump("Knowledge Fact")
    for r in arch.get("relationships") or []:
        a, b = str(r.get("character_a")), str(r.get("character_b"))
        _upsert(session, project_id, "Relationship Arc", f"{a} ↔ {b}", {
            "character_a": a, "character_b": b, "trust": int(r.get("trust") or 50), "affection": int(r.get("affection") or 50), "fear": 0, "dependency": 0, "resentment": int(r.get("hostility") or 0),
            "power_balance": r.get("power_balance") or "", "private_relationship": r.get("private_relationship") or "", "unresolved_tension": r.get("unresolved_tension") or "", "planned_arc": r.get("arc_summary") or "", "truth_status": "canon", "confidence": 1.0,
        })
        bump("Relationship Arc")
    for t in arch.get("plot_threads") or []:
        ttype = t.get("thread_type") if t.get("thread_type") in ("main_plot", "character_arc", "relationship_arc", "mystery", "romance", "political_conflict", "faction_conflict", "training_progression", "survival", "comic_subplot", "thematic", "subplot") else "subplot"
        _upsert(session, project_id, "Plot Thread", str(t.get("name"))[:200], {"name": t.get("name"), "thread_type": ttype, "central_question": t.get("central_question") or "", "participants": t.get("participants") or [], "opening_chapter": int(t.get("opening_chapter") or 1), "last_advanced_chapter": 0, "next_expected_chapter": int(t.get("opening_chapter") or 1), "planned_resolution": f"chapter {t.get('resolution_chapter')}" if t.get("resolution_chapter") else "", "status": "active" if int(t.get("opening_chapter") or 1) <= 1 else "planned", "urgency": "high" if ttype == "main_plot" else "medium", "milestones": [], "truth_status": "canon", "confidence": 1.0})
        bump("Plot Thread")
    for sp in arch.get("setups_payoffs") or []:
        ptype = sp.get("promise_type") if sp.get("promise_type") in ("promise", "foreshadowing", "clue", "chekhovs_gun", "question", "secret", "prophecy", "threat", "deal", "debt", "vow", "unresolved_emotion", "mystery") else "foreshadowing"
        pc = int(sp.get("payoff_chapter") or 0)
        _upsert(session, project_id, "Promise Payoff", str(sp.get("setup"))[:200], {"setup": sp.get("setup"), "promise_type": ptype, "source_chapter": int(sp.get("setup_chapter") or 1), "target_payoff_range": [pc, pc] if pc else [], "planned_payoff": sp.get("payoff") or "", "status": "planned", "participants": [], "truth_status": "planned", "confidence": 1.0})
        bump("Promise Payoff")
    for i, ev in enumerate(arch.get("timeline") or []):
        _upsert(session, project_id, "Timeline Event", str(ev.get("title"))[:200], {"title": ev.get("title"), "story_time": ev.get("story_time") or "", "order_index": i, "chapter_number": int(ev.get("chapter") or 0) or None, "location": ev.get("location") or "", "participants": ev.get("participants") or [], "cause": ev.get("summary") or "", "truth_status": "planned", "confidence": 0.9})
        bump("Timeline Event")
    _upsert(session, project_id, ARCHITECTURE_CARD_TYPE, ARCHITECTURE_TITLE, {**arch, "chapter_count": chapter_count, "schema_version": AUTONOMOUS_SCHEMA_VERSION})
    session.flush()
    return counts


def stored_architecture(session: Session, project_id: int) -> Dict[str, Any]:
    return _c(BibleService(session).find_card(project_id, ARCHITECTURE_CARD_TYPE, ARCHITECTURE_TITLE))


# ------------------------------------------------------------------- stages

async def stage_novel_architecture(session: Session, *, original_project_id: int, source_project_id: int, storyline: Dict[str, Any], chapter_count: int, client: ModelClient, brief: str, preferences: Dict[str, Any], profile: Optional[fw.SourceProfile]) -> Dict[str, Any]:
    """Generate, validate and (if needed) repair the architecture; store it on the original project."""
    existing = stored_architecture(session, original_project_id)
    if existing and int(existing.get("chapter_count") or 0) == chapter_count and not validate_architecture(existing, chapter_count=chapter_count):
        return {"reused": True, "problems": [], "rounds": 0, "allocation": allocate_chapters(chapter_count, source_proportions=source_proportions(session, source_project_id))}
    allocation = allocate_chapters(chapter_count, source_proportions=source_proportions(session, source_project_id))
    fit = fit_report(storyline, chapter_count)
    problems: List[Dict[str, Any]] = []
    previous: Optional[Dict[str, Any]] = None
    arch: Optional[Dict[str, Any]] = None
    for round_no in range(1, MAX_ARCHITECT_ROUNDS + 1):
        result = await client.structured(role="novel_architect", schema=NovelArchitecture, system_prompt=SYSTEM_PROMPT, user_prompt=build_prompt(storyline, chapter_count=chapter_count, allocation=allocation, fit=fit, brief=brief, preferences=preferences, problems=problems, previous=previous), prompt_version=ARCHITECTURE_PROMPT_VERSION, stage="NOVEL_ARCHITECTURE")
        arch = result.model_dump(mode="json")
        problems = validate_architecture(arch, chapter_count=chapter_count) + firewall_architecture(arch, profile)
        if not problems:
            break
        previous = arch
    if arch is None or problems:
        raise fail.StageFailure(fail.PLANNING_IMPOSSIBILITY, f"Architecture still invalid after {MAX_ARCHITECT_ROUNDS} rounds: {len(problems)} problem(s)", detail={"problems": problems[:30]})
    arch["allocation"] = allocation
    arch["fit"] = fit
    _upsert(session, original_project_id, ARCHITECTURE_CARD_TYPE, ARCHITECTURE_TITLE, {**arch, "chapter_count": chapter_count, "schema_version": AUTONOMOUS_SCHEMA_VERSION})
    session.commit()
    return {"reused": False, "problems": [], "rounds": round_no, "allocation": allocation, "fit": fit, "characters": len(arch.get("characters") or []), "setups": len(arch.get("setups_payoffs") or [])}


def stage_bible_build(session: Session, *, original_project_id: int, chapter_count: int, storyline: Dict[str, Any]) -> Dict[str, Any]:
    """Write Bible cards from the stored architecture, verify isolation, seed canon."""
    arch = stored_architecture(session, original_project_id)
    if not arch:
        raise fail.StageFailure(fail.STALE_DEPENDENCY, "No stored architecture to build the Bible from")
    counts = write_bible_cards(session, original_project_id, arch, chapter_count=chapter_count, storyline=storyline)
    session.commit()
    iso = transfer.isolation_report(session, original_project_id)
    if not iso["isolated"]:
        raise fail.StageFailure(fail.ORIGINALITY_VIOLATION, "Original Bible is contaminated with source content", detail={"problems": iso["problems"][:20]})
    bible = BibleService(session)
    manifest = provenance.get_manifest(session, original_project_id, create=True)
    # Re-seeding must be idempotent: drop previous Bible-sourced chapter-0 facts first.
    for row in session.exec(select(CanonFact).where(CanonFact.project_id == original_project_id, CanonFact.source == "bible")).all():
        session.delete(row)
    session.flush()
    cards = []
    for t in ("Character Card", "Relationship Arc", "Knowledge Fact", "Item Card"):
        cards += bible.cards_of_type(original_project_id, t)
    seeded = canon_store.seed_from_bible(session, original_project_id, cards, canon_revision=manifest.canon_revision)
    session.commit()
    return {"cards": counts, "facts_seeded": seeded, "isolation": {"isolated": True}}


__all__ = [
    "ACT_TEMPLATE", "ARCHITECTURE_CARD_TYPE", "ARCHITECTURE_PROMPT_VERSION", "ARCHITECTURE_TITLE", "allocate_chapters", "build_prompt", "firewall_architecture", "fit_report",
    "source_proportions", "stage_bible_build", "stage_novel_architecture", "stored_architecture", "validate_architecture", "write_bible_cards",
]
