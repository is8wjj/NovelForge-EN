# CI, release gates and branch protection

## Workflows (`.github/workflows/`)

| Workflow | Jobs | Needs secrets? |
| --- | --- | --- |
| `backend.yml` | Python 3.11 + 3.12 matrix: deterministic install, app import (asserts the autonomous preflight route), Alembic fresh upgrade + drift check, populated-database migration tests, startup smoke, full pytest with coverage (`coverage.xml` artifact), scoped `ruff`, OpenAPI export | No |
| `frontend.yml` | `npm ci` from the lockfile, `npm run typecheck`, scoped ESLint, Vitest, `npm run build:web`, `electron-vite build` (bundles, no installer) | No |
| `security.yml` | tracked-file hygiene (no `.db`, `.epub`, keys, `.env`), ignore-rule check, gitleaks secret scan, `pip-audit --strict`, `npm audit` with an explicit policy | Only the default `GITHUB_TOKEN` (gitleaks) |

Ordinary CI never uses a provider credential: every backend test substitutes
`provider_call` / `list_models` at the provider boundary, and the frontend tests
mock the HTTP layer. The live Kimi K3 qualification is a manual procedure
(`docs/live-qualification.md`).

## Lint ratchet plan

The tree carries a large legacy lint debt (~22k ESLint findings, mostly
Prettier formatting and `no-explicit-any`; ~700 ruff findings under a broad
rule set). To keep CI meaningful without a permanently red check:

1. **Now**: ruff runs with a small correctness-oriented rule set (`backend/ruff.toml`)
   on the autonomous pipeline, its endpoint, `app/db/migrations.py` and the new
   test modules; ESLint runs on the autonomous workflow files with `any`,
   return-type and Prettier rules disabled. Both are green.
2. **Next**: widen ruff to `app/services/forge`, `app/services/lab`, then
   `app/`; enable `prettier --check` on the autonomous files after one
   formatting-only commit.
3. **Then**: run `npm run lint` on the full tree after `prettier --write .`
   lands as its own commit; re-enable `no-explicit-any` file by file.

## Accepted security findings

| Finding | Where | Why accepted | Exit criteria |
| --- | --- | --- | --- |
| GHSA-xf7x-x43h-rpqh (`json-repair` < 0.60.1) | backend runtime | `langchain-qwq 0.3.5` pins `json-repair < 0.54`; only reached by the bounded JSON-mode fallback on the provider's own text | upgrade when `langchain-qwq` lifts the pin |
| 15 `high` npm advisories (axios, electron/electron-updater/builder-util-runtime/extract-zip, form-data, js-yaml, linkify-it, lodash/lodash-es, nanoid, picomatch, postcss, rollup, tar-fs) | frontend lockfile as inherited | `npm audit fix --omit=dev` was tried: it rewrites the lockfile so that `npm ci` no longer installs the dev toolchain (`cross-env`, `electron-vite`) and leaves Electron/`extract-zip` unfixed (needs Electron 44, a major upgrade). The lockfile was therefore left as inherited; the allowlist in `security.yml` is exactly this set and `critical` fails the build | dedicated dependency-upgrade PR: `npm update` per package with `npm ci` + typecheck + builds green, then shrink the allowlist |

The remaining moderate findings are dev-only transitive packages.

## Recommended `main` branch protection

Configuring branch protection needs repository admin rights, which this
automation does not have; apply manually under
*Settings -> Branches -> Add branch ruleset* (or classic protection) for `main`:

1. **Require a pull request before merging** — direct pushes to `main` disabled.
2. **Require approvals**: at least **1**; enable **Dismiss stale pull request approvals when new commits are pushed**.
3. **Require status checks to pass before merging**, with these required checks
   (names as reported by the workflows):
   - `Backend / Python 3.11 · import, migrate, test`
   - `Backend / Python 3.12 · import, migrate, test`
   - `Frontend / typecheck · lint (scoped) · vitest · web + electron build`
   - `Security and repository hygiene / No committed databases, books, credentials or local artifacts`
   - `Security and repository hygiene / Python dependency audit`
   - `Security and repository hygiene / npm audit (fail on critical; high findings reported — see docs/ci.md)`
   Enable **Require branches to be up to date before merging** so a merge cannot
   happen while checks are pending or stale.
4. **Block force pushes** and **Restrict deletions** on `main`.
5. Optionally **Require conversation resolution before merging**.

With GitHub CLI and admin rights the equivalent is:

```bash
gh api -X PUT repos/<owner>/NovelForge-EN/branches/main/protection --input - <<'JSON'
{"required_status_checks":{"strict":true,"contexts":["Backend / Python 3.11 · import, migrate, test","Backend / Python 3.12 · import, migrate, test","Frontend / typecheck · lint (scoped) · vitest · web + electron build","Security and repository hygiene / No committed databases, books, credentials or local artifacts","Security and repository hygiene / Python dependency audit","Security and repository hygiene / npm audit (fail on critical; high findings reported — see docs/ci.md)"]},
 "enforce_admins":true,"required_pull_request_reviews":{"dismiss_stale_reviews":true,"required_approving_review_count":1},
 "restrictions":null,"allow_force_pushes":false,"allow_deletions":false}
JSON
```
