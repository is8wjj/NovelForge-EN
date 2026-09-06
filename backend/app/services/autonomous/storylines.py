"""STORYLINE_GENERATION: 5-10 detailed original options related to the source only
through structure (genre, pacing, beat architecture, reveal cadence, role
functions, themes), never through names, settings, objects, twists or scenes.

Gates (all deterministic, model-independent):
- originality: every option is run through the Originality Firewall against
  the source profile; critical/high findings reject the option;
- diversity: pairwise similarity over the discriminating fields (setting,
  protagonist role, core conflict, antagonist mechanism, relationship
  configuration, mystery, climax mechanism, ending type); too-similar pairs
  reject the later option;
- count: at least ``MIN_OPTIONS`` surviving options or the stage fails with
  ``PLANNING_IMPOSSIBILITY`` (the runner then re-ideates with the rejects as
  explicit anti-examples).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlmodel import Session, select

from app.db.models import StorylineCandidate
from app.schemas.autonomous import AUTONOMOUS_SCHEMA_VERSION, StorylineOptionSet
from app.services.autonomous import failures as fail
from app.services.autonomous.model_client import ModelClient
from app.services.bible.bible_service import BibleService
from app.services.forge import firewall as fw
from app.services.forge.fingerprint import compact_fingerprint
from app.services.forge.textmetrics import tokenize

STORYLINE_PROMPT_VERSION = "autonomous-storylines-1"
MIN_OPTIONS = 5
TARGET_OPTIONS = 7
MAX_PAIRWISE_SIMILARITY = 0.45
DIVERSITY_FIELDS = ("setting", "protagonist", "central_conflict", "antagonist", "relationship_arc", "central_mystery", "climax", "ending_type")

SYSTEM_PROMPT = (
    "You are a storyline ideator for an original novel. You receive an abstract Narrative Fingerprint and entity-free mechanisms learned from a reference novel. "
    "Your options must relate to the reference ONLY through genre, audience appeal, emotional experience, pacing, beat architecture, tension/reveal pattern, "
    "character-role functions, high-level themes and structural mechanisms. They must NOT reuse the reference's names, setting identifiers, distinctive objects, "
    "scene sequence, unique twists, specific relationships, proprietary terminology, memorable phrases or close plot correspondence. "
    "Every option must differ from every other option in setting, protagonist occupation/role, core conflict, antagonist mechanism, relationship configuration, "
    "central mystery, climax mechanism and ending type. Output must validate against the schema."
)


def _c(card) -> Dict[str, Any]:
    return card.content if card is not None and isinstance(card.content, dict) else {}


def source_profile(session: Session, source_project_id: int) -> Optional[fw.SourceProfile]:
    """Firewall profile of the *source* project (the forge helper expects an original project id)."""
    from app.services.forge.corpus import load_source_chapters

    chapters = load_source_chapters(session, source_project_id)
    if not chapters:
        return None
    bible = BibleService(session)
    names: List[str] = []
    roles: Dict[str, str] = {}
    locations: List[str] = []
    objects: List[str] = []
    for card in bible.cards_of_type(source_project_id, "Character Card"):
        c = _c(card)
        n = str(c.get("name") or card.title)
        names.append(n)
        roles[n] = str(c.get("role_type") or "")
        names += [str(a) for a in (c.get("aliases") or [])]
    for card in bible.cards_of_type(source_project_id, "Organization Card"):
        names.append(str(_c(card).get("name") or card.title))
    for card in bible.cards_of_type(source_project_id, "Scene Card"):
        locations.append(str(_c(card).get("name") or card.title))
    for card in bible.cards_of_type(source_project_id, "Item Card"):
        objects.append(str(_c(card).get("name") or card.title))
    summaries: List[str] = []
    beats: List[str] = []
    for ch in chapters:
        for s in (ch.analysis or {}).get("scenes") or []:
            if isinstance(s, dict):
                if s.get("summary") or s.get("goal"):
                    summaries.append(str(s.get("summary") or s.get("goal")))
                if s.get("function"):
                    beats.append(str(s["function"]))
    return fw.SourceProfile.from_chapters(chapters, manuscript_id=chapters[0].manuscript_id, entity_names=names + locations + objects, scene_summaries=summaries, beat_sequence=beats, character_roles=roles, locations=locations, objects=objects)


def source_brief(session: Session, source_project_id: int, *, preferences: Dict[str, Any]) -> str:
    """Abstract, entity-free description of the source that the ideator may see."""
    bible = BibleService(session)
    fp = _c(bible.singleton(source_project_id, "Narrative Fingerprint"))
    genome = _c(bible.singleton(source_project_id, "Narrative Genome"))
    structure = _c(bible.find_card(source_project_id, "Story Structure Map", "Story Structure Map"))
    lines = ["[NARRATIVE FINGERPRINT — measurable targets]", compact_fingerprint(fp, max_chars=2600) if fp else "(none)"]
    if genome.get("patterns"):
        lines.append("\n[ABSTRACT MECHANISMS — technique only]")
        for p in genome["patterns"][:12]:
            if isinstance(p, dict):
                lines.append(f"- {p.get('dimension')}: {str(p.get('transferable_abstraction') or p.get('description') or '')[:300]}")
    stages = structure.get("stages") or []
    if stages:
        total = max(int(s.get("chapter_end") or 0) for s in stages) or 1
        lines.append("\n[STRUCTURAL PROPORTIONS]")
        lines.append(", ".join(f"stage {s.get('stage_number')}: {round(100 * (int(s.get('chapter_end') or 0) - int(s.get('chapter_start') or 1) + 1) / total)}%" for s in stages[:16]))
        lines.append(f"Source stage count: {len(stages)}; chapters: {total}")
    prefs = {k: v for k, v in preferences.items() if v not in (None, "", [], {})}
    if prefs:
        lines.append("\n[USER PREFERENCES]")
        lines += [f"- {k}: {v}" for k, v in prefs.items()]
    return "\n".join(lines)


def build_prompt(brief: str, *, count: int, rejected: Sequence[Dict[str, Any]] = ()) -> str:
    parts = [brief, f"\n[TASK]\nGenerate exactly {count} substantially different, detailed original storyline options. For each option fill every field of the schema in depth: premise 120-250 words; 4-6 act entries covering setup, first turn, midpoint, crisis, climax and resolution; a main cast of 4-7 original names; chapter_suitability_min/max as a realistic range."]
    if rejected:
        parts.append("\n[REJECTED IN A PREVIOUS ROUND — do not produce anything resembling these]")
        for r in rejected[:10]:
            parts.append(f"- {r.get('title')}: {str(r.get('reason') or '')[:200]}")
    return "\n".join(parts)


def option_text(opt: Dict[str, Any]) -> str:
    return " ".join(str(opt.get(k) or "") for k in ("title", "hook", "premise", "protagonist", "antagonist", "setting", "central_conflict", "midpoint", "crisis", "climax", "ending", "major_subplot", "relationship_arc", "central_mystery")) + " " + " ".join(opt.get("main_cast") or []) + " " + " ".join(a.get("summary", "") for a in opt.get("acts") or [] if isinstance(a, dict))


def originality_check(opt: Dict[str, Any], profile: Optional[fw.SourceProfile]) -> Dict[str, Any]:
    if profile is None:
        return {"passed": True, "skipped": "no source profile", "findings": [], "score": 1.0}
    names = [str(n).split(":")[0].split("(")[0].strip() for n in (opt.get("main_cast") or [])]
    roles = {n: "character" for n in names if n}
    rep = fw.check_text(option_text(opt), profile, character_roles=roles, scene_summaries=[a.get("summary", "") for a in opt.get("acts") or [] if isinstance(a, dict)], max_summary_similarity=0.5)
    critical = [f.as_dict() for f in rep.findings if f.severity in ("critical", "high")]
    score = max(0.0, 1.0 - 0.25 * len(critical) - 0.05 * len([f for f in rep.findings if f.severity not in ("critical", "high")]))
    return {"passed": not critical, "findings": [f.as_dict() for f in rep.findings][:30], "scores": rep.scores, "score": round(score, 3)}


def _tokens(text: str) -> set:
    return {t for t in tokenize(str(text or "").lower(), "en") if len(t) > 3}


def pairwise_similarity(a: Dict[str, Any], b: Dict[str, Any]) -> float:
    """Mean Jaccard similarity over the discriminating fields."""
    sims = []
    for f in DIVERSITY_FIELDS:
        ta, tb = _tokens(a.get(f)), _tokens(b.get(f))
        if not ta and not tb:
            continue
        sims.append(len(ta & tb) / float(len(ta | tb) or 1))
    if str(a.get("ending_type") or "").strip().lower() and str(a.get("ending_type") or "").strip().lower() == str(b.get("ending_type") or "").strip().lower():
        sims.append(1.0)
    return round(sum(sims) / len(sims), 3) if sims else 0.0


def similarity_matrix(options: Sequence[Dict[str, Any]]) -> List[List[float]]:
    n = len(options)
    m = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            m[i][j] = m[j][i] = pairwise_similarity(options[i], options[j])
    return m


def gate_options(options: Sequence[Dict[str, Any]], profile: Optional[fw.SourceProfile], *, max_similarity: float = MAX_PAIRWISE_SIMILARITY) -> Tuple[List[Dict[str, Any]], List[List[float]]]:
    """Attach originality and diversity verdicts to each option (in place copy)."""
    out = [dict(o) for o in options]
    matrix = similarity_matrix(out)
    accepted_idx: List[int] = []
    for i, opt in enumerate(out):
        orig = originality_check(opt, profile)
        opt["_originality"] = orig
        reason = None
        if not orig["passed"]:
            reason = "source overlap: " + "; ".join(f["detail"] for f in orig["findings"] if f["severity"] in ("critical", "high"))[:400]
        else:
            for j in accepted_idx:
                if matrix[i][j] > max_similarity:
                    reason = f"too similar to option {j + 1} ({out[j].get('title')}), similarity {matrix[i][j]}"
                    break
        if not (opt.get("premise") or "").strip() or len(opt.get("acts") or []) < 3:
            reason = reason or "underspecified option (missing premise or act progression)"
        opt["_rejected"] = reason is not None
        opt["_rejection_reason"] = reason
        if reason is None:
            accepted_idx.append(i)
    return out, matrix


def recommended_range(opt: Dict[str, Any], source_chapters: int) -> Tuple[int, int]:
    lo = int(opt.get("chapter_suitability_min") or 0) or max(8, round(source_chapters * 0.5))
    hi = int(opt.get("chapter_suitability_max") or 0) or max(lo + 4, round(source_chapters * 1.5))
    if hi < lo:
        lo, hi = hi, lo
    return max(3, lo), max(lo + 1, hi)


def persist_candidates(session: Session, *, job_id: int, source_project_id: int, options: Sequence[Dict[str, Any]], matrix: List[List[float]], source_chapters: int, replace: bool = True) -> List[StorylineCandidate]:
    if replace:
        for row in session.exec(select(StorylineCandidate).where(StorylineCandidate.job_id == job_id)).all():
            session.delete(row)
        session.flush()
    rows: List[StorylineCandidate] = []
    for i, opt in enumerate(options):
        clean = {k: v for k, v in opt.items() if not k.startswith("_")}
        lo, hi = recommended_range(clean, source_chapters)
        row = StorylineCandidate(
            job_id=job_id, source_project_id=source_project_id, option_index=i, title=str(clean.get("title") or f"Option {i + 1}")[:200], content={**clean, "schema_version": AUTONOMOUS_SCHEMA_VERSION},
            originality_score=float(opt.get("_originality", {}).get("score") or 0.0), originality_report=opt.get("_originality") or {},
            similarity_to_others={str(j + 1): matrix[i][j] for j in range(len(options)) if j != i}, recommended_chapters_min=lo, recommended_chapters_max=hi,
            rejected=bool(opt.get("_rejected")), rejection_reason=opt.get("_rejection_reason"),
        )
        session.add(row)
        rows.append(row)
    session.flush()
    return rows


async def stage_storyline_generation(session: Session, *, job_id: int, source_project_id: int, client: ModelClient, preferences: Dict[str, Any], count: int = TARGET_OPTIONS, max_rounds: int = 2) -> Dict[str, Any]:
    from app.services.forge.corpus import load_source_chapters

    profile = source_profile(session, source_project_id)
    brief = source_brief(session, source_project_id, preferences=preferences)
    source_chapters = len(load_source_chapters(session, source_project_id))
    rejected_history: List[Dict[str, Any]] = []
    accepted: List[Dict[str, Any]] = []
    all_gated: List[Dict[str, Any]] = []
    rounds = 0
    while rounds < max_rounds and len(accepted) < MIN_OPTIONS:
        rounds += 1
        need = max(count - len(accepted), MIN_OPTIONS)
        result = await client.structured(role="storyline_ideator", schema=StorylineOptionSet, system_prompt=SYSTEM_PROMPT, user_prompt=build_prompt(brief, count=need, rejected=rejected_history), prompt_version=STORYLINE_PROMPT_VERSION, stage="STORYLINE_GENERATION")
        fresh = [o.model_dump(mode="json") for o in result.options]
        gated, _ = gate_options(accepted + fresh, profile)
        accepted = [o for o in gated if not o["_rejected"]]
        known = {r["title"] for r in rejected_history}
        rejected_history += [{"title": o.get("title"), "reason": o.get("_rejection_reason")} for o in gated if o["_rejected"] and o.get("title") not in known]
        all_gated = gated
    final, matrix = gate_options(all_gated, profile)
    survivors = [o for o in final if not o["_rejected"]]
    rows = persist_candidates(session, job_id=job_id, source_project_id=source_project_id, options=final, matrix=matrix, source_chapters=source_chapters)
    session.commit()
    if len(survivors) < MIN_OPTIONS:
        raise fail.StageFailure(fail.PLANNING_IMPOSSIBILITY, f"Only {len(survivors)} storyline options passed originality/diversity gates after {rounds} round(s); need {MIN_OPTIONS}", detail={"rejected": rejected_history[:20]})
    return {"options": len(final), "accepted": len(survivors), "rejected": len(final) - len(survivors), "rounds": rounds, "candidate_ids": [r.id for r in rows], "similarity_matrix": matrix}


def candidate_dict(row: StorylineCandidate) -> Dict[str, Any]:
    return {
        "id": row.id, "job_id": row.job_id, "option_index": row.option_index, "title": row.title, "content": row.content, "originality_score": row.originality_score,
        "originality_report": {k: v for k, v in (row.originality_report or {}).items() if k != "findings"} | {"findings": (row.originality_report or {}).get("findings", [])[:10]},
        "similarity_to_others": row.similarity_to_others, "recommended_chapters_min": row.recommended_chapters_min, "recommended_chapters_max": row.recommended_chapters_max,
        "rejected": row.rejected, "rejection_reason": row.rejection_reason, "selected": row.selected,
    }


__all__ = [
    "DIVERSITY_FIELDS", "MAX_PAIRWISE_SIMILARITY", "MIN_OPTIONS", "STORYLINE_PROMPT_VERSION", "TARGET_OPTIONS", "build_prompt", "candidate_dict", "gate_options",
    "originality_check", "pairwise_similarity", "persist_candidates", "recommended_range", "similarity_matrix", "source_brief", "source_profile", "stage_storyline_generation",
]
