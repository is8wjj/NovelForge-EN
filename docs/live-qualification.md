# Live Kimi K3 qualification procedure

Ordinary CI never touches a provider. This is the manual, credential-bearing
procedure that qualifies a real Kimi K3 configuration and runs a private EPUB end
to end. It records only sanitized evidence (`docs/evidence/`); source text,
generated prose, prompts, responses and keys are never written to the repository.

## Inputs (supplied by the operator, never committed)

| Variable | Meaning |
| --- | --- |
| `NF_KIMI_PROVIDER` | `openai_compatible` (default) or `authnd` |
| `NF_KIMI_MODEL` | exact model id as listed by the gateway |
| `NF_KIMI_API_BASE` | versioned base URL, e.g. `https://<gateway>/v1` |
| `NF_KIMI_API_KEY` | credential (read from the environment by the script, stored only in the local database) |
| `NF_EPUB_PATH` | path to the authorized EPUB **outside** the repository (e.g. `/tmp/qualification/book.epub`) |
| `NF_QUAL_DB` | throw-away database path, e.g. `/tmp/qualification/qual.db` |
| `NF_PRICE_INPUT`, `NF_PRICE_OUTPUT` | USD per million tokens (needed for the cost cap; otherwise cost is reported as unknown) |

```bash
export NOVELFORGE_DB_PATH="$NF_QUAL_DB" HOST=127.0.0.1 PORT=54321
cd backend && . .venv/bin/activate
```

## Stage A — Preflight (stop on failure)

```bash
python scripts/live_qualification.py preflight
```

Creates/updates the `LLMConfig` from the `NF_KIMI_*` variables and runs
`run_preflight` (model list, text, structured output, usage metadata). Exit code
is non-zero unless `passed` is true. The sanitized result is appended to
`docs/evidence/live-preflight.json`.

## Stage B — Ingestion and analysis canary

```bash
python scripts/live_qualification.py analyze
```

Creates the job (`mode=fully_automatic`, `quality_preset=balanced`, hard budget
from the variables below) and runs the worker until `STORYLINE_SELECTION`. It
prints the ingestion quality report (counts only), storyline **titles**,
originality scores and the pairwise-similarity maximum, and appends them to the
evidence file. Confirm the candidates are meaningfully different before
continuing.

Default canary budget (override with `NF_BUDGET_JSON`):

```json
{"max_calls": 400, "max_total_tokens": 4000000, "max_output_tokens": 800000, "max_repair_calls": 40,
 "max_cost_usd": 25, "price_per_million": {"input": <NF_PRICE_INPUT>, "output": <NF_PRICE_OUTPUT>}}
```

## Stage C — Short generation canary (6 chapters)

```bash
python scripts/live_qualification.py select --chapters 6 --words 1500        # picks the highest-originality, non-rejected candidate
python scripts/live_qualification.py run --until-chapters 2                  # architecture, bible, plan, preflight, chapters 1-2
```

Selection basis: the non-rejected candidate with the highest
`originality_score`; ties broken by lowest maximum similarity to the other
options. The chosen title and scores are recorded.

## Stage D — Crash and resume

```bash
# 1. Crash the worker at a supported failpoint after the next durable chapter commit.
AUTONOMOUS_FAILPOINTS=after_chapter_commit!kill python scripts/live_qualification.py run --until-chapters 3   # process exits 137
# 2. Lease expiry: wait AUTONOMOUS_LEASE_SECONDS (300 s) or run with a short lease:
AUTONOMOUS_LEASE_SECONDS=10 python scripts/live_qualification.py recover   # recover_stale_leases + open-reservation cleanup
# 3. Resume in a new process (new lease generation) and prove no duplicates.
python scripts/live_qualification.py run --until-chapters 6
python scripts/live_qualification.py verify-durability                     # one committed run per chapter, generation increased, stale fenced update rejected
```

## Stage E — Completion and exports

```bash
python scripts/live_qualification.py run            # audit, repair, export, DONE
python scripts/live_qualification.py exports        # structural validation of EPUB/DOCX/MD/TXT/report + SHA-256 into the evidence file
python scripts/live_qualification.py report         # budget, retries, schema repairs, fallbacks, quality summary
```

Structural validation: the EPUB is opened with `zipfile`, `mimetype` must be the
first entry and uncompressed, `META-INF/container.xml` must point to the OPF, the
OPF spine must list the chapters in order and every spine item must exist; the
DOCX must contain `word/document.xml` with one heading per chapter; Markdown and
text must contain the chapter headings in order; the report must be valid JSON
with `chapters`, `audit` and `run` keys.

## Stage F — Originality checks (deterministic)

```bash
python scripts/live_qualification.py originality
```

Compares the generated chapters with the source chapters: shared named entities
(from the source fingerprint's entity list), 8-gram overlap ratio, longest common
sentence, distinctive-phrase hits, opening/closing paragraph similarity. Only the
scores are recorded.

## Cleanup

```bash
rm -f "$NF_QUAL_DB" "$NF_QUAL_DB-wal" "$NF_QUAL_DB-shm"
```

Downloaded artifacts and the EPUB live under `/tmp/qualification` (or wherever
`NF_EPUB_PATH` points); delete them after the run unless asked to keep them.

## Status of the last attempt

See `docs/evidence/live-qualification-status.md`. When no credential was
available the file states exactly which stages were **NOT RUN** and why.
