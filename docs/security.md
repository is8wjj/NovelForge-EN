# Security and deployment boundary

## Decision: local, single-user application

NovelForge has **no authentication, session, user or ownership model**: every
endpoint (projects, cards, LLM configurations including their API keys, jobs,
telemetry, reports, chapters, downloads) is reachable by anyone who can reach the
port. The tree therefore treats the backend as a **local, single-user service**
and makes that boundary explicit and safe by default:

| Concern | Default | Override |
| --- | --- | --- |
| Bind address | `127.0.0.1` (`main.py`, `run_backend.py`) | `HOST=` env var — prints an "unsupported" warning when not loopback |
| Port | `54321` | `PORT=` |
| CORS | `CORS_ORIGINS=local`: loopback origins on any port (Electron dev server / Vite) plus the `null` origin of packaged Electron `file://` pages | comma-separated explicit origins, or `*` |
| Artifact and job data | SQLite file on the local disk | `NOVELFORGE_DB_PATH` |

**Remote / multi-user exposure is unsupported.** Putting the API on `0.0.0.0`
or behind a reverse proxy gives every network user full read/write access to
all data and to stored provider keys. Adding remote support would require, at
minimum: authenticated endpoints, job/project/artifact ownership and
authorization on job lists, telemetry, reports, chapters and downloads, rate and
upload limits, and non-enumerable identifiers. None of that exists and it was
deliberately not built as part of hardening a local tool.

The web build's CSP still allows `connect-src *` so the renderer can talk to a
backend on another port of the same machine; the Electron CSP is restricted to
`http://127.0.0.1:54321`.

## Upload and parsing hardening (`app/api/endpoints/autonomous.py`, `lab/manuscript_import.py`)

- base64 is size-checked **before** decoding (`len(raw) > limit*4/3+16` -> 413)
  and the decoded bytes are checked again (`AUTONOMOUS_MAX_UPLOAD_BYTES`, 60 MB).
- EPUB containers are inspected before parsing: entry count
  (`AUTONOMOUS_MAX_ZIP_ENTRIES`, 5000), per-entry size (20 MB), total expanded size
  (400 MB), compression ratio (> 200:1 on entries > 1 MB rejected), absolute /
  drive-letter / `..` paths rejected. The manuscript importer applies the same
  path and size checks a second time (`_safe_zip`).
- Malformed containers return 400 (`BadZipFile`); malformed XHTML falls through
  the ingestion quality report (`INSUFFICIENT_EVIDENCE`) instead of crashing.
- Filenames are reduced to a conservative basename (`safe_filename`) before
  storage and before being placed in `Content-Disposition` (header-safe, quoted,
  120 chars max).
- Artifact downloads are job-scoped
  (`/jobs/{job_id}/artifacts/{artifact_id}/download` verifies the artifact
  belongs to the job); the legacy unscoped route now only redirects there.

## Secrets and telemetry

- `ModelInvocation` / `ModelInvocationAttempt` store prompt and response
  **hashes**, token counts and a ≤300-char redacted diagnostic; never prompt or
  response text. `redact()` strips the configuration's key, `Bearer` tokens and
  common key formats.
- Preflight results and job responses never include `api_key`, headers, prompts
  or raw responses (`tests/test_autonomous_preflight.py::test_preflight_redacts_secrets_everywhere`).
- Source manuscript text lives only in the database (job `source_bytes` and the
  reference project's chapter cards). It is never written to logs.
- Failpoints (`AUTONOMOUS_FAILPOINTS`) are read from the process environment
  only and cannot be triggered by a request.

## Repository hygiene

`security.yml` fails when a database, EPUB, key file or `.env` is tracked, when
the ignore rules stop covering local artifacts, or when gitleaks finds a secret.
`pip-audit --strict` and `npm audit --audit-level=high --omit=dev` run on every
push; accepted findings are listed in `docs/ci.md`.
