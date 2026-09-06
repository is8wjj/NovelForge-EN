"""Alembic-driven schema management.

- A fresh database is created straight from the current revision chain.
- A legacy database (tables exist, no ``alembic_version``) is first brought to
  the baseline shape with the conservative auto-add-column pass, then stamped at
  the baseline revision and upgraded to head like any other database.
- ``check_schema_drift`` reports model/database differences so CI can fail when
  a model change ships without a revision.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from loguru import logger
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlmodel import SQLModel

from alembic import command

BASELINE_REVISION = "0001_baseline"
# Tables that exist at the baseline revision. Legacy (pre-Alembic) databases are
# brought to exactly this shape before being stamped; later tables come from
# their own revisions.
BASELINE_TABLES = frozenset({"cardtype", "kgrelation", "knowledge", "llmconfig", "project", "prompt", "workflow", "bibleupdatereview", "card", "foreshadowitem", "workflowrun", "nodeexecutionstate"})
BACKEND_DIR = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_DIR / "alembic.ini"
ALEMBIC_DIR = BACKEND_DIR / "alembic"


def alembic_config(engine: Optional[Engine] = None, url: Optional[str] = None) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    cfg.set_main_option("skip_logging_config", "1")
    if url:
        cfg.set_main_option("app_database_url", url)
    elif engine is not None:
        cfg.set_main_option("app_database_url", str(engine.url.render_as_string(hide_password=False)))
    return cfg


def head_revision() -> str:
    script = ScriptDirectory.from_config(alembic_config())
    return script.get_current_head() or ""


def current_revision(engine: Engine) -> Optional[str]:
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def _has_user_tables(engine: Engine) -> bool:
    names = set(inspect(engine).get_table_names())
    names.discard("alembic_version")
    return bool(names)


def auto_add_missing_columns(engine: Engine, only_tables: Optional[frozenset] = None) -> List[str]:
    """Add columns that exist in the models but not in the database.

    Only columns with a server_default (or nullable) can be added safely with
    ALTER TABLE on SQLite. Returns the list of ``table.column`` added.
    """
    inspector = inspect(engine)
    added: List[str] = []
    for table_name, table in SQLModel.metadata.tables.items():
        if only_tables is not None and table_name not in only_tables:
            continue
        if not inspector.has_table(table_name):
            continue
        existing = {col["name"] for col in inspector.get_columns(table_name)}
        for col_name in set(table.columns.keys()) - existing:
            column = table.columns[col_name]
            server_default = column.server_default
            if server_default is None and not column.nullable:
                logger.warning(f"[Schema Migration] Skipping non-nullable column '{col_name}' on '{table_name}' (no server_default)")
                continue
            col_type = column.type.compile(engine.dialect)
            nullable = "" if column.nullable else " NOT NULL"
            default = f" DEFAULT {server_default.arg}" if server_default is not None else ""
            sql = f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_type}{nullable}{default}"
            with engine.begin() as conn:
                conn.execute(text(sql))
            added.append(f"{table_name}.{col_name}")
            logger.info(f"[Schema Migration] Added column '{col_name}' to '{table_name}'")
    return added


def upgrade_database(engine: Engine) -> dict:
    """Bring ``engine``'s database to the current head revision."""
    cfg = alembic_config(engine)
    before = current_revision(engine)
    legacy = before is None and _has_user_tables(engine)
    result = {"before": before, "legacy": legacy, "added_columns": [], "after": None}
    with engine.connect() as conn:
        cfg.attributes["connection"] = conn
        if legacy:
            # Tables predate Alembic: create any missing *baseline* tables and
            # columns, then adopt the baseline so real revisions apply from
            # here on. Tables introduced by later revisions must be created by
            # those revisions, otherwise ``upgrade head`` would fail on them.
            baseline_tables = [t for name, t in SQLModel.metadata.tables.items() if name in BASELINE_TABLES]
            SQLModel.metadata.create_all(conn, tables=baseline_tables)
            conn.commit()
            result["added_columns"] = auto_add_missing_columns(engine, only_tables=BASELINE_TABLES)
            command.stamp(cfg, BASELINE_REVISION)
            conn.commit()
        command.upgrade(cfg, "head")
        conn.commit()
    result["after"] = current_revision(engine)
    logger.info(f"[Schema Migration] database at revision {result['after']} (was {before}, legacy={legacy})")
    return result


def check_schema_drift(engine: Engine) -> List[str]:
    """Return human-readable differences between the models and the database."""
    from alembic.autogenerate import compare_metadata

    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": False, "render_as_batch": True})
        diffs = compare_metadata(ctx, SQLModel.metadata)
    out: List[str] = []
    for d in diffs:
        kind = d[0] if isinstance(d, tuple) else str(d)
        if isinstance(d, tuple):
            # Index differences from SQLite name normalisation are noisy; report tables/columns only.
            if kind in ("add_table", "remove_table"):
                out.append(f"{kind}: {d[1].name}")
            elif kind in ("add_column", "remove_column"):
                out.append(f"{kind}: {d[2]}.{d[3].name}")
        elif isinstance(d, list):
            for sub in d:
                if not (isinstance(sub, tuple) and sub[0] in ("modify_nullable", "modify_default", "modify_type")):
                    continue
                # Legacy SQLite "INTEGER PRIMARY KEY" columns report as nullable; not real drift.
                if sub[0] == "modify_nullable" and str(sub[3]) == "id":
                    continue
                out.append(f"{sub[0]}: {sub[2]}.{sub[3]}")
    return out


def database_file_for(engine: Engine) -> Optional[str]:
    if engine.url.get_backend_name() != "sqlite":
        return None
    return engine.url.database if engine.url.database not in (None, ":memory:") else None
