"""Alembic chain safety on SQLite: fresh upgrade, every-revision upgrade, populated/duplicate upgrades, idempotency, app startup.

Each test uses its own database file and engine (never the shared test
database). PostgreSQL is not exercised: the application configures SQLite only
(``DatabaseSettings.get_database_url``).
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from typing import List

import pytest
import sqlalchemy as sa
from sqlalchemy import inspect, text

from alembic import command

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.db import migrations  # noqa: E402

PRE_FENCING_REV = "0003_autonomous"

JOB_SQL = "INSERT INTO autonomousnoveljob (id, idempotency_key, status, stage, mode, llm_config_id, role_llm_config_ids, source_filename, source_file_hash, source_bytes, options, chapter_count, chapters_committed, progress_percent, progress_message, stage_results, warnings, error, model_calls, input_tokens, output_tokens, lease_owner, lease_expires_at, heartbeat_at, created_at, updated_at, started_at, finished_at) VALUES (:id, :key, :status, :stage, 'fully_automatic', 1, '{}', 'x.epub', 'h', NULL, '{}', 0, 0, 0, '', :sr, '[]', NULL, 0, 0, 0, :owner, NULL, NULL, :n, :n, NULL, NULL)"
ART_SQL = "INSERT INTO exportartifact (job_id, project_id, kind, filename, media_type, size_bytes, content_hash, data, created_at) VALUES (1, 1, :kind, :fn, 'x', :size, :h, :data, :n)"
STORY_SQL = "INSERT INTO storylinecandidate (job_id, source_project_id, option_index, title, content, originality_score, originality_report, similarity_to_others, recommended_chapters_min, recommended_chapters_max, rejected, rejection_reason, selected, created_at) VALUES (1, 1, :idx, :title, '{}', :score, '{}', '{}', 1, 2, :rej, NULL, :sel, :n)"


def _engine(tmp_path, name: str):
    return sa.create_engine(f"sqlite:///{tmp_path / name}")


def _upgrade(engine, target: str = "head") -> None:
    command.upgrade(migrations.alembic_config(engine), target)


def _revisions() -> List[str]:
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(migrations.alembic_config())
    return [r.revision for r in reversed(list(script.walk_revisions()))]


def _seed_pre_fencing(engine, *, duplicates: bool) -> None:
    """Populate a 0003 database with jobs, artifacts and storylines (optionally duplicated) plus nulls."""
    now = datetime(2026, 1, 1).isoformat(sep=" ")
    later = datetime(2026, 1, 2).isoformat(sep=" ")
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO llmconfig (provider, model_name, api_key) VALUES ('openai_compatible', 'm', 'k')"))
        conn.execute(text("INSERT INTO project (name, description) VALUES ('p', NULL)"))
        for jid, status, stage in ((1, "completed", "DONE"), (2, "running", "CHAPTER_GENERATION_LOOP"), (3, "paused", "SOURCE_ANALYSIS")):
            conn.execute(text(JOB_SQL), {"id": jid, "key": f"k{jid}", "status": status, "stage": stage, "sr": "{}" if jid != 3 else "null", "owner": "dead" if jid == 2 else None, "n": now})
        conn.execute(text(ART_SQL), {"kind": "epub", "fn": "old.epub", "size": 10, "h": "h-old", "data": b"\x00", "n": now})
        conn.execute(text(ART_SQL), {"kind": "docx", "fn": "a.docx", "size": 5, "h": "h-docx", "data": b"\x00", "n": now})
        conn.execute(text(STORY_SQL), {"idx": 0, "title": "a", "score": 0.5, "rej": 0, "sel": 0, "n": now})
        conn.execute(text(STORY_SQL), {"idx": 1, "title": "b", "score": 0.6, "rej": 1, "sel": 0, "n": now})
        if duplicates:
            conn.execute(text(ART_SQL), {"kind": "epub", "fn": "empty.epub", "size": 0, "h": "h-empty", "data": b"", "n": later})
            conn.execute(text(ART_SQL), {"kind": "epub", "fn": "new.epub", "size": 20, "h": "h-new", "data": b"\x00\x00", "n": later})
            conn.execute(text(STORY_SQL), {"idx": 0, "title": "a-selected", "score": 0.4, "rej": 0, "sel": 1, "n": later})
            conn.execute(text(STORY_SQL), {"idx": 0, "title": "a-best", "score": 0.9, "rej": 0, "sel": 0, "n": later})


def test_fresh_upgrade_to_head_and_no_drift(tmp_path):
    engine = _engine(tmp_path, "fresh.db")
    info = migrations.upgrade_database(engine)
    assert info["after"] == migrations.head_revision() and not info["legacy"]
    assert migrations.check_schema_drift(engine) == []
    names = set(inspect(engine).get_table_names())
    assert {"autonomousnoveljob", "budgetreservation", "modelinvocationattempt", "recoveryaction", "exportartifact", "storylinecandidate"} <= names


@pytest.mark.parametrize("start", _revisions())
def test_upgrade_from_every_revision_to_head(tmp_path, start):
    engine = _engine(tmp_path, f"from-{start}.db")
    _upgrade(engine, start)
    assert migrations.current_revision(engine) == start
    _upgrade(engine, "head")
    assert migrations.current_revision(engine) == migrations.head_revision()
    assert migrations.check_schema_drift(engine) == []


def test_populated_pre_fencing_upgrade_with_nulls_and_partial_jobs(tmp_path):
    engine = _engine(tmp_path, "populated.db")
    _upgrade(engine, PRE_FENCING_REV)
    _seed_pre_fencing(engine, duplicates=False)
    _upgrade(engine, "head")
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT id, status, lease_generation, reserved_calls, reserved_input_tokens, cost_usd, cost_unknown_calls FROM autonomousnoveljob ORDER BY id")).fetchall()
    assert [r[0] for r in rows] == [1, 2, 3]
    assert all(r[2] == 0 and r[3] == 0 and r[4] == 0 and r[5] == 0 and r[6] == 0 for r in rows)
    assert migrations.check_schema_drift(engine) == []


def test_duplicate_export_artifacts_and_storylines_survive_uniqueness(tmp_path):
    engine = _engine(tmp_path, "dupes.db")
    _upgrade(engine, PRE_FENCING_REV)
    _seed_pre_fencing(engine, duplicates=True)
    _upgrade(engine, "head")
    with engine.connect() as conn:
        arts = conn.execute(text("SELECT kind, filename FROM exportartifact WHERE job_id = 1 ORDER BY kind")).fetchall()
        stories = conn.execute(text("SELECT title, option_index, selected, rejected FROM storylinecandidate WHERE job_id = 1 ORDER BY option_index, title")).fetchall()
    uniques = {u["name"] for u in inspect(engine).get_unique_constraints("exportartifact")} | {u["name"] for u in inspect(engine).get_unique_constraints("storylinecandidate")}
    assert [tuple(a) for a in arts] == [("docx", "a.docx"), ("epub", "new.epub")]  # newest non-empty survives
    assert len(stories) == 4 and [tuple(s) for s in stories if s[1] == 0] == [("a-selected", 0, 1, 0)]  # selected keeps its index; nothing deleted
    assert len({s[1] for s in stories}) == 4
    assert {"uq_export_job_kind", "uq_storyline_job_option"} <= uniques
    assert migrations.check_schema_drift(engine) == []


def test_half_applied_fencing_migration_recovers(tmp_path):
    """A database where the original 0004 failed mid-way (tables created, temp table left, version still 0003) upgrades cleanly."""
    engine = _engine(tmp_path, "half.db")
    _upgrade(engine, PRE_FENCING_REV)
    _seed_pre_fencing(engine, duplicates=True)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE modelinvocationattempt (id INTEGER PRIMARY KEY, invocation_id INTEGER, job_id INTEGER, attempt INTEGER NOT NULL, provider VARCHAR NOT NULL, model_name VARCHAR NOT NULL, llm_config_id INTEGER, fallback BOOLEAN NOT NULL, role VARCHAR NOT NULL, stage VARCHAR NOT NULL, started_at DATETIME NOT NULL, completed_at DATETIME, latency_ms INTEGER NOT NULL, status VARCHAR NOT NULL, error_category VARCHAR, provider_status VARCHAR, provider_request_id VARCHAR, retry_after_seconds FLOAT, input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL, timeout_seconds FLOAT, response_hash VARCHAR NOT NULL, diagnostic VARCHAR)"))
        conn.execute(text("ALTER TABLE autonomousnoveljob ADD COLUMN lease_generation INTEGER NOT NULL DEFAULT '0'"))
        conn.execute(text("CREATE TABLE _alembic_tmp_exportartifact (id INTEGER PRIMARY KEY)"))
    _upgrade(engine, "head")
    assert migrations.current_revision(engine) == migrations.head_revision()
    assert not [t for t in inspect(engine).get_table_names() if t.startswith("_alembic_tmp_")]
    assert migrations.check_schema_drift(engine) == []


def test_upgrade_is_idempotent(tmp_path):
    engine = _engine(tmp_path, "idem.db")
    _upgrade(engine, PRE_FENCING_REV)
    _seed_pre_fencing(engine, duplicates=True)
    first = migrations.upgrade_database(engine)
    second = migrations.upgrade_database(engine)
    assert first["after"] == second["after"] == migrations.head_revision()
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM exportartifact")).scalar() == 2
        assert conn.execute(text("SELECT COUNT(*) FROM storylinecandidate")).scalar() == 4


def test_legacy_pre_alembic_database_is_adopted(tmp_path):
    from sqlmodel import SQLModel

    engine = _engine(tmp_path, "legacy.db")
    baseline = [t for name, t in SQLModel.metadata.tables.items() if name in migrations.BASELINE_TABLES]
    SQLModel.metadata.create_all(engine, tables=baseline)
    info = migrations.upgrade_database(engine)
    assert info["legacy"] and info["after"] == migrations.head_revision()
    assert migrations.check_schema_drift(engine) == []


def test_application_services_work_against_upgraded_database(tmp_path):
    """The application layer (ORM models, budget snapshot, job serialisation, startup recovery) runs over the upgraded populated file."""
    engine = _engine(tmp_path, "startup.db")
    _upgrade(engine, PRE_FENCING_REV)
    _seed_pre_fencing(engine, duplicates=True)
    _upgrade(engine, "head")
    from sqlmodel import Session, select

    from app.db.models import AutonomousNovelJob, ExportArtifact
    from app.services.autonomous import budget
    from app.services.autonomous import runner as runner_mod

    with Session(engine) as s:
        jobs = s.exec(select(AutonomousNovelJob).order_by(AutonomousNovelJob.id)).all()
        assert [j.id for j in jobs] == [1, 2, 3]
        assert budget.usage_snapshot(s, jobs[0])["cost_usd"]["known"] is None
        assert runner_mod.job_dict(s, jobs[0])["budget"]["calls"]["used"] == 0
        assert runner_mod.recover_stale_leases(s) == 1  # the dead 'running' job is requeued
        assert s.exec(select(ExportArtifact).where(ExportArtifact.job_id == 1)).all()
