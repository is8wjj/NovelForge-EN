# Kimi K3 configuration, preflight and the autonomous pipeline

## Configuring Kimi K3

Kimi K3 is used through the existing `LLMConfig` abstraction; nothing in the
code assumes a model identifier, endpoint or key. Create a configuration in
*Settings -> Models* (or `POST /api/llm-configs/`) with:

| Field | Value |
| --- | --- |
| `provider` | `openai_compatible` (Kimi's OpenAI-compatible chat-completions API) or `authnd` (browser-backed route, no key) |
| `model_name` | the exact model id your gateway lists under `GET {api_base}/models` — never guessed by the code |
| `api_base` | the versioned base URL of the gateway, e.g. `https://<your-gateway>/v1` (no `/chat/completions` suffix) |
| `api_key` | the gateway credential; stored in the database only, never returned by the API or written to logs |
| `api_protocol` | `chat_completions` (default). `responses` is warned about because most compatible gateways do not implement it |

`app.services.autonomous.preflight.kimi_warnings()` flags the usual endpoint
mistakes (missing `/v1` path, `/chat/completions` suffix, `responses` protocol).

### Structured output

The client first asks LangChain for native structured output (tool calling /
JSON schema). When the gateway does not support it — a typical limitation of
OpenAI-compatible endpoints — `LLMModelClient._default_provider_call` retries
once in **JSON mode**: the JSON schema is appended to the prompt, the response is
parsed (fenced or embedded JSON accepted, `json_repair` as last resort) and
validated with Pydantic. Residual failures enter the bounded schema-repair loop
(`CLARIFIED_SCHEMA_SUFFIX`, one clarified retry per role policy) and, after that,
the recovery ladder (`REDUCE_SCOPE`, `FALLBACK_MODEL`). Auth (401/403), rate
limit (429) and timeout errors skip the JSON-mode retry.

Every attempt records the actual `provider`, `model_name`, `llm_config_id`,
`fallback` flag, `max_tokens` sent and whether usage was reported
(`modelinvocationattempt`). A fallback model is never used silently:
`fallback=True` on the attempt, `fallback_used` on the invocation and a
`RecoveryAction` row when the ladder switched the whole job.

## Preflight

`POST /api/autonomous/preflight`

```json
{"llm_config_id": 3, "fallback_llm_config_id": 4, "timeout_seconds": 45, "check_fallback": true}
```

Runs, in order: static validation (no network), model availability
(`GET /models` for OpenAI-style providers; skipped with a warning when the
provider has no list), one minimal plain-text generation ("reply OK"), one
minimal structured generation validated by Pydantic (`PreflightProbe`), usage
metadata detection (advisory), then the same checks on the fallback when
requested. Requests are capped at 64 output tokens; the model is never asked for
novel content.

The response is sanitized: pass/fail, provider, model, endpoint class, latency,
per-check results, `usage_reporting` (`reported | missing | unknown`), fallback
result, warnings, a stable `failure_category`
(`config_invalid | unreachable | auth_failed | model_not_found | rate_limited |
timeout | text_generation_failed | structured_output_invalid | provider_error |
fallback_failed`), a bounded redacted diagnostic and a timestamp. No prompts,
responses, keys or headers are ever included.

Job creation (`POST /api/autonomous/jobs`) always applies the static validation;
the UI requires a passing preflight or an explicit acknowledgement
(`preflight_acknowledged: true`).

## Autonomous pipeline

Stages (`runner.STAGES`):

```
INGEST -> SOURCE_ANALYSIS -> ANALYSIS_VERIFICATION -> BOOK_STRUCTURE -> FINGERPRINT_BUILD -> EXAMPLE_LIBRARY_BUILD
-> STORYLINE_GENERATION -> STORYLINE_SELECTION (user) -> NOVEL_ARCHITECTURE -> BIBLE_BUILD -> CHAPTER_PLAN_BUILD
-> NOVEL_PREFLIGHT -> CHAPTER_GENERATION_LOOP (one chapter per step) -> WHOLE_NOVEL_AUDIT -> GLOBAL_REPAIR -> EXPORT -> DONE
```

1. Upload (`POST /jobs`, base64 EPUB/TXT/DOCX/Markdown, ≤ 60 MB, zip-bomb and
   traversal checks) -> the worker runs ingestion, analysis and storyline
   generation and stops at `STORYLINE_SELECTION` (`waiting_for: storyline_selection`).
2. `GET /jobs/{id}/storylines` -> `POST /jobs/{id}/select {storyline_id, chapter_count, words_per_chapter?}`.
3. The worker plans, drafts, validates, repairs and commits chapters one at a
   time, audits the whole novel, repairs, exports.
4. `GET /jobs/{id}` (status, stage, progress, budget, lease, recovery),
   `/chapters`, `/report`, `/artifacts`, `/artifacts/{aid}/download`.

`mode=approval_gates` additionally waits before drafting and before export
(`POST /jobs/{id}/approve`); `mode=manual` stops after the example library.

Terminal states are kept distinct: `status` in
`completed | failed | cancelled`, plus `quality_status` in
`completed | completed_with_warnings | quality_gate_failed | manual_review_required`;
`waiting_for` explains a paused job (`budget_exhausted`, `provider_unavailable`,
`manual_review_required`, `paused`).

## Budgets

`budget` on job creation (all `0` = unlimited):

```json
{
  "max_calls": 400, "max_input_tokens": 0, "max_output_tokens": 600000, "max_total_tokens": 3000000,
  "max_repair_calls": 40, "max_cost_usd": 25.0,
  "price_per_million": {"input": 0.6, "output": 2.5},
  "prices": {"7": {"input": 1.0, "output": 4.0}},
  "stage_limits": {"CHAPTER_GENERATION_LOOP": {"max_calls": 300, "max_total_tokens": 2500000}},
  "chapter_limits": {"max_calls_per_chapter": 12, "max_total_tokens_per_chapter": 120000}
}
```

Guarantees (`budget.py`, tested in `tests/test_autonomous_budget.py`):

- every provider attempt (retry, schema repair, fallback) reserves **before** the
  network call: estimated input tokens + the **full** output allowance it may use;
  `max_tokens` sent to the provider is clamped to what the remaining limits allow;
  a request with no viable allowance is refused with `budget_exceeded` and no
  call is made;
- reservation is a compare-and-set on the job row plus a `budgetreservation`
  ledger row, so concurrent attempts cannot jointly exceed a ceiling;
- actual usage is charged afterwards; timeouts/dropped connections charge the
  whole reserved output (the provider may have produced it); missing usage
  metadata charges estimates and counts in `usage_estimated_calls`;
- cost is reserved from `prices[llm_config_id]` or `price_per_million`; a
  `max_cost_usd` without a price table is rejected at job creation and refused at
  reservation; a call with no known price makes the job's cost **unknown**
  (`cost_usd.status = "unknown"`, `estimated_cost_usd = null`), never zero;
- an accounting failure raises `BudgetAccountingError` (the reservation stays
  open — conservative); startup recovery and resume abandon open ledger rows and
  rebuild the counters, so a crash cannot leave phantom reservations.

Raise limits when resuming: `POST /jobs/{id}/resume {"budget": {...}}`.

## Pause, resume, recovery

- **Pause** (`POST /jobs/{id}/pause`): cancels the worker task; the runner marks
  the attempt paused and publishes `paused` under its lease.
- **Resume** (`POST /jobs/{id}/resume`): clears the error and open reservations,
  requeues the job; the runner continues from the persisted stage / next
  uncommitted chapter. Stage functions are idempotent per manuscript/chapter, so
  re-execution converges rather than duplicating.
- **Crash**: the row stays `running` with its lease until the lease expires
  (`AUTONOMOUS_LEASE_SECONDS`, default 300 s, heartbeat every 45 s). On the next
  process start `recover_on_startup` requeues expired-lease jobs and restarts
  them; a live lease held by another process is left alone.
- **Fencing**: every publication is a conditional `UPDATE ... WHERE lease_owner
  = ? AND lease_generation = ?`. A worker that lost its lease gets
  `JobLeaseLost`, publishes nothing, consumes no retry budget and exits with an
  informational log line; the worker wrapper never mutates a job.
- **Failures** are classified (`failures.py`) and recovered through the typed
  ladder (`recovery.py`): retry, clarified schema, re-analyse smallest unit,
  independent verifier, repair artifact, rebuild stale downstream, reduce scope,
  fallback model, pause. Each rung is persisted as a `RecoveryAction`.
