# Live qualification status

Last updated: 2026-09-05 (hardening branch).

## Credentials

No Kimi K3 credential, model identifier or gateway URL was present in the
qualification environment (no `NF_KIMI_*` / provider variables, no `.env`, no
`LLMConfig` rows in any local database). All **live** stages below are therefore
**NOT RUN**. Everything that does not need a provider was run with the
deterministic test model instead (`NF_QUAL_FAKE_PROVIDER=1`) to prove the
procedure, the durability path and the export/originality validators end to end.

| Stage | Live Kimi K3 | Deterministic self-test (`docs/evidence/self-test-fake-provider.json`) |
| --- | --- | --- |
| A Preflight | NOT RUN (no credential) | preflight orchestration covered by `tests/test_autonomous_preflight.py` (13 tests) |
| B Ingestion + analysis + storylines | NOT RUN | PASS: synthetic fixture EPUB -> 8 storyline options, originality gate rejected 1 leak + 1 near-duplicate, `STORYLINE_SELECTION` reached |
| C 6-chapter canary | NOT RUN | PASS: architecture, bible, plan, preflight, chapters 1–2 committed |
| D Crash + resume | NOT RUN | PASS: `after_chapter_commit!kill` (exit 137) after chapter 3, dead-owner lease expired, `recover_stale_leases` requeued, resumed to chapter 6; one committed run per chapter (1–6), lease generations advanced (1 → 22), stale-generation update rejected |
| E Completion + exports | NOT RUN | PASS: audit, repair, EPUB/DOCX/Markdown/text/report (+ synopsis, character guide) exported; all structural checks `all_valid: true`; `quality_status = completed_with_warnings` |
| F Originality | NOT RUN | PASS on synthetic data: 8-gram overlap 0.0, shared long sentences 0, opening/ending similarity 0.0 |

## Attached EPUB (private test input, never committed)

Only structural facts were recorded (`backend/tests` and the parser were run on
the file; no text was logged or stored outside the throw-away database):

| Fact | Value |
| --- | --- |
| Sanitized filename | `How_to_survive_in_the_Romance_Fantasy_Game.epub` |
| Size | 4 010 764 bytes |
| SHA-256 | `b8d37383259341f1e0c1efa8f997df305673b96353c0038cfa0b326afe0532e1` |
| Container | valid ZIP, `mimetype` = `application/epub+zip`, 646 entries (640 XHTML, 1 OPF, 1 NCX, 1 CSS, 1 JPG), expanded 9.38 MB, ratio 2.42, no traversal / drive paths |
| Upload guards | passed (`inspect_zip_upload`: entry count, per-entry size, expanded size, compression ratio, path safety) |
| Metadata | title, creator, identifier, language `en_US` present |
| Sections detected | 640 (561 main chapters, 77 interludes, 1 front matter excluded, 1 unclassified "Information" section excluded pending review) |
| Included chapters | 638, numbered 1–638 monotonic in spine order |
| Words | 1 380 417 included; per chapter min 1 520 / median 2 079 / max 3 761; 0 chapters under 300 words |
| Estimated input tokens (whole source) | 2 889 583 — the pipeline analyses per chapter with a windowed context; the whole source is never sent in one prompt |
| Ingestion quality report | `ok: true`, no blocking problems; warnings: 20 label numbers missing in the sequence, 1 unclassified section |

The live run on this EPUB (Stages A–F with Kimi K3) is **blocked on credentials**.
To execute it, export `NF_KIMI_PROVIDER`, `NF_KIMI_MODEL`, `NF_KIMI_API_BASE`,
`NF_KIMI_API_KEY`, `NF_EPUB_PATH` (path outside the repository) and follow
`docs/live-qualification.md`. The default canary budget is 400 calls /
4 M total tokens / 800 k output tokens / 40 repair calls, plus a `max_cost_usd`
cap of 25 USD when `NF_PRICE_INPUT` / `NF_PRICE_OUTPUT` are supplied.

## Controlled Beta Status & Operational Boundaries

- **Verdict**: `CONTROLLED BETA — DETERMINISTIC CHECKS PASS; LIVE KIMI/EPUB QUALIFICATION NOT RUN`
- **Deterministic suite**: All tests pass cleanly across Python 3.11 and Python 3.12 (222 backend unit/integration tests, 13 migration tests, 12 concurrency/recovery tests, 22 frontend vitest tests, web and electron builds).
- **Concurrency-Safe Repair**: `max_repair_calls` is enforced via an atomic Compare-And-Set `reserved_repair_calls` counter on `autonomousnoveljob`, guaranteeing that concurrent repair attempts never jointly exceed the repair budget.
- **Conservative Recovery**: Worker crash recovery differentiates undispatched reservations (safely released) from dispatched reservations (conservatively charged at 1 model call, full reserved output tokens, estimated input tokens, and worst-case or unknown cost). Capacity consumed by uncertain attempts is never released to replacement workers, and late responses are idempotent no-ops.
- **Loopback-Only / No Auth**: System is designed strictly for local loopback operation; production multi-user authentication/authorization is not implemented.
- **Database Scope**: Verified on SQLite; PostgreSQL migration and dialect semantics are not exercised.
- **Security & Lint Debt**: Gitleaks and pip-audit pass strictly; 15 inherited npm high advisories documented in allowlist (`docs/ci.md`); legacy lint cleanup tracked in ratchet plan.
