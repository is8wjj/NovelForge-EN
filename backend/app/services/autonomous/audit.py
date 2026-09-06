"""WHOLE_NOVEL_AUDIT + GLOBAL_REPAIR.

The audit is deterministic and runs over the whole manuscript, catching what
chapter-local validation cannot: unresolved setups, disappearing subplots,
tension plateaus, repeated chapter openings/endings, POV/tense drift across
chapters, aggregate source similarity, unused major characters.

Global repair is targeted and dependency-aware: only chapters with blocking
findings are rewritten (through the repair editor with the chapter's original
compiled constraints), then re-validated with the Forge validators, and canon
is rebuilt from the earliest changed chapter forward by re-running the sync
for each affected chapter in order.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import Session, select

from app.db.models import Card, ChapterPipelineRun
from app.services.autonomous import failures as fail
from app.services.autonomous.model_client import ModelClient
from app.services.bible.bible_service import BibleService
from app.services.forge import claims as claims_mod
from app.services.forge import firewall as fw
from app.services.forge import provenance
from app.services.forge import validators as v
from app.services.forge.compiler import ChapterContextCompiler
from app.services.forge.pipeline import REPAIR_SYSTEM_PROMPT, build_repair_prompt, source_profile_for, validate_draft
from app.services.forge.textmetrics import measure, split_paragraphs, split_sentences, tokenize

AUDIT_VERSION = "whole-novel-audit-1"
GLOBAL_REPAIR_PROMPT_VERSION = "autonomous-global-repair-1"
MAX_GLOBAL_REPAIR_CHAPTERS = 8


def _c(card: Optional[Card]) -> Dict[str, Any]:
    return card.content if card is not None and isinstance(card.content, dict) else {}


def chapter_texts(session: Session, project_id: int) -> List[Tuple[int, Card, str]]:
    out = []
    for card in BibleService(session).cards_of_type(project_id, "Chapter Text"):
        c = _c(card)
        n = int(c.get("chapter_number") or 0)
        if n > 0 and c.get("content"):
            out.append((n, card, str(c["content"])))
    out.sort(key=lambda t: t[0])
    return out


def _finding(kind: str, severity: str, message: str, *, chapter: Optional[int] = None, chapters: Sequence[int] = (), **extra: Any) -> Dict[str, Any]:
    f = {"kind": kind, "severity": severity, "message": message}
    if chapter is not None:
        f["chapter"] = chapter
    if chapters:
        f["chapters"] = list(chapters)
    f.update(extra)
    return f


def structural_audit(session: Session, project_id: int, chapter_count: int) -> List[Dict[str, Any]]:
    bible = BibleService(session)
    findings: List[Dict[str, Any]] = []
    for card in bible.cards_of_type(project_id, "Promise Payoff"):
        c = _c(card)
        if c.get("status") in ("planted", "planned", "active", "open", "reinforced"):
            findings.append(_finding("unresolved_setup", "high", f"Setup '{str(c.get('setup'))[:80]}' was never paid off", card_id=card.id, setup=str(c.get("setup"))[:120], planned_payoff=str(c.get("planned_payoff"))[:160], target=c.get("target_payoff_range")))
    for card in bible.cards_of_type(project_id, "Plot Thread"):
        c = _c(card)
        last = int(c.get("last_advanced_chapter") or 0)
        opening = int(c.get("opening_chapter") or 1)
        if c.get("status") in ("active", "planned") and c.get("thread_type") == "main_plot" and last < chapter_count:
            findings.append(_finding("main_plot_not_closed", "high", f"Main plot '{c.get('name')}' last advanced in chapter {last} of {chapter_count}", card_id=card.id))
        elif c.get("status") in ("active", "planned") and last and chapter_count - last > max(6, chapter_count // 3):
            findings.append(_finding("subplot_disappeared", "medium", f"Thread '{c.get('name')}' last advanced in chapter {last}, then vanished", card_id=card.id, chapter=last))
        elif c.get("status") in ("active", "planned") and not last and opening < chapter_count:
            findings.append(_finding("subplot_never_started", "medium", f"Thread '{c.get('name')}' planned from chapter {opening} never advanced", card_id=card.id))
    # Tension plateau from stored outline targets vs style reports.
    tensions = []
    for card in bible.cards_of_type(project_id, "Chapter Outline"):
        c = _c(card)
        if c.get("target_tension") is not None:
            tensions.append((int(c.get("chapter_number") or 0), int(c["target_tension"])))
    tensions.sort()
    if len(tensions) >= 6:
        vals = [t for _, t in tensions]
        run = 1
        for i in range(1, len(vals)):
            run = run + 1 if vals[i] == vals[i - 1] else 1
            if run == 5:
                findings.append(_finding("tension_plateau", "low", f"Planned tension flat for 5+ chapters ending at chapter {tensions[i][0]}", chapter=tensions[i][0]))
        peak_idx = max(range(len(vals)), key=lambda k: vals[k])
        if peak_idx < len(vals) * 0.5:
            findings.append(_finding("early_climax", "medium", f"Tension peak at chapter {tensions[peak_idx][0]} is in the first half", chapter=tensions[peak_idx][0]))
    return findings


def continuity_audit(session: Session, project_id: int) -> List[Dict[str, Any]]:
    """Cross-chapter checks using claims already stored on the pipeline runs."""
    findings: List[Dict[str, Any]] = []
    runs = session.exec(select(ChapterPipelineRun).where(ChapterPipelineRun.project_id == project_id, ChapterPipelineRun.status == "committed").order_by(ChapterPipelineRun.chapter_number)).all()
    latest: Dict[int, ChapterPipelineRun] = {}
    for r in runs:
        latest[r.chapter_number] = r
    dead: Dict[str, int] = {}
    injured: Dict[str, int] = {}
    for n in sorted(latest):
        r = latest[n]
        for cl in ((r.validation_report or {}).get("metrics") or {}).get("claims") or []:
            kind, subj = str(cl.get("kind")), str(cl.get("subject"))
            if kind == "death":
                dead[subj] = n
            elif kind == "injury":
                injured[subj] = n
        text = ""
        card = session.get(Card, r.chapter_card_id) if r.chapter_card_id else None
        text = str(_c(card).get("content") or "")
        for who, when in list(dead.items()):
            if when < n and re.search(rf"\b{re.escape(who)}\b\s+(said|asked|walked|smiled|laughed|nodded|stood|ran)", text):
                findings.append(_finding("dead_character_acts", "high", f"{who} died in chapter {when} but acts in chapter {n}", chapter=n, subject=who))
    return findings


def prose_audit(chapters: Sequence[Tuple[int, Card, str]], fingerprint: Dict[str, Any]) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    openings = Counter()
    endings = Counter()
    first_sentences: Dict[str, List[int]] = {}
    last_sentences: Dict[str, List[int]] = {}
    povs: List[Tuple[int, str]] = []
    tenses: List[Tuple[int, float]] = []
    sentence_means: List[float] = []
    phrase_counter: Counter = Counter()
    for n, _, text in chapters:
        paras = split_paragraphs(text)
        if not paras:
            continue
        sents = split_sentences(text)
        m = measure(text)
        openings[m.opening_type] += 1
        endings[m.ending_type] += 1
        fs = " ".join(tokenize(sents[0])[:6]) if sents else ""
        ls = " ".join(tokenize(sents[-1])[:6]) if sents else ""
        first_sentences.setdefault(fs, []).append(n)
        last_sentences.setdefault(ls, []).append(n)
        povs.append((n, "first" if m.first_person_ratio >= 0.55 else ("third" if m.third_person_ratio >= 0.55 else "mixed")))
        tenses.append((n, m.past_tense_ratio))
        sentence_means.append(float(m.sentence_len.get("mean") or 0))
        toks = tokenize(text)
        for i in range(0, max(0, len(toks) - 4)):
            g = tuple(toks[i:i + 5])
            phrase_counter[g] += 1
    for fs, ns in first_sentences.items():
        if fs and len(ns) >= 3:
            findings.append(_finding("chapter_opening_repetition", "medium", f"{len(ns)} chapters open with the same words: '{fs}'", chapters=ns))
    for ls, ns in last_sentences.items():
        if ls and len(ns) >= 3:
            findings.append(_finding("chapter_ending_repetition", "medium", f"{len(ns)} chapters end with the same words: '{ls}'", chapters=ns))
    if len(chapters) >= 4 and openings and openings.most_common(1)[0][1] >= len(chapters) * 0.85 and len(openings) == 1:
        findings.append(_finding("opening_type_monotony", "low", f"Every chapter opens as '{openings.most_common(1)[0][0]}'"))
    dominant_pov = Counter(p for _, p in povs).most_common(1)[0][0] if povs else "unknown"
    expected_pov = ((fingerprint.get("layers") or {}).get("pov_focalization") or {}).get("features", {}).get("pov", "")
    for n, p in povs:
        if p != dominant_pov and p != "mixed":
            findings.append(_finding("pov_drift", "high", f"Chapter {n} is {p}-person while the novel is {dominant_pov}-person", chapter=n))
    if expected_pov and dominant_pov != "unknown" and not expected_pov.startswith(dominant_pov):
        findings.append(_finding("pov_contract_mismatch", "medium", f"Novel POV '{dominant_pov}' differs from the contract '{expected_pov}'"))
    if tenses:
        median = sorted(t for _, t in tenses)[len(tenses) // 2]
        for n, t in tenses:
            if abs(t - median) > 0.35:
                findings.append(_finding("tense_drift", "high", f"Chapter {n} past-tense ratio {t:.2f} vs novel median {median:.2f}", chapter=n))
    stock = [(" ".join(g), c) for g, c in phrase_counter.items() if c >= max(4, len(chapters) // 2) and sum(1 for w in g if len(w) > 3) >= 3]
    for phrase, count in sorted(stock, key=lambda x: -x[1])[:10]:
        findings.append(_finding("stock_phrase", "low", f"Phrase '{phrase}' recurs {count} times", phrase=phrase, count=count))
    if len(sentence_means) >= 4 and max(sentence_means) - min(sentence_means) < 1.0:
        findings.append(_finding("rhythm_flat", "low", "Sentence length barely varies across chapters"))
    return findings


def character_audit(session: Session, project_id: int, chapters: Sequence[Tuple[int, Card, str]]) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    joined = "\n".join(t for _, _, t in chapters).lower()
    for card in BibleService(session).cards_of_type(project_id, "Character Card"):
        c = _c(card)
        name = str(c.get("name") or card.title)
        role = str(c.get("role_type") or "")
        first = name.split()[0].lower() if name else ""
        mentions = len(re.findall(rf"\b{re.escape(first)}\b", joined)) if first else 0
        if role in ("Protagonist", "Antagonist", "Supporting Character") and mentions == 0:
            findings.append(_finding("unused_major_character", "medium", f"{role} '{name}' never appears in the manuscript", card_id=card.id, subject=name))
        elif role == "Antagonist" and mentions < max(3, len(chapters) // 4):
            findings.append(_finding("antagonist_underused", "low", f"Antagonist '{name}' appears only {mentions} times", card_id=card.id, subject=name))
    return findings


def originality_audit(session: Session, project_id: int, chapters: Sequence[Tuple[int, Card, str]]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    profile = source_profile_for(session, project_id)
    if profile is None or not chapters:
        return [], {"passed": True, "skipped": "no source profile"}
    findings: List[Dict[str, Any]] = []
    allowed = []
    for card in BibleService(session).cards_of_type(project_id, "Character Card") + BibleService(session).cards_of_type(project_id, "Scene Card") + BibleService(session).cards_of_type(project_id, "Item Card") + BibleService(session).cards_of_type(project_id, "Organization Card"):
        allowed.append(str(_c(card).get("name") or card.title))
    whole = "\n\n".join(t for _, _, t in chapters)
    # Whole-manuscript beat sequence vs source.
    beats: List[str] = []
    for card in BibleService(session).cards_of_type(project_id, "Chapter Outline"):
        for b in _c(card).get("beats") or []:
            if isinstance(b, dict) and b.get("function"):
                beats.append(str(b["function"]))
    rep = fw.check_text(whole, profile, allowed_names=allowed, beat_sequence=beats, max_beat_lcs_ratio=0.7)
    for f in rep.findings:
        if f.severity in ("critical", "high"):
            ch = None
            if f.span:
                pos = 0
                for n, _, t in chapters:
                    if pos <= f.span[0] < pos + len(t) + 2:
                        ch = n
                        break
                    pos += len(t) + 2
            findings.append(_finding(f"source_{f.check}", "high" if f.severity == "high" else "critical", f.detail, chapter=ch, matched=f.matched))
    return findings, {"passed": rep.passed, "scores": rep.scores}


def whole_novel_audit(session: Session, project_id: int, chapter_count: int) -> Dict[str, Any]:
    chapters = chapter_texts(session, project_id)
    fingerprint = _c(BibleService(session).singleton(project_id, "Narrative Fingerprint"))
    findings: List[Dict[str, Any]] = []
    if len(chapters) != chapter_count:
        findings.append(_finding("chapter_count_mismatch", "critical", f"{len(chapters)} chapter texts for a {chapter_count}-chapter plan"))
    findings += structural_audit(session, project_id, chapter_count)
    findings += continuity_audit(session, project_id)
    findings += prose_audit(chapters, fingerprint)
    findings += character_audit(session, project_id, chapters)
    orig, orig_scores = originality_audit(session, project_id, chapters)
    findings += orig
    counts = Counter(f["severity"] for f in findings)
    blocking = [f for f in findings if f["severity"] in ("critical", "high")]
    return {"version": AUDIT_VERSION, "chapters": len(chapters), "words": sum(measure(t).unit_count for _, _, t in chapters), "findings": findings, "counts": dict(counts), "blocking": len(blocking), "passed": not blocking, "originality": orig_scores}


# ------------------------------------------------------------ global repair

def repairable_chapters(audit: Dict[str, Any]) -> List[int]:
    """Chapters with blocking findings that a chapter-local rewrite can fix."""
    fixable = {"pov_drift", "tense_drift", "dead_character_acts"}
    chs = set()
    for f in audit.get("findings") or []:
        if f["severity"] not in ("critical", "high"):
            continue
        if f["kind"] in fixable or f["kind"].startswith("source_"):
            if f.get("chapter"):
                chs.add(int(f["chapter"]))
    return sorted(chs)


async def global_repair(session: Session, *, project_id: int, chapter_count: int, client: ModelClient, audit: Dict[str, Any], lease_check: Optional[Callable[[], None]] = None, checkpoint: Optional[Dict[str, Any]] = None, save_checkpoint: Optional[Callable[[Dict[str, Any]], None]] = None) -> Dict[str, Any]:
    """Rewrite blocking chapters, re-validate, then rewind canon/ledgers to the first changed chapter and replay forward.

    ``checkpoint`` (persisted by the caller through ``save_checkpoint``) makes the
    operation resumable: repaired chapters and the last replayed chapter are
    recorded so an interrupted run continues without repeating model calls or
    duplicating ledger mutations.
    """
    from app.services.autonomous import rewind as rewind_mod

    check = lease_check or (lambda: None)
    cp: Dict[str, Any] = dict(checkpoint or {})
    persist = save_checkpoint or (lambda d: None)
    targets = repairable_chapters(audit)[:MAX_GLOBAL_REPAIR_CHAPTERS]
    repaired: List[int] = list(cp.get("repaired") or [])
    if not targets and not repaired:
        return {"repaired": [], "rebuilt_from": None, "audit": audit, "repaired_findings": 0}
    profile = source_profile_for(session, project_id)
    fingerprint = _c(BibleService(session).singleton(project_id, "Narrative Fingerprint"))
    compiler = ChapterContextCompiler(session)
    by_chapter = {n: (card, text) for n, card, text in chapter_texts(session, project_id)}
    repaired_findings = int(cp.get("repaired_findings") or 0)
    for n in targets:
        if n in repaired or n in (cp.get("skipped") or []):
            continue
        check()
        card, text = by_chapter[n]
        issues = [v.Issue(layer="audit", code=f["kind"], severity=f["severity"], message=f["message"], span=None, hint="Rewrite only what is needed to remove this problem; keep every fact, beat and the chapter's ending state.") for f in audit["findings"] if f.get("chapter") == n and f["severity"] in ("critical", "high")]
        if not issues:
            continue
        try:
            ctx = compiler.compile(project_id=project_id, chapter_number=n, regenerate=True, budget_chars=16000, example_budget_chars=0)
            session.rollback()
        except Exception as exc:  # noqa: BLE001 - compile for an already committed chapter should work; report otherwise
            raise fail.StageFailure(fail.STALE_DEPENDENCY, f"Cannot recompile chapter {n} for global repair: {exc}")
        raw = await client.text(role="whole_novel_editor", system_prompt=REPAIR_SYSTEM_PROMPT, user_prompt=build_repair_prompt(ctx, text, issues), prompt_version=GLOBAL_REPAIR_PROMPT_VERSION, stage=f"GLOBAL_REPAIR:ch{n}")
        check()
        prose, model_claims = claims_mod.split_prose_and_claims(raw)
        report, all_claims, model_claims = validate_draft(session, ctx, raw, profile=profile, fingerprint=fingerprint)
        if report.blocking:
            cp.setdefault("skipped", []).append(n)
            persist(cp)
            continue  # keep the original; the finding stays in the report
        c = _c(card)
        c.setdefault("revisions", []).append({"replaced_at": datetime.now().isoformat(timespec="seconds"), "reason": "global_repair", "issues": [i.code for i in issues], "previous_hash": provenance.content_hash(text)})
        c["content"] = prose
        c["sync_status"] = "pending"
        c["repaired_by_global_audit"] = True
        card.content = c
        flag_modified(card, "content")
        session.add(card)
        session.commit()
        repaired.append(n)
        repaired_findings += len(issues)
        cp.update({"repaired": repaired, "repaired_findings": repaired_findings})
        persist(cp)
    rebuild: Optional[Dict[str, Any]] = None
    rebuilt_from = None
    if repaired:
        rebuilt_from = min(repaired)
        resume_after = int(cp.get("replayed_through") or 0)

        def _ckpt(n: int) -> None:
            check()
            cp["replayed_through"] = n
            persist(cp)

        rebuild = rewind_mod.rebuild_forward(session, project_id, from_chapter=rebuilt_from, to_chapter=chapter_count, checkpoint=_ckpt, resume_after=resume_after)
        if not rebuild["verification"]["ok"]:
            raise fail.StageFailure(fail.INTERNAL_CONTRADICTION, "Canon rebuild left stale references", detail=rebuild["verification"])
    final_audit = whole_novel_audit(session, project_id, chapter_count)
    return {"repaired": repaired, "skipped": cp.get("skipped") or [], "rebuilt_from": rebuilt_from, "rebuild": rebuild, "audit": final_audit, "repaired_findings": repaired_findings}


__all__ = ["AUDIT_VERSION", "GLOBAL_REPAIR_PROMPT_VERSION", "MAX_GLOBAL_REPAIR_CHAPTERS", "chapter_texts", "character_audit", "continuity_audit", "global_repair", "originality_audit", "prose_audit", "repairable_chapters", "structural_audit", "whole_novel_audit"]
