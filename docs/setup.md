# Clean installation, migration and verification

These are the exact commands CI runs (`.github/workflows/`). They work from a
fresh clone on Linux, macOS and Windows (PowerShell paths differ only in the
virtual-environment activation).

## Prerequisites

- Python 3.11 or 3.12
- Node.js 22 (18+ works for the web build) and npm 10+
- No provider credentials are needed for any of the steps below.

## Backend

```bash
cd backend
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt -r requirements-dev.txt
# PySide6 (AuthND hCaptcha helper) is heavy; skip it when you do not use AuthND:
#   grep -v -i pyside6 requirements.txt > /tmp/req.txt && pip install -r /tmp/req.txt -r requirements-dev.txt

cp .env.example .env            # optional; every setting has a safe default
```

### Database migrations

The database is SQLite (`backend/novelforge.db` by default, override with
`NOVELFORGE_DB_PATH`). Alembic owns the schema:

```bash
# Upgrade (or create) the configured database to the current head revision
alembic upgrade head

# What the application does at startup, plus a model/DB drift check
NOVELFORGE_DB_PATH=/tmp/smoke.db python scripts/migration_smoke.py
```

`app.core.startup.startup()` calls `upgrade_database()` on every start, so a
populated older database is upgraded automatically. Databases that predate
Alembic (tables but no `alembic_version`) are adopted at the baseline revision
and then upgraded. See `docs/migrations.md` for the duplicate-data rules.

### Start the backend

```bash
python main.py                  # loopback only: http://127.0.0.1:54321
curl -s http://127.0.0.1:54321/ ; curl -s http://127.0.0.1:54321/openapi.json | head -c 200
```

`HOST`/`PORT` can be overridden through the environment; binding to anything
other than loopback is unsupported (`docs/security.md`).

### Backend tests

```bash
python -m pytest tests -q --timeout 900                     # full suite (no network)
python -m pytest tests/test_startup_smoke.py -q             # app import + all routers
python -m pytest tests/test_migrations.py -q                # fresh / populated / duplicate upgrades
python -m pytest tests/test_autonomous_preflight.py tests/test_autonomous_fencing.py tests/test_autonomous_budget.py -q
ruff check app/services/autonomous app/api/endpoints/autonomous.py app/db/migrations.py tests/test_autonomous_*.py tests/test_migrations.py tests/test_startup_smoke.py
```

Tests never touch `backend/novelforge.db`: `tests/conftest.py` points
`NOVELFORGE_DB_PATH` at a temporary file before any `app.*` import.

## Frontend

```bash
cd frontend
npm ci --ignore-scripts --no-audit --no-fund   # lockfile install; skips electron-builder native rebuild
npm run typecheck                              # tsc (main/preload) + vue-tsc (renderer)
npx vitest run                                 # unit tests (mocked HTTP)
npm run build:web                              # static web build -> dist-web/
npx electron-vite build                        # main/preload/renderer bundles (no installer packaging)
npm run dev:web                                # dev server on :5173 proxying /api -> 127.0.0.1:54321
```

`npm run lint` on the whole tree reports thousands of legacy Prettier/`any`
findings; CI lints only the autonomous workflow files (`docs/ci.md`).

## Generated API types

The renderer's `src/renderer/src/types/generated.d.ts` is produced from the
backend OpenAPI document:

```bash
cd backend && python scripts/export_openapi.py ../backend/openapi.json   # openapi.json is git-ignored
cd ../frontend && npm run gen:types:file
```

The autonomous API additionally keeps hand-typed request/response interfaces in
`src/renderer/src/api/autonomous.ts` because its responses are `Dict[str, Any]`
in OpenAPI; keep them in sync with `backend/app/services/autonomous/preflight.py`
(`result_dict`) and `budget.py` (`usage_snapshot`).

## Artifact locations and cleanup

| What | Where | Cleanup |
| --- | --- | --- |
| Application database | `backend/novelforge.db` (+ `-wal`/`-shm`) or `NOVELFORGE_DB_PATH` | delete the file; ignored by git |
| Uploaded source manuscripts | `autonomousnoveljob.source_bytes` and the reference project's chapter cards inside the database | delete the job / project |
| Exported novels | `exportartifact` rows (EPUB/DOCX/Markdown/text/report bytes) | downloaded through `/api/autonomous/jobs/{id}/artifacts/{aid}/download` |
| Test databases | `$TMPDIR/novelforge-tests-*/` | safe to delete any time |
| OpenAPI export | `backend/openapi.json` | git-ignored |
| Frontend builds | `frontend/dist-web`, `frontend/out`, `frontend/dist` | `npm run clean`, `npm run clean:web` |

Never commit databases, EPUBs, generated manuscripts or `.env` files; the
`security.yml` workflow fails the build if any are tracked.
