"""Manual live qualification of a Kimi K3 configuration against a private EPUB (docs/live-qualification.md).

Reads the provider configuration from ``NF_KIMI_*`` environment variables (never
from arguments, never committed), stores it in the configured database and drives
the autonomous pipeline stage by stage. Every subcommand appends a sanitized
record to the evidence file (``NF_EVIDENCE``, default
``docs/evidence/live-qualification.json``): counts, hashes, scores, ids,
timestamps and lease generations only. No prompt, response, chapter text or
credential is ever written.

Usage (from backend/, with NOVELFORGE_DB_PATH pointing at a throw-away database):

    python scripts/live_qualification.py preflight
    python scripts/live_qualification.py analyze
    python scripts/live_qualification.py select --chapters 6 --words 1500
    python scripts/live_qualification.py run [--until-chapters N]
    python scripts/live_qualification.py recover
    python scripts/live_qualification.py verify-durability
    python scripts/live_qualification.py exports
    python scripts/live_qualification.py originality
    python scripts/live_qualification.py report
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import os
import re
import sys
import zipfile
from datetime import datetime
from typing import Any, Dict, List, Optional
from xml.etree import ElementTree

BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, BACKEND)
os.environ.setdefault("AUTHND_TOKEN_MODE", "pool")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from sqlmodel import Session, select  # noqa: E402

from app.db.models import AutonomousNovelJob, ChapterPipelineRun, ExportArtifact, LLMConfig, ModelInvocation, ModelInvocationAttempt, StorylineCandidate  # noqa: E402
from app.db.session import engine  # noqa: E402
from app.services.autonomous import budget as budget_mod  # noqa: E402
from app.services.autonomous import lease as lease_mod  # noqa: E402
from app.services.autonomous import preflight as preflight_mod  # noqa: E402
from app.services.autonomous import runner as runner_mod  # noqa: E402
from app.services.autonomous.audit import chapter_texts  # noqa: E402

EVIDENCE = os.environ.get("NF_EVIDENCE", os.path.join(BACKEND, "..", "docs", "evidence", "live-qualification.json"))
STATE = os.environ.get("NF_QUAL_STATE", os.path.join(os.path.dirname(os.path.abspath(EVIDENCE)), ".live-qualification-state.json"))
CONFIG_NAME = "Live qualification (Kimi K3)"


# ------------------------------------------------------------------ helpers
def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _record(kind: str, payload: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(EVIDENCE)), exist_ok=True)
    rows: List[Dict[str, Any]] = []
    if os.path.exists(EVIDENCE):
        with open(EVIDENCE, "r", encoding="utf-8") as f:
            rows = json.load(f)
    rows.append({"at": _now(), "kind": kind, **payload})
    with open(EVIDENCE, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(json.dumps({"at": rows[-1]["at"], "kind": kind, **payload}, indent=2, ensure_ascii=False))


def _state() -> Dict[str, Any]:
    if os.path.exists(STATE):
        with open(STATE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_state(**kw: Any) -> None:
    st = {**_state(), **kw}
    os.makedirs(os.path.dirname(os.path.abspath(STATE)), exist_ok=True)
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(st, f)


def _env(name: str, default: Optional[str] = None, *, required: bool = False) -> Optional[str]:
    v = os.environ.get(name, default)
    if required and not v:
        sys.exit(f"{name} is required (see docs/live-qualification.md)")
    return v


def _config(session: Session) -> LLMConfig:
    provider = (_env("NF_KIMI_PROVIDER", "openai_compatible") or "").strip().lower()
    model = _env("NF_KIMI_MODEL", required=True)
    base = _env("NF_KIMI_API_BASE", "" if provider != "openai_compatible" else None, required=provider == "openai_compatible")
    key = _env("NF_KIMI_API_KEY", "", required=provider in ("openai", "openai_compatible", "anthropic", "google"))
    cfg = session.exec(select(LLMConfig).where(LLMConfig.display_name == CONFIG_NAME)).first() or LLMConfig(provider=provider, model_name=model, api_key=key or "", display_name=CONFIG_NAME)
    cfg.provider, cfg.model_name, cfg.api_key, cfg.api_base = provider, model, key or "", base or None
    session.add(cfg)
    session.commit()
    session.refresh(cfg)
    return cfg


def _job(session: Session) -> AutonomousNovelJob:
    jid = _state().get("job_id")
    job = session.get(AutonomousNovelJob, int(jid)) if jid else None
    if job is None:
        sys.exit("no qualification job yet: run `analyze` first")
    session.refresh(job)
    return job


def _budget() -> Dict[str, Any]:
    raw = _env("NF_BUDGET_JSON")
    if raw:
        return json.loads(raw)
    b: Dict[str, Any] = {"max_calls": 400, "max_total_tokens": 4_000_000, "max_output_tokens": 800_000, "max_repair_calls": 40}
    pin, pout = _env("NF_PRICE_INPUT"), _env("NF_PRICE_OUTPUT")
    if pin and pout:
        b["price_per_million"] = {"input": float(pin), "output": float(pout)}
        b["max_cost_usd"] = float(_env("NF_MAX_COST_USD", "25") or 25)
    return b


def _job_facts(session: Session, job: AutonomousNovelJob) -> Dict[str, Any]:
    return {"job_id": job.id, "status": job.status, "stage": job.stage, "quality_status": job.quality_status, "chapters_committed": job.chapters_committed, "chapter_count": job.chapter_count, "lease_generation": job.lease_generation, "lease_owner": job.lease_owner, "budget": budget_mod.usage_snapshot(session, job), "warnings": len(job.warnings or []), "error_category": (job.error or {}).get("category")}


def _client_factory():
    """Production factory, or the deterministic test model when NF_QUAL_FAKE_PROVIDER=1 (script self-test, no credentials)."""
    if os.environ.get("NF_QUAL_FAKE_PROVIDER") == "1":
        from tests.test_autonomous_pipeline import FakeClient

        fake = FakeClient()
        return lambda s, j, r: fake
    return runner_mod.default_client_factory


def _run(job_id: int, *, until_chapters: Optional[int] = None, owner: str) -> AutonomousNovelJob:
    factory = _client_factory()

    async def go() -> AutonomousNovelJob:
        with Session(engine) as s:
            runner = runner_mod.JobRunner(s, job_id, owner=owner, client_factory=factory)
            job = runner.job
            for _ in range(10_000):
                job = await runner.step()
                if job.status in runner_mod.TERMINAL or job.status in ("waiting_for_user", "paused"):
                    break
                stop_chapters = until_chapters is not None and job.stage == "CHAPTER_GENERATION_LOOP" and job.chapters_committed >= until_chapters
                stop_past_loop = until_chapters is not None and runner_mod.STAGES.index(job.stage) > runner_mod.STAGES.index("CHAPTER_GENERATION_LOOP")
                if stop_chapters or stop_past_loop:
                    # Deliberate stop between steps: hand the (queued) job back so another process can take it immediately.
                    if runner.lease is not None:
                        lease_mod.release(s, runner.lease)
                    break
            return job

    return asyncio.run(go())


# --------------------------------------------------------------- commands
def cmd_preflight(_: argparse.Namespace) -> int:
    with Session(engine) as s:
        cfg = _config(s)
        res = asyncio.run(preflight_mod.run_preflight(s, int(cfg.id), timeout=float(_env("NF_PREFLIGHT_TIMEOUT", "60") or 60)))
    d = preflight_mod.result_dict(res)
    _record("preflight", d)
    _save_state(llm_config_id=int(cfg.id))
    return 0 if d["passed"] else 1


def cmd_analyze(_: argparse.Namespace) -> int:
    path = _env("NF_EPUB_PATH", required=True)
    data = open(path, "rb").read()
    digest = hashlib.sha256(data).hexdigest()
    with Session(engine) as s:
        cfg = _config(s)
        job = runner_mod.create_job(s, filename=os.path.basename(path), data=data, llm_config_id=int(cfg.id), mode="fully_automatic", options={"quality_preset": "balanced", "max_repairs": 2, "analysis_concurrency": 4, "storyline_count": 7, "live_qualification": True}, budget=_budget(), idempotency_key=f"live-qual-{digest[:16]}")
        _save_state(job_id=int(job.id))
        _record("epub", {"filename_sanitized": re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.basename(path)), "bytes": len(data), "sha256": digest, "job_id": job.id})
    job = _run(int(job.id), owner="qual-analyze")
    with Session(engine) as s:
        job = s.get(AutonomousNovelJob, int(job.id))
        quality = ((job.stage_results or {}).get("INGEST") or {}).get("quality") or {}
        cands = s.exec(select(StorylineCandidate).where(StorylineCandidate.job_id == job.id).order_by(StorylineCandidate.option_index)).all()
        sims = [max([v for k, v in (c.similarity_to_others or {}).items()] or [0.0]) for c in cands]
        _record("analysis", {**_job_facts(s, job), "ingestion": {k: quality.get(k) for k in ("chapters", "words", "story_fraction", "boundary_confidence", "ok")}, "ingestion_warnings": len(quality.get("warnings") or []), "storylines": [{"id": c.id, "option_index": c.option_index, "title": c.title, "originality_score": c.originality_score, "rejected": c.rejected, "rejection_reason": c.rejection_reason, "max_similarity_to_others": max([v for v in (c.similarity_to_others or {}).values()] or [0.0])} for c in cands], "max_pairwise_similarity": max(sims) if sims else None})
    return 0 if job.stage == "STORYLINE_SELECTION" else 1


def cmd_select(args: argparse.Namespace) -> int:
    with Session(engine) as s:
        job = _job(s)
        cands = [c for c in s.exec(select(StorylineCandidate).where(StorylineCandidate.job_id == job.id)).all() if not c.rejected]
        if not cands:
            sys.exit("no eligible storyline candidates")
        best = sorted(cands, key=lambda c: (-float(c.originality_score or 0.0), max([v for v in (c.similarity_to_others or {}).values()] or [0.0]), c.id))[0]
        job = runner_mod.select_storyline(s, job, storyline_id=int(best.id), chapter_count=int(args.chapters), options={"words_per_chapter": int(args.words)})
        _record("selection", {"job_id": job.id, "storyline_id": best.id, "title": best.title, "originality_score": best.originality_score, "basis": "highest originality score among non-rejected candidates; tie-break lowest max similarity", "chapter_count": args.chapters, "words_per_chapter": args.words})
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    with Session(engine) as s:
        job = _job(s)
        before = job.lease_generation
        jid = int(job.id)
    lease_lost = False
    try:
        _run(jid, until_chapters=args.until_chapters, owner=f"qual-run-{os.getpid()}")
    except lease_mod.JobLeaseLost as exc:
        lease_lost = True
        print(f"lease not available: {exc} (another worker holds it; wait for expiry or run `recover`)")
    with Session(engine) as s:
        job = s.get(AutonomousNovelJob, jid)
        committed = sorted(r.chapter_number for r in s.exec(select(ChapterPipelineRun).where(ChapterPipelineRun.project_id == int(job.original_project_id or 0), ChapterPipelineRun.status == "committed")).all()) if job.original_project_id else []
        _record("run", {**_job_facts(s, job), "lease_generation_before": before, "committed_chapters": committed, "until_chapters": args.until_chapters, "lease_lost": lease_lost})
    return 0 if not lease_lost and job.status not in ("failed", "cancelled") else 1


def cmd_recover(args: argparse.Namespace) -> int:
    with Session(engine) as s:
        job = _job(s)
        expired_here = False
        if args.force_expire and job.lease_owner:
            # The kill failpoint left a lease held by a process that no longer exists on this host;
            # expire it (equivalent to waiting AUTONOMOUS_LEASE_SECONDS) so recovery can requeue.
            m = re.search(r"-(\d+)$", job.lease_owner or "")
            pid = int(m.group(1)) if m else None
            alive = pid is not None and os.path.exists(f"/proc/{pid}")
            if pid is not None and not alive:
                job.lease_expires_at = datetime.now()
                s.add(job)
                s.commit()
                expired_here = True
        n = runner_mod.recover_stale_leases(s)
        s.refresh(job)
        _record("recover", {**_job_facts(s, job), "requeued_jobs": n, "forced_expiry_of_dead_owner": expired_here})
    return 0


def cmd_verify_durability(_: argparse.Namespace) -> int:
    with Session(engine) as s:
        job = _job(s)
        pid = int(job.original_project_id or 0)
        runs = s.exec(select(ChapterPipelineRun).where(ChapterPipelineRun.project_id == pid, ChapterPipelineRun.status == "committed")).all()
        per_chapter: Dict[int, int] = {}
        for r in runs:
            per_chapter[r.chapter_number] = per_chapter.get(r.chapter_number, 0) + 1
        dup_chapters = sorted(k for k, v in per_chapter.items() if v > 1)
        arts = s.exec(select(ExportArtifact).where(ExportArtifact.job_id == job.id)).all()
        dup_exports = len(arts) != len({a.kind for a in arts})
        cands = s.exec(select(StorylineCandidate).where(StorylineCandidate.job_id == job.id)).all()
        selected = [c for c in cands if c.selected]
        gens = sorted({a.detail.get("lease_generation") for a in s.exec(select(runner_mod.JobStageAttempt).where(runner_mod.JobStageAttempt.job_id == job.id)).all() if a.detail and a.detail.get("lease_generation") is not None})
        stale_rejected: Optional[bool] = None
        if job.status in runner_mod.TERMINAL or job.status in ("waiting_for_user", "paused") or job.lease_owner:
            stale = lease_mod.Lease(job_id=int(job.id), owner="stale-qualification-worker", generation=max(0, int(job.lease_generation) - 1), expires_at=datetime.now())
            try:
                lease_mod.fenced_update(s, stale, {"progress_message": "STALE WRITE MUST NOT LAND"})
                stale_rejected = False
            except lease_mod.JobLeaseLost:
                stale_rejected = True
            s.refresh(job)
        _record("durability", {**_job_facts(s, job), "lease_generations_seen": gens, "duplicate_chapters": dup_chapters, "duplicate_exports": dup_exports, "selected_storylines": len(selected), "stale_generation_update_rejected": stale_rejected, "committed_chapters": sorted(per_chapter)})
    ok = not dup_chapters and not dup_exports and len(selected) == 1 and stale_rejected is not False
    return 0 if ok else 1


def _validate_epub(data: bytes) -> Dict[str, Any]:
    zf = zipfile.ZipFile(io.BytesIO(data))
    infos = zf.infolist()
    first_ok = bool(infos) and infos[0].filename == "mimetype" and infos[0].compress_type == zipfile.ZIP_STORED and zf.read("mimetype") == b"application/epub+zip"
    container = ElementTree.fromstring(zf.read("META-INF/container.xml"))
    opf_path = next(el.attrib["full-path"] for el in container.iter() if el.tag.endswith("rootfile"))
    opf = ElementTree.fromstring(zf.read(opf_path))
    ns = {"opf": "http://www.idpf.org/2007/opf"}
    items = {it.attrib["id"]: it.attrib["href"] for it in opf.find("opf:manifest", ns)}
    spine = [items[ref.attrib["idref"]] for ref in opf.find("opf:spine", ns)]
    base = os.path.dirname(opf_path)
    names = set(zf.namelist())
    missing = [h for h in spine if (f"{base}/{h}" if base else h) not in names]
    chapters = [h for h in spine if h.startswith("chapter-")]
    ordered = chapters == sorted(chapters)
    nav_ok = any(h.endswith("nav.xhtml") for h in items.values()) and any(h.endswith("toc.ncx") for h in items.values())
    title = opf.find(".//{http://purl.org/dc/elements/1.1/}title")
    return {"mimetype_first_stored": first_ok, "spine_items": len(spine), "chapter_files": len(chapters), "chapters_in_order": ordered, "missing_spine_files": missing, "nav_and_ncx": nav_ok, "has_title_metadata": title is not None and bool((title.text or "").strip())}


def _validate_docx(data: bytes, n_chapters: int) -> Dict[str, Any]:
    zf = zipfile.ZipFile(io.BytesIO(data))
    doc = zf.read("word/document.xml").decode("utf-8", "replace")
    headings = len(re.findall(r'<w:pStyle w:val="Heading1"', doc))
    return {"has_document_xml": True, "heading1_count": headings, "chapter_headings_present": headings >= n_chapters, "content_types": "[Content_Types].xml" in zf.namelist()}


def _validate_text_like(data: bytes, titles: List[str], *, markdown: bool) -> Dict[str, Any]:
    """Chapter headings must appear in order: ``## Title`` lines in Markdown, upper-cased title lines in plain text."""
    text = data.decode("utf-8", "replace")
    lines = [ln.strip() for ln in text.splitlines()]
    wanted = [f"## {t}" if markdown else t.upper() for t in titles]
    pos = 0
    found = 0
    for w in wanted:
        try:
            pos = lines.index(w, pos) + 1
            found += 1
        except ValueError:
            break
    return {"utf8": True, "headings_found": found, "chapter_headings_present": found == len(titles), "bytes": len(data)}


