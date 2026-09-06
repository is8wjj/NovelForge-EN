"""Autonomous pipeline: EPUB upload -> storyline options -> selection -> finished novel, with no live model.

A deterministic ``FakeClient`` plays every model role. It also injects faults
the pipeline must catch: a malformed first storyline batch (format repair), a
storyline that leaks source entities (originality gate), two near-identical
storylines (diversity gate), one architecture that fails validation
(architect repair round), and a draft that leaks a source name (chapter repair).
The job is interrupted after chapter 2 and resumed in a fresh runner to prove
restart safety without duplicated chapters or canon.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import sys
import zipfile
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Type

import pytest
from sqlmodel import Session, select

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tests.fixtures import synthetic_novel as syn  # noqa: E402

pytestmark = pytest.mark.timeout(900)

CHAPTERS = 6
ORIGINAL_NAMES = ["Nadia Quill", "Teo Marsh", "Corvin Ashe", "Petra Vale", "Sable Rook"]
LOCATIONS = ["Harrow Quay", "the Salt Archive", "Kestrel Row"]


# ------------------------------------------------------------------ fixtures
def build_source_epub() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        manifest, spine, points = ['<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'], [], []
        items = [("cover.xhtml", "Cover", "<p>Cover</p>")] + [(f"ch{n:02d}.xhtml", f"Chapter {n}", "".join(f"<p>{p}</p>" for p in syn.chapter_text(n).split("\n\n"))) for n in range(1, syn.CHAPTER_COUNT + 1)] + [("afterword.xhtml", "Afterword", "<p>Thanks to everyone who read this far.</p>")]
        for i, (fname, label, body) in enumerate(items):
            manifest.append(f'<item id="it{i}" href="{fname}" media-type="application/xhtml+xml"/>')
            spine.append(f'<itemref idref="it{i}"/>')
            points.append(f'<navPoint id="n{i}" playOrder="{i + 1}"><navLabel><text>{label}</text></navLabel><content src="{fname}"/></navPoint>')
            zf.writestr(f"OEBPS/{fname}", f'<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml"><head><title>{label}</title></head><body><h1>{label}</h1>{body}</body></html>')
        zf.writestr("OEBPS/content.opf", f'<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Ledger of the Lantern Ward</dc:title><dc:creator>Fixture Author</dc:creator><dc:language>en</dc:language><dc:identifier id="id">urn:uuid:ledger</dc:identifier></metadata><manifest>{"".join(manifest)}</manifest><spine toc="ncx">{"".join(spine)}</spine></package>')
        zf.writestr("OEBPS/toc.ncx", f'<?xml version="1.0"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1"><head/><docTitle><text>Ledger</text></docTitle><navMap>{"".join(points)}</navMap></ncx>')
    return buf.getvalue()


def _storyline(i: int, *, leak: bool = False, clone_of: Optional[int] = None) -> Dict[str, Any]:
    settings = ["a mountain observatory", "a floating market city", "a snowbound rail depot", "a desert salt monastery", "an orbital shipyard", "a river delta of stilt villages", "a walled vineyard republic", "a glacier research station"]
    jobs = ["cartographer", "auctioneer", "signal engineer", "novice archivist", "hull inspector", "ferry pilot", "wine assessor", "ice-core driller"]
    conflicts = ["a forged map that erases a village", "an auction of a stolen relic", "a sabotaged timetable hiding a smuggling route", "a heresy trial built on a mistranslation", "a defective hull certified as sound", "a flood warning nobody believes", "a poisoned harvest blamed on the wrong estate", "a buried instrument that predicts a collapse"]
    antagonists = ["a guild that profits from erasure", "a broker who launders provenance", "a stationmaster who sells schedules", "an abbot protecting a lie", "an inspector paid to look away", "a councillor who owns the levees", "a rival house with a debt to hide", "a director who needs the funding"]
    mysteries = ["who ordered the village erased", "where the relic really came from", "which train carries the contraband", "what the original text said", "who signed the false certificate", "why the gauges were disabled", "who poisoned the vats", "what the instrument recorded"]
    climaxes = ["a public redrawing of the map", "a live auction reversed by proof of provenance", "a rerouted train exposed on the platform", "a reading of the true text before the synod", "a hull test in open water", "an opened floodgate at night", "a blind tasting before the assembly", "a core sample read aloud to the board"]
    endings = ["bittersweet", "triumphant", "open", "tragic", "hopeful", "quiet", "ironic", "redemptive"]
    j = clone_of if clone_of is not None else i
    cast = [f"{n} — {r}" for n, r in zip(ORIGINAL_NAMES, ["protagonist", "ally", "antagonist", "mentor", "rival"])]
    if leak:
        cast[0] = "Ilse Varn — protagonist"
    return {
        "title": f"Option {i + 1}: The {jobs[j].title()}" + (" (clone)" if clone_of is not None else ""), "genre": "mystery", "hook": f"A {jobs[j]} in {settings[j]} discovers {conflicts[j]}.",
        "premise": " ".join([f"In {settings[j]}, a {jobs[j]} named {ORIGINAL_NAMES[0]} uncovers {conflicts[j]}."] * 8), "protagonist": f"{ORIGINAL_NAMES[0]}, a {jobs[j]}", "central_desire": "to be believed", "internal_flaw": "notices everything, trusts no one",
        "antagonist": antagonists[j], "setting": settings[j] + (" near the Greywater Exchange" if leak else ""), "central_conflict": conflicts[j], "stakes": "a community's survival",
        "acts": [{"act": "Act I", "summary": f"{ORIGINAL_NAMES[0]} notices {conflicts[j]} and is dismissed."}, {"act": "Midpoint", "summary": f"The {antagonists[j]} is implicated."}, {"act": "Crisis", "summary": f"{ORIGINAL_NAMES[0]} loses an ally."}, {"act": "Climax", "summary": climaxes[j]}],
        "midpoint": f"{antagonists[j]} implicated", "crisis": "an ally lost", "climax": climaxes[j], "ending": f"{endings[j]} resolution", "ending_type": endings[j], "major_subplot": f"a friendship with {ORIGINAL_NAMES[1]}",
        "relationship_arc": f"{ORIGINAL_NAMES[0]} and {ORIGINAL_NAMES[1]} from distrust to alliance ({j})", "central_mystery": mysteries[j], "thematic_question": "Is noticing the same as knowing?", "main_cast": cast, "tone": "restrained", "pov_plan": "first person, past tense",
        "chapter_suitability_min": 4, "chapter_suitability_max": 30, "fingerprint_usage": "question-hook endings, false-reassurance beat, reveal ladder", "originality_notes": "new setting, cast, mystery and climax mechanism",
    }


def _architecture(chapter_count: int, *, bad: bool = False) -> Dict[str, Any]:
    chars = [
        {"name": "Nadia Quill", "aliases": ["Nadia"], "role": "Protagonist", "identity": "harbor clerk", "goal": "protect the manifest", "motivation": "duty", "fear": "being wrong", "flaw": "trusts no one", "wound": "a lost brother", "secret": "", "knowledge_boundaries": ["Corvin Ashe is the courier"], "voice_sentence_tendency": "short", "voice_tells": ["counts things"], "forbidden_speech": ["I beg you"], "capabilities": ["notices details"], "limitations": ["no authority"], "possessions": ["brass token"], "appearance": "tall", "home_location": "Harrow Quay", "arc": [{"phase": "start", "state": "notices but does not act", "chapter_hint": 1}, {"phase": "mid", "state": "acts and is punished", "chapter_hint": max(2, chapter_count // 2)}, {"phase": "end", "state": "acts and is believed", "chapter_hint": chapter_count}], "introduction_chapter": 1},
        {"name": "Teo Marsh", "aliases": ["Teo"], "role": "Deuteragonist", "identity": "dock hand", "goal": "keep his job", "motivation": "family", "fear": "the courier", "flaw": "evasive", "wound": "", "secret": "suspects Corvin", "knowledge_boundaries": [], "voice_sentence_tendency": "medium", "voice_tells": [], "forbidden_speech": [], "capabilities": [], "limitations": [], "possessions": [], "appearance": "", "home_location": "Kestrel Row", "arc": [{"phase": "start", "state": "evasive", "chapter_hint": 1}, {"phase": "end", "state": "honest", "chapter_hint": chapter_count}], "introduction_chapter": 1},
        {"name": "Corvin Ashe", "aliases": ["Corvin"], "role": "Antagonist", "identity": "harbor master", "goal": "keep forging", "motivation": "debt", "fear": "exposure", "flaw": "pride", "wound": "", "secret": "is the courier", "knowledge_boundaries": [], "voice_sentence_tendency": "long", "voice_tells": [], "forbidden_speech": [], "capabilities": ["authority"], "limitations": [], "possessions": [], "appearance": "", "home_location": "Harrow Quay", "arc": [{"phase": "start", "state": "untouchable", "chapter_hint": 1}, {"phase": "end", "state": "exposed", "chapter_hint": chapter_count}], "introduction_chapter": 1},
        {"name": "Petra Vale", "aliases": ["Petra"], "role": "Supporting Character", "identity": "baker", "goal": "feed people", "motivation": "kindness", "fear": "", "flaw": "", "wound": "", "secret": "", "knowledge_boundaries": [], "voice_sentence_tendency": "warm", "voice_tells": [], "forbidden_speech": [], "capabilities": [], "limitations": [], "possessions": [], "appearance": "", "home_location": "Kestrel Row", "arc": [], "introduction_chapter": 1},
    ]
    reveal = chapter_count if not bad else chapter_count + 3  # bad: reveal after the last chapter
    return {
        "architecture_thinking": "expanded", "contract": {"premise": "A harbor clerk hunts the courier who forges the tide manifests.", "genre_promise": "quiet mystery", "target_audience": "adult", "pov": "first person, single narrator", "tense": "past", "tone": "restrained", "thematic_question": "Is noticing the same as knowing?", "ending_contract": "the courier is named", "prohibited_deviations": ["melodrama"], "primary_fantasy": "quiet competence", "primary_emotional_reward": "reinterpretation", "expected_protagonist_behavior": ["notices details", "never begs"], "violations": ["melodrama"]},
        "characters": chars,
        "locations": [{"name": l, "description": "original place", "function_in_story": "setting"} for l in LOCATIONS],
        "factions": [{"name": "the Quay Wardens", "description": "harbor police", "goal": "order"}], "items": [{"name": "brass token", "description": "fits a lock", "owner": "Nadia Quill", "significance": "opens the Archive cellar"}],
        "world_rules": [{"rule": "The manifest records lies as blank pages.", "domain": "magic", "cost": "", "limits": "", "known_by": ["Nadia Quill"]}],
        "knowledge_facts": [{"fact": "Corvin Ashe is the courier who forges the manifests", "is_true": True, "knowers_at_start": ["Corvin Ashe"], "reader_reveal_chapter": reveal, "sensitivity": "high"}],
        "relationships": [{"character_a": "Nadia Quill", "character_b": "Teo Marsh", "trust": 30, "affection": 40, "hostility": 0, "power_balance": "equal", "private_relationship": "wary allies", "unresolved_tension": "Teo knows more than he says", "arc_summary": "to trust"}],
        "plot_threads": [{"name": "Who forges the manifests", "thread_type": "main_plot", "central_question": "Who is the courier?", "participants": ["Nadia Quill", "Corvin Ashe"], "opening_chapter": 1, "resolution_chapter": chapter_count}, {"name": "Teo's silence", "thread_type": "subplot", "central_question": "What has Teo not said?", "participants": ["Nadia Quill", "Teo Marsh"], "opening_chapter": 1, "resolution_chapter": max(2, chapter_count - 1)}],
        "setups_payoffs": [{"setup": "The brass token fits a lock", "setup_chapter": 1, "payoff": "the token opens the Archive cellar", "payoff_chapter": max(2, chapter_count - 1), "promise_type": "chekhovs_gun"}, {"setup": "A chalk mark on the hatch", "setup_chapter": 1, "payoff": "the chalk is the courier's signal", "payoff_chapter": chapter_count, "promise_type": "clue"}],
        "timeline": [{"title": "Nadia begins the watch", "story_time": "Day 1", "chapter": 1, "participants": ["Nadia Quill"], "location": "Harrow Quay", "summary": "start"}],
        "act_plan": ["setup: chapters 1-2", "escalation: middle", "climax and resolution: last chapters"],
    }


def _blueprint(n: int, total: int) -> Dict[str, Any]:
    last = n == total
    beats = [
        {"function": "quiet_scene_opening", "description": "Nadia counts the rivets on the hatch at dawn", "keywords": ["rivets", "hatch"]},
        {"function": "dialogue_heavy_scene", "description": "A confrontation about the Archive; Nadia deflects", "keywords": ["Archive", "places"]},
        {"function": "false_reassurance", "description": "Teo says the quay is clear and Nadia lets him", "keywords": ["quay", "clear"]},
        {"function": "threat_escalation", "description": "Something on the quay goes wrong and Nadia goes out regardless", "keywords": ["went", "regardless"]},
        {"function": "reveal" if last else "chapter_cliffhanger", "description": "Nadia names Corvin as the courier" if last else "Nadia refuses to go below; ends on a question", "keywords": ["courier", "Corvin"] if last else ["below", "Not yet"]},
    ]
    return {
        "chapter_number": n, "title": f"Rivets {n}", "purpose": "advance the manifest mystery", "pov": "Nadia Quill", "location": "Harrow Quay", "story_time": f"Day {n}", "opening_state": "dawn watch", "goal": "protect the manifest", "conflict": "the quay is not safe",
        "participants": ["Nadia Quill", "Teo Marsh", "Corvin Ashe", "Petra Vale"], "beats": beats, "reveals": ["Corvin Ashe is the courier who forges the manifests"] if last else [], "setups": ["A chalk mark on the hatch"] if n == 1 else [], "payoffs": ["the token opens the Archive cellar"] if n == max(2, total - 1) else (["the chalk is the courier's signal"] if last else []),
        "character_state_transition": "Nadia grows wary", "relationship_transition": "Teo evasive", "knowledge_transition": "Nadia learns the courier's identity" if last else "nothing decisive", "target_tension": min(10, 4 + n), "emotional_movement": "calm -> dread", "closing_hook": "a question",
        "allowed_outcomes": ["Nadia refuses to go below"] + (["Corvin Ashe is revealed as the courier"] if last else []), "forbidden_outcomes": [] if last else ["Corvin is revealed as the courier"],
        "overview": f"Chapter {n}: Nadia Quill begins another watch on Harrow Quay. A confrontation about the Salt Archive, a false calm, an escalation on the quay and a refusal to go below. " + ("She finally names Corvin Ashe as the courier." if last else "Nothing about the courier's identity is revealed."),
    }


def _chapter_prose(n: int, total: int, *, leak: bool = False) -> str:
    other = "Teo" if n % 3 else "Corvin"
    last = n == total
    paras = [
        ["The tide bell rang once.", "Rope and rust on Kestrel Row.", "Teo was already at the rail when I came up.", "Nobody on Harrow Quay locks a hatch they mean to open."][n % 4],
        "I counted the rivets on the hatch. Eleven. Same as yesterday.",
        f"{other} did not look round. \"You were at the Archive.\"", "\"I am at a lot of places.\"", "Quiet, for a breath.",
        f"Page {n} of the manifest stayed blank. I wrote the date.", "Teo said the quay was clear. I let him say it.", "I checked the locker. I checked the hatch. I checked the locker again.",
    ]
    if leak:
        paras.append("Marit Solen was waiting by the Greywater Exchange with the copper key.")
    paras += [
        "Petra came by with bread and no news. \"Clear night,\" she said. I nodded.", "I went out regardless.",
        ["Then the Archive lamp died in its window. No wind that night.", "Then a chalk mark appeared on our hatch. Fresh. Still damp.", "Then Petra did not come back from the Row.", "Then somebody raised the harbor chain, and nobody had rung for it."][n % 4],
        "I kept walking. Running admits something.", f"Harrow Quay had gone dark before I turned the corner. {'Corvin' if other == 'Teo' else 'Teo'} stood under the last lamp.",
    ]
    if last:
        paras += ["\"It was you,\" I said. \"You are the courier, Corvin.\"", "Corvin set the chalk down and did not deny it.", "The manifest, for once, was not blank."]
    else:
        paras += ["\"Go below, Nadia.\"", "\"Not yet.\"", ["Who had known I would come?", "If Corvin was not the courier, why the chalk?", "What had Teo not said?", "How long had the chain been up?"][n % 4]]
    claims = {"claims": [], "summary": f"Nadia is confronted by {other}; the quay escalates" + ("; she names Corvin as the courier." if last else "; ends on a question."), "ending_location": "Harrow Quay", "current_time": "night", "unresolved_immediate_action": "" if last else "Nadia refuses to go below", "open_dialogue_obligation": ""}
    return "\n\n".join(paras) + "\n<claims>" + json.dumps(claims) + "</claims>"


class FakeClient:
    """Deterministic model for every role; records calls and injects planned faults once each."""

    def __init__(self):
        self.calls: List[Dict[str, Any]] = []
        self.faults = {"storyline_malformed": True, "architecture_bad": True, "draft_leak": True}
        self.chapter_total = CHAPTERS

    async def structured(self, *, role: str, schema: Type[Any], system_prompt: str, user_prompt: str, prompt_version: str, stage: str = ""):
        self.calls.append({"role": role, "schema": schema.__name__, "stage": stage, "prompt_version": prompt_version})
        name = schema.__name__
        if name == "ChapterAnalysis":
            n = int(user_prompt.split("Book-wide chapter number: ")[1].split("\n")[0].strip())
            data = syn.fake_chapter_analysis(n)
            data["scenes"] = [{**s, "index": i + 1} for i, s in enumerate(data["scenes"])]
            return schema.model_validate(data)
        if name == "LocalArcPlan":
            return schema.model_validate({"arcs": [{"name": "Watch", "chapter_start": 1, "chapter_end": 12, "confidence": 0.7, "summary": "first half", "open_at_end": False}, {"name": "Chain", "chapter_start": 13, "chapter_end": 24, "confidence": 0.7, "summary": "second half", "open_at_end": False}]})
        if name == "StoryStructureMap":
            return schema.model_validate({"reconciliation_thinking": "two stages", "stages": [{"stage_number": 1, "name": "Watch", "chapter_start": 1, "chapter_end": 12, "confidence": 0.8}, {"stage_number": 2, "name": "Chain", "chapter_start": 13, "chapter_end": 24, "confidence": 0.8}], "volume_hints": []})
        if name == "EntityResolutionPlan":
            ents = [{"canonical": n, "entity_type": "character", "aliases": [n.split()[0]], "confidence": 0.9} for n in syn.SOURCE_CHARACTERS] + [{"canonical": l, "entity_type": "scene", "aliases": [], "confidence": 0.9} for l in syn.SOURCE_LOCATIONS] + [{"canonical": "copper key", "entity_type": "item", "aliases": [], "confidence": 0.9}]
            return schema.model_validate({"entities": ents})
        if name == "NarrativeArchitecture":
            return schema.model_validate({"plot_threads": [{"name": "Who is the courier", "thread_type": "main_plot", "central_question": "Who forges the ledger?", "participants": ["Ilse Varn", "Brann Hale"], "opening_chapter": 1, "status": "resolved"}], "promises": [{"setup": "copper key", "promise_type": "chekhovs_gun", "source_chapter": 2, "planned_payoff": "opens the vault", "status": "paid_off", "payoff_chapter": 14}], "knowledge_facts": [{"fact": "Brann Hale is the courier", "reader_state": "unaware", "planned_reveal_chapter": 18, "knowers": []}], "timeline_events": [{"title": "Ilse begins", "chapter_number": 1}], "relationship_arcs": [{"character_a": "Ilse Varn", "character_b": "Marit Solen", "trust": 30, "affection": 40}]})
        if name == "NarrativeGenome":
            return schema.model_validate(syn.fake_genome())
        if name == "StorylineOptionSet":
            if self.faults["storyline_malformed"]:
                self.faults["storyline_malformed"] = False
                raise ValueError("structured output invalid: 1 validation error for StorylineOptionSet")
            opts = [_storyline(0, leak=True), _storyline(1), _storyline(2), _storyline(3), _storyline(4), _storyline(5), _storyline(6, clone_of=5), _storyline(7)]
            return schema.model_validate({"ideation_thinking": "varied settings, roles, conflicts", "options": opts})
        if name == "NovelArchitecture":
            bad = self.faults["architecture_bad"]
            self.faults["architecture_bad"] = False
            return schema.model_validate(_architecture(self.chapter_total, bad=bad))
        if name == "ChapterBlueprintBatch":
            nums = [int(x) for x in user_prompt.split("Plan chapters ")[1].split(" of ")[0].split("-")]
            lo, hi = nums[0], nums[-1]
            return schema.model_validate({"planning_thinking": "windowed", "chapters": [_blueprint(n, self.chapter_total) for n in range(lo, hi + 1)]})
        raise AssertionError(f"unexpected schema {name}")

    async def text(self, *, role: str, system_prompt: str, user_prompt: str, prompt_version: str, stage: str = "") -> str:
        self.calls.append({"role": role, "schema": "text", "stage": stage, "prompt_version": prompt_version})
        n = int(stage.split(":ch")[-1])
        if role == "repair_editor" or role == "whole_novel_editor":
            return _chapter_prose(n, self.chapter_total)
        leak = self.faults["draft_leak"]
        self.faults["draft_leak"] = False
        return _chapter_prose(n, self.chapter_total, leak=leak)


@pytest.fixture(scope="module")
def client(app_client):
    return app_client


@pytest.fixture(scope="module")
def fake():
    return FakeClient()


@pytest.fixture(scope="module")
def state() -> Dict[str, Any]:
    return {}


def _runner(session: Session, job_id: int, fake: FakeClient):
    from app.services.autonomous.runner import JobRunner

    return JobRunner(session, job_id, client_factory=lambda s, j, r: fake, owner="test-runner")


# --------------------------------------------------------------------- tests
def test_01_create_job_is_idempotent_and_persisted(client, state):
    from app.db.session import engine
    from app.services.autonomous import runner as runner_mod

    epub = build_source_epub()
    state["epub"] = epub
    with Session(engine) as s:
        from app.db.models import LLMConfig

        cfg = LLMConfig(provider="authnd", model_name="moonshotai/kimi-k3", api_key="", display_name="Kimi K3 test")
        s.add(cfg)
        s.commit()
        s.refresh(cfg)
        state["llm_config_id"] = cfg.id
        job = runner_mod.create_job(s, filename="ledger.epub", data=epub, llm_config_id=cfg.id, options={"genre": "mystery"})
        again = runner_mod.create_job(s, filename="ledger.epub", data=epub, llm_config_id=cfg.id, options={"genre": "mystery"})
        assert job.id == again.id and job.stage == "INGEST" and job.status == "queued"
        state["job_id"] = job.id


def test_02_upload_to_storyline_options(fake, state):
    from app.db.models import StorylineCandidate
    from app.db.session import engine

    with Session(engine) as s:
        job = asyncio.run(_runner(s, state["job_id"], fake).run())
        assert job.status == "waiting_for_user" and job.stage == "STORYLINE_SELECTION", (job.status, job.stage, job.error, job.progress_message)
        res = job.stage_results
        assert res["INGEST"]["quality"]["ok"] and res["INGEST"]["quality"]["detected_chapter_count"] == syn.CHAPTER_COUNT
        assert res["INGEST"]["quality"]["story_content_fraction"] > 0.9
        assert res["SOURCE_ANALYSIS"]["status"]["analysed"] == syn.CHAPTER_COUNT
        assert res["ANALYSIS_VERIFICATION"]["status"]["completeness"] == 1.0
        assert res["BOOK_STRUCTURE"]["stages"] == 2 and res["BOOK_STRUCTURE"]["genome_patterns"] == 3
        assert res["FINGERPRINT_BUILD"]["layers"] == 20
        assert res["EXAMPLE_LIBRARY_BUILD"]["examples"] > 0
        gen = res["STORYLINE_GENERATION"]
        assert gen["accepted"] >= 5 and gen["rejected"] >= 2
        rows = s.exec(select(StorylineCandidate).where(StorylineCandidate.job_id == job.id)).all()
        rejected = {r.title: r.rejection_reason for r in rows if r.rejected}
        assert any("source overlap" in (reason or "") for reason in rejected.values()), rejected  # Ilse Varn / Greywater leak
        assert any("too similar" in (reason or "") for reason in rejected.values()), rejected  # the clone
        for r in rows:
            if not r.rejected:
                assert r.originality_score >= 0.9 and r.recommended_chapters_min >= 3 and r.similarity_to_others
        # The malformed first batch was retried and recorded as a warning, not a failure.
        assert any(w.get("category") == "malformed_output" for w in job.warnings), job.warnings
        state["storyline_id"] = next(r.id for r in rows if not r.rejected)
        # The failed attempt is on the audit trail with its category and recovery action.
        from app.db.models import JobStageAttempt

        attempts = s.exec(select(JobStageAttempt).where(JobStageAttempt.job_id == job.id, JobStageAttempt.stage == "STORYLINE_GENERATION")).all()
        assert [a.status for a in attempts] == ["failed", "succeeded"]
        assert attempts[0].failure_category == "malformed_output" and attempts[0].recovery_action == "retry"


def test_03_select_storyline_then_architecture_and_plan(fake, state):
    from app.db.models import AutonomousNovelJob
    from app.db.session import engine
    from app.services.autonomous import runner as runner_mod

    with Session(engine) as s:
        job = s.get(AutonomousNovelJob, state["job_id"])
        with pytest.raises(ValueError):
            runner_mod.select_storyline(s, job, storyline_id=999999, chapter_count=CHAPTERS)
        job = runner_mod.select_storyline(s, job, storyline_id=state["storyline_id"], chapter_count=CHAPTERS, options={"words_per_chapter": 260})
        assert job.status == "queued" and job.chapter_count == CHAPTERS
        job = asyncio.run(_runner(s, job.id, fake).run(until_stage="CHAPTER_GENERATION_LOOP"))
        assert job.stage == "CHAPTER_GENERATION_LOOP", (job.stage, job.status, job.error)
        arch = job.stage_results["NOVEL_ARCHITECTURE"]
        assert arch["rounds"] == 2  # first architecture failed validation (reveal after the last chapter) and was repaired
        assert [a["chapters"] for a in arch["allocation"]] and sum(a["chapters"] for a in arch["allocation"]) == CHAPTERS
        assert job.stage_results["BIBLE_BUILD"]["facts_seeded"] >= 5 and job.stage_results["BIBLE_BUILD"]["isolation"]["isolated"]
        assert job.stage_results["CHAPTER_PLAN_BUILD"]["outline_count"] == CHAPTERS
        assert job.stage_results["NOVEL_PREFLIGHT"]["ok"] and job.stage_results["NOVEL_PREFLIGHT"]["next_chapter"] == 1
        state["original_pid"] = job.original_project_id
        from app.services.forge import transfer

        assert transfer.isolation_report(s, job.original_project_id)["isolated"]


def test_04_chapter_loop_interrupt_and_resume_without_duplicates(fake, state):
    from app.db.models import AutonomousNovelJob, ChapterPipelineRun
    from app.db.session import engine
    from app.services.autonomous import runner as runner_mod
    from app.services.forge import provenance

    with Session(engine) as s:
        runner = _runner(s, state["job_id"], fake)
        job = asyncio.run(runner.step())
        job = asyncio.run(runner.step())
        assert job.chapters_committed == 2 and job.stage == "CHAPTER_GENERATION_LOOP"
        ch1 = job.stage_results["CHAPTER_GENERATION_LOOP"]["1"]
        assert ch1["repair_attempts"] >= 1  # the leaked source name was caught and repaired in-run
        # Simulate a crash: the process dies while the job row says running and its lease is stale.
        job.status = "running"
        job.lease_owner = "dead-process"
        job.lease_expires_at = datetime.now() - timedelta(seconds=1)
        s.add(job)
        s.commit()
    with Session(engine) as s:
        assert runner_mod.recover_stale_leases(s) == 1
        job = s.get(AutonomousNovelJob, state["job_id"])
        assert job.status == "queued" and job.stage == "CHAPTER_GENERATION_LOOP" and job.chapters_committed == 2
        job = asyncio.run(_runner(s, job.id, fake).run())
        assert job.status == "completed" and job.stage == "DONE", (job.status, job.stage, job.error, job.progress_message)
        assert job.progress_percent == 100.0
        manifest = provenance.get_manifest(s, state["original_pid"])
        assert manifest.latest_committed_chapter == CHAPTERS
        committed = s.exec(select(ChapterPipelineRun).where(ChapterPipelineRun.project_id == state["original_pid"], ChapterPipelineRun.status == "committed")).all()
        assert sorted({r.chapter_number for r in committed}) == list(range(1, CHAPTERS + 1))
        assert len({r.chapter_number for r in committed}) == len(committed)  # exactly one committed run per chapter
        from app.services.autonomous.audit import chapter_texts

        texts = chapter_texts(s, state["original_pid"])
        assert [n for n, _, _ in texts] == list(range(1, CHAPTERS + 1))
        for _, _, text in texts:
            for name in ("Ilse", "Marit", "Brann", "Greywater", "Tessaly", "Lantern Ward"):
                assert name not in text


def test_05_audit_export_and_reports(client, fake, state):
    from app.db.models import AutonomousNovelJob, ExportArtifact
    from app.db.session import engine

    with Session(engine) as s:
        job = s.get(AutonomousNovelJob, state["job_id"])
        audit = job.stage_results["WHOLE_NOVEL_AUDIT"]
        assert audit["chapters"] == CHAPTERS and audit["originality"]["passed"]
        assert not [f for f in audit["findings"] if f["kind"].startswith("source_")]
        exp = job.stage_results["EXPORT"]
        kinds = {a["kind"] for a in exp["artifacts"]}
        assert {"epub", "docx", "markdown", "text", "report", "synopsis", "character_guide"} <= kinds
        epub = s.exec(select(ExportArtifact).where(ExportArtifact.job_id == job.id, ExportArtifact.kind == "epub")).first()
        with zipfile.ZipFile(io.BytesIO(epub.data)) as zf:
            names = zf.namelist()
            assert names[0] == "mimetype" and zf.read("mimetype") == b"application/epub+zip"
            opf = zf.read("OEBPS/content.opf").decode()
            assert 'version="3.0"' in opf and opf.count("<itemref") == CHAPTERS + 1 and "<dc:title>" in opf
            nav = zf.read("OEBPS/nav.xhtml").decode()
            assert nav.count("<li>") == CHAPTERS + 1
            assert "OEBPS/toc.ncx" in names and "OEBPS/title.xhtml" in names
        docx = s.exec(select(ExportArtifact).where(ExportArtifact.job_id == job.id, ExportArtifact.kind == "docx")).first()
        with zipfile.ZipFile(io.BytesIO(docx.data)) as zf:
            doc = zf.read("word/document.xml").decode()
            assert doc.count('w:val="Heading1"') == CHAPTERS and "[Content_Types].xml" in zf.namelist()
    # API surface
    r = client.get(f"/api/autonomous/jobs/{state['job_id']}")
    assert r.status_code == 200 and r.json()["job"]["status"] == "completed"
    r = client.get(f"/api/autonomous/jobs/{state['job_id']}/storylines", params={"include_rejected": True})
    assert r.status_code == 200 and len(r.json()) == 8 and sum(1 for o in r.json() if o["rejected"]) >= 2
    r = client.get(f"/api/autonomous/jobs/{state['job_id']}/artifacts")
    assert r.status_code == 200 and len(r.json()) == 7
    art = next(a for a in r.json() if a["kind"] == "epub")
    r = client.get(f"/api/autonomous/artifacts/{art['id']}/download")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/epub+zip") and r.content[:2] == b"PK"
    r = client.get(f"/api/autonomous/jobs/{state['job_id']}/report")
    assert r.status_code == 200 and r.json()["audit"]["chapters"] == CHAPTERS and r.json()["ingestion"]["ok"]
    r = client.get(f"/api/autonomous/jobs/{state['job_id']}/chapters")
    assert r.status_code == 200 and len(r.json()) == CHAPTERS and all(c["sync_status"] == "synchronized" for c in r.json())
    # The only human inputs were the storyline and the chapter count.
    r = client.get(f"/api/autonomous/jobs/{state['job_id']}")
    attempts = r.json()["job"]["attempts"]
    assert not any(a["failure_category"] == "user_input_required" for a in attempts)


def test_06_api_create_and_select_validation(client, state):
    r = client.post("/api/autonomous/jobs", json={"filename": "x.epub", "content_base64": "!!!", "llm_config_id": state["llm_config_id"]})
    assert r.status_code == 400
    r = client.post("/api/autonomous/jobs", json={"filename": "x.epub", "content_base64": base64.b64encode(b"x").decode(), "llm_config_id": 999999})
    assert r.status_code == 400
    r = client.post(f"/api/autonomous/jobs/{state['job_id']}/select", json={"storyline_id": state["storyline_id"], "chapter_count": 5})
    assert r.status_code == 409  # already past selection


def test_07_failure_ladder_is_deterministic():
    from app.services.autonomous import failures as fail

    p = fail.POLICIES[fail.MALFORMED_OUTPUT]
    assert [p.action_for(i) for i in range(1, 6)] == [fail.RETRY, fail.RETRY_CLARIFIED, fail.REDUCE_SCOPE, fail.FALLBACK_MODEL, fail.PAUSE]
    assert fail.POLICIES[fail.BUDGET_EXCEEDED].action_for(1) == fail.PAUSE
    assert fail.classify_exception(ValueError("structured output invalid: x")) == fail.MALFORMED_OUTPUT
    assert fail.classify_exception(TimeoutError("timed out")) == fail.PROVIDER_FAILURE
    assert fail.classify_exception(RuntimeError("context length exceeded")) == fail.TOKEN_OVERFLOW


def test_08_chapter_allocation_and_fit():
    from app.services.autonomous.architecture import allocate_chapters, fit_report

    for n in (3, 5, 9, 12, 40, 120):
        alloc = allocate_chapters(n)
        assert sum(a["chapters"] for a in alloc) == n
        assert alloc[0]["chapter_start"] == 1 and alloc[-1]["chapter_end"] == n
        for a, b in zip(alloc, alloc[1:]):
            assert b["chapter_start"] == a["chapter_end"] + 1
    assert len(allocate_chapters(120)) == 9
    fit = fit_report({"chapter_suitability_min": 20, "chapter_suitability_max": 40}, 8)
    assert fit["severity"] == "warning" and "reduce cast size" in fit["adaptations"]
    assert fit_report({"chapter_suitability_min": 10, "chapter_suitability_max": 40}, 20)["severity"] == "none"


def test_09_storyline_gates_are_deterministic():
    from app.services.autonomous.storylines import gate_options, pairwise_similarity

    a, b, c = _storyline(1), _storyline(2), _storyline(6, clone_of=5)
    d = _storyline(5)
    assert pairwise_similarity(a, b) < 0.45
    assert pairwise_similarity(c, d) > 0.45
    gated, matrix = gate_options([a, b, d, c], None)
    assert [g["_rejected"] for g in gated] == [False, False, False, True]
    assert matrix[2][3] == pairwise_similarity(d, c)
