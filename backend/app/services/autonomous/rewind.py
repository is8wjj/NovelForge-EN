"""Canon and ledger rewind: reconstruct derived state as of chapter N-1, then replay N..latest.

Every chapter-derived mutation written by ``forge.sync`` is traceable: canon
facts carry ``valid_from_chapter`` + ``chapter_card_id`` (temporal, append-only),
ledger cards carry ``history`` entries with ``chapter_number``, and Timeline
Event / State Packet cards are keyed by chapter number. Rewinding therefore
means:

1. delete canon facts with ``valid_from_chapter >= N`` (source 'sync');
2. roll every ledger card back through its history entries from chapters >= N
   (restoring ``previous`` values, dropping knowers learned at >= N);
3. delete Timeline Event / State Packet cards of chapters >= N;
4. mark the manifest at N-1;
5. replay ``synchronize_chapter`` for N..latest in order.

The operation is checkpointed on the job (``rewind_progress``) so an
interruption after chapter M resumes at M+1 without duplicating mutations;
because every step is idempotent, repeating the whole rewind yields the same
ledger.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import Session, select

from app.db.models import CanonFact, Card
from app.services.bible.bible_service import BibleService
from app.services.forge import canon as canon_store
from app.services.forge import claims as claims_mod
from app.services.forge import provenance
from app.services.forge import sync as sync_mod

LEDGER_TYPES = ("Plot Thread", "Promise Payoff", "Relationship Arc", "Knowledge Fact", "Character Card", "Item Card", "World Rule")
CHAPTER_KEYED_TYPES = ("Timeline Event", sync_mod.STATE_PACKET_TYPE)
REWIND_VERSION = "rewind-1"


def _c(card: Optional[Card]) -> Dict[str, Any]:
    return card.content if card is not None and isinstance(card.content, dict) else {}


def _rollback_card(card: Card, from_chapter: int) -> int:
    """Undo history entries from chapters >= from_chapter (newest first). Returns entries undone."""
    c = _c(card)
    history = [h for h in (c.get("history") or []) if isinstance(h, dict)]
    keep: List[Dict[str, Any]] = []
    undo: List[Dict[str, Any]] = []
    for h in history:
        ch = h.get("chapter_number")
        (undo if isinstance(ch, int) and ch >= from_chapter and h.get("accepted_by") == "ai" else keep).append(h)
    for h in reversed(undo):
        field = str(h.get("field") or "")
        if not field:
            continue
        if field == "last_advanced_chapter":
            c[field] = max([k.get("new") for k in keep if k.get("field") == field and isinstance(k.get("new"), int)] + [0])
        elif "previous" in h:
            c[field] = h.get("previous")
        if field == "status" and h.get("previous") in (None, "") and c.get(field) is None:
            c[field] = "planted"
    field_payoff_reset(c, undo)
    # Knowledge Fact knowers learned in rewound chapters are forgotten again.
    if isinstance(c.get("knowers"), list):
        for k in c["knowers"]:
            if isinstance(k, dict) and isinstance(k.get("learned_chapter"), int) and k["learned_chapter"] >= from_chapter and k.get("how_learned") == "chapter text":
                k.update({"state": "unaware", "learned_chapter": None, "how_learned": ""})
    if undo:
        c["history"] = keep
        card.content = c
        flag_modified(card, "content")
    return len(undo)


def field_payoff_reset(c: Dict[str, Any], undo: List[Dict[str, Any]]) -> bool:
    if any(h.get("field") == "status" and h.get("new") == "paid_off" for h in undo):
        c.pop("payoff_chapter", None)
        return True
    return False


def rewind_to(session: Session, project_id: int, from_chapter: int) -> Dict[str, Any]:
    """Reconstruct canon + ledgers as of ``from_chapter - 1``. Idempotent."""
    bible = BibleService(session)
    facts_deleted = canon_store.delete_facts_from_chapter(session, project_id, from_chapter)
    undone = 0
    for t in LEDGER_TYPES:
        for card in bible.cards_of_type(project_id, t):
            n = _rollback_card(card, from_chapter)
            if n:
                session.add(card)
                undone += n
    removed = 0
    for t in CHAPTER_KEYED_TYPES:
        for card in bible.cards_of_type(project_id, t):
            if int(_c(card).get("chapter_number") or 0) >= from_chapter:
                session.delete(card)
                removed += 1
    for card in bible.cards_of_type(project_id, "Chapter Text"):
        c = _c(card)
        if int(c.get("chapter_number") or 0) >= from_chapter and c.get("sync_status") == "synchronized":
            c["sync_status"] = "pending"
            card.content = c
            flag_modified(card, "content")
            session.add(card)
    manifest = provenance.get_manifest(session, project_id, create=True)
    manifest.latest_committed_chapter = min(int(manifest.latest_committed_chapter), from_chapter - 1)
    manifest.next_allowed_chapter = manifest.latest_committed_chapter + 1
    manifest.canon_revision = int(manifest.canon_revision) + 1
    manifest.updated_at = datetime.now()
    session.add(manifest)
    session.commit()
    return {"from_chapter": from_chapter, "facts_deleted": facts_deleted, "history_entries_undone": undone, "chapter_cards_removed": removed, "canon_revision": manifest.canon_revision}


def replay_chapter(session: Session, project_id: int, n: int) -> Dict[str, Any]:
    """Re-synchronize one committed chapter text against the current canon."""
    bible = BibleService(session)
    card = next((c for c in bible.cards_of_type(project_id, "Chapter Text") if int(_c(c).get("chapter_number") or 0) == n), None)
    if card is None:
        raise ValueError(f"no Chapter Text for chapter {n}")
    c = _c(card)
    text = str(c.get("content") or "")
    outline = next((o for o in bible.cards_of_type(project_id, "Chapter Outline") if int(_c(o).get("chapter_number") or 0) == n), None)
    oc = _c(outline)
    allowed = list(oc.get("allowed_outcomes") or []) + [str(b.get("description") or "") for b in oc.get("beats") or [] if isinstance(b, dict)]
    claims = claims_mod.extract_claims(text)
    rep = sync_mod.synchronize_chapter(session, project_id=project_id, chapter_number=n, chapter_card_id=card.id, pov=str(c.get("pov") or oc.get("pov") or ""), participants=list(c.get("participants") or oc.get("participants") or []), prose=text, claims=claims, model_claims=None, allowed_outcomes=allowed, outline_card_id=outline.id if outline else None)
    return {"chapter": n, "committed": len(rep.get("committed") or []), "canon_revision": rep.get("canon_revision_after")}


def rebuild_forward(session: Session, project_id: int, *, from_chapter: int, to_chapter: int, checkpoint: Optional[Callable[[int], None]] = None, resume_after: int = 0) -> Dict[str, Any]:
    """Rewind to ``from_chapter`` (unless resuming) and replay chapters in order, checkpointing each."""
    report: Dict[str, Any] = {"version": REWIND_VERSION, "from_chapter": from_chapter, "to_chapter": to_chapter, "replayed": [], "resumed_after": resume_after}
    if resume_after < from_chapter:
        report["rewind"] = rewind_to(session, project_id, from_chapter)
        start = from_chapter
    else:
        start = resume_after + 1
    for n in range(start, to_chapter + 1):
        report["replayed"].append(replay_chapter(session, project_id, n))
        if checkpoint:
            checkpoint(n)
    report["verification"] = verify_no_stale_references(session, project_id, to_chapter)
    return report


def verify_no_stale_references(session: Session, project_id: int, latest: int) -> Dict[str, Any]:
    """No active canon fact may point at a Chapter Text revision that is no longer the accepted one, and none beyond ``latest``."""
    bible = BibleService(session)
    accepted = {c.id for c in bible.cards_of_type(project_id, "Chapter Text")}
    rows = session.exec(select(CanonFact).where(CanonFact.project_id == project_id, CanonFact.source == "sync", CanonFact.superseded_by_id.is_(None))).all()
    stale = [r.id for r in rows if (r.chapter_card_id and r.chapter_card_id not in accepted) or r.valid_from_chapter > latest]
    packets = [c.id for c in bible.cards_of_type(project_id, sync_mod.STATE_PACKET_TYPE) if int(_c(c).get("chapter_number") or 0) > latest]
    return {"ok": not stale and not packets, "stale_fact_ids": stale[:20], "orphan_packet_ids": packets[:20], "active_sync_facts": len(rows)}


__all__ = ["CHAPTER_KEYED_TYPES", "LEDGER_TYPES", "REWIND_VERSION", "rebuild_forward", "replay_chapter", "rewind_to", "verify_no_stale_references"]