def cmd_exports(_: argparse.Namespace) -> int:
    with Session(engine) as s:
        job = _job(s)
        arts = s.exec(select(ExportArtifact).where(ExportArtifact.job_id == job.id).order_by(ExportArtifact.kind)).all()
        n = int(job.chapter_count or 0)
        titles = [str(((card.content or {}) if isinstance(card.content, dict) else {}).get("title") or f"Chapter {num}") for num, card, _ in chapter_texts(s, int(job.original_project_id or 0))]
        out: Dict[str, Any] = {"job_id": job.id, "chapter_count": n, "artifacts": {}}
        ok = bool(arts) and len(titles) == n
        for a in arts:
            entry: Dict[str, Any] = {"filename": a.filename, "bytes": a.size_bytes, "sha256": hashlib.sha256(a.data or b"").hexdigest(), "hash_matches_row": hashlib.sha256(a.data or b"").hexdigest() == a.content_hash}
            try:
                if a.kind == "epub":
                    entry["structure"] = _validate_epub(a.data)
                    ok &= entry["structure"]["mimetype_first_stored"] and entry["structure"]["chapters_in_order"] and not entry["structure"]["missing_spine_files"] and entry["structure"]["chapter_files"] == n
                elif a.kind == "docx":
                    entry["structure"] = _validate_docx(a.data, n)
                    ok &= entry["structure"]["chapter_headings_present"]
                elif a.kind in ("markdown", "text"):
                    entry["structure"] = _validate_text_like(a.data, titles, markdown=a.kind == "markdown")
                    ok &= entry["structure"]["chapter_headings_present"]
                elif a.kind == "report":
                    rep = json.loads(a.data.decode("utf-8"))
                    entry["structure"] = {"json": True, "keys": sorted(rep.keys())[:20], "chapters_listed": len(rep.get("chapters") or [])}
                    ok &= {"audit", "chapters", "run"} <= set(rep.keys())
            except Exception as exc:  # noqa: BLE001 - recorded as a validation failure
                entry["structure_error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
                ok = False
            out["artifacts"][a.kind] = entry
        out["all_valid"] = ok
        _record("exports", out)
    return 0 if ok else 1


def _ngrams(text: str, n: int) -> set:
    toks = re.findall(r"[a-z']+", text.lower())
    return {" ".join(toks[i:i + n]) for i in range(max(0, len(toks) - n + 1))}


def cmd_originality(_: argparse.Namespace) -> int:
    from app.services.forge import firewall as fw

    with Session(engine) as s:
        job = _job(s)
        gen = chapter_texts(s, int(job.original_project_id))
        src = chapter_texts(s, int(job.source_project_id))
        gen_text = "\n\n".join(t for _, _, t in gen)
        src_text = "\n\n".join(t for _, _, t in src)
        g8, s8 = _ngrams(gen_text, 8), _ngrams(src_text, 8)
        overlap8 = len(g8 & s8) / max(1, len(g8))
        g_sents = {x.strip().lower() for x in re.split(r"(?<=[.!?])\s+", gen_text) if len(x.split()) >= 8}
        s_sents = {x.strip().lower() for x in re.split(r"(?<=[.!?])\s+", src_text) if len(x.split()) >= 8}
        shared_sentences = len(g_sents & s_sents)
        profile = fw.source_profile_for(s, int(job.original_project_id)) if hasattr(fw, "source_profile_for") else None
        names_hits: Optional[int] = None
        if profile is not None:
            rep = fw.check_text(gen_text, profile)
            names_hits = sum(1 for f in rep.findings if f.check in ("names", "named_entities", "entity"))
            scores = rep.scores
            passed = rep.passed
        else:
            scores, passed = {}, None
        open_sim = len(_ngrams(gen_text[:2000], 5) & _ngrams(src_text[:2000], 5)) / max(1, len(_ngrams(gen_text[:2000], 5)))
        end_sim = len(_ngrams(gen_text[-2000:], 5) & _ngrams(src_text[-2000:], 5)) / max(1, len(_ngrams(gen_text[-2000:], 5)))
        _record("originality", {"job_id": job.id, "generated_chapters": len(gen), "generated_words": len(gen_text.split()), "source_chapters": len(src), "eight_gram_overlap_ratio": round(overlap8, 5), "shared_long_sentences": shared_sentences, "opening_5gram_similarity": round(open_sim, 4), "ending_5gram_similarity": round(end_sim, 4), "firewall_passed": passed, "firewall_scores": scores, "named_entity_findings": names_hits})
    return 0 if overlap8 < 0.01 and shared_sentences == 0 and passed is not False else 1


def cmd_report(_: argparse.Namespace) -> int:
    with Session(engine) as s:
        job = _job(s)
        inv = s.exec(select(ModelInvocation).where(ModelInvocation.job_id == job.id)).all()
        att = s.exec(select(ModelInvocationAttempt).where(ModelInvocationAttempt.job_id == job.id)).all()
        by_status: Dict[str, int] = {}
        by_cat: Dict[str, int] = {}
        for a in att:
            by_status[a.status] = by_status.get(a.status, 0) + 1
            if a.error_category:
                by_cat[a.error_category] = by_cat.get(a.error_category, 0) + 1
        first_ok = sum(1 for i in inv if i.validation_status == "ok" and (i.selected_attempt or 1) == 1)
        structured = [i for i in inv if i.schema_name and i.schema_name != "text"]
        rep = {
            **_job_facts(s, job),
            "provider_path": "fake_test_model (no provider telemetry; script self-test)" if os.environ.get("NF_QUAL_FAKE_PROVIDER") == "1" else "live LLMModelClient",
            "models_used": sorted({(a.provider, a.model_name) for a in att}),
            "invocations": len(inv), "attempts": len(att), "attempt_status": by_status, "attempt_error_categories": by_cat,
            "first_attempt_valid_rate": round(first_ok / max(1, len(inv)), 4),
            "structured_invocations": len(structured), "structured_first_attempt_valid_rate": round(sum(1 for i in structured if i.validation_status == "ok" and (i.selected_attempt or 1) == 1) / max(1, len(structured)), 4),
            "schema_repair_attempts": sum(1 for a in att if a.status == "invalid"),
            "provider_retry_attempts": sum(1 for a in att if a.status in ("error", "timeout")),
            "fallback_attempts": sum(1 for a in att if a.fallback), "budget_refused_attempts": by_status.get("budget_refused", 0),
            "usage_reported_attempts": sum(1 for a in att if a.usage_reported), "usage_missing_attempts": sum(1 for a in att if a.usage_reported is False),
            "latency_ms": {"p50": sorted(a.latency_ms for a in att)[len(att) // 2] if att else None, "max": max((a.latency_ms for a in att), default=None)},
            "max_input_tokens_single_attempt": max((a.input_tokens for a in att), default=0),
            "by_role": {r: v for r, v in __import__("app.services.autonomous.export", fromlist=["run_summary"]).run_summary(s, job)["by_role"].items()},
            "quality_summary": job.quality_summary, "recovery_actions": len(__import__("app.services.autonomous.recovery", fromlist=["history"]).history(s, int(job.id), limit=1000)),
        }
        _record("report", rep)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("preflight").set_defaults(fn=cmd_preflight)
    sub.add_parser("analyze").set_defaults(fn=cmd_analyze)
    p = sub.add_parser("select")
    p.add_argument("--chapters", type=int, default=6)
    p.add_argument("--words", type=int, default=1500)
    p.set_defaults(fn=cmd_select)
    p = sub.add_parser("run")
    p.add_argument("--until-chapters", type=int, default=None)
    p.set_defaults(fn=cmd_run)
    p = sub.add_parser("recover")
    p.add_argument("--force-expire", action="store_true", help="expire the lease of a dead local owner process instead of waiting AUTONOMOUS_LEASE_SECONDS")
    p.set_defaults(fn=cmd_recover)
    sub.add_parser("verify-durability").set_defaults(fn=cmd_verify_durability)
    sub.add_parser("exports").set_defaults(fn=cmd_exports)
    sub.add_parser("originality").set_defaults(fn=cmd_originality)
    sub.add_parser("report").set_defaults(fn=cmd_report)
    args = ap.parse_args()
    # Same startup as the API process: migrations + bootstrap (card types, prompts) the pipeline depends on.
    from app.core.startup import startup

    startup()
    return int(args.fn(args))


if __name__ == "__main__":
    raise SystemExit(main())
