"""Role-based model access for the autonomous pipeline.

Every role has its own temperature, token budget, retry count and prompt
version; they may all resolve to the same Kimi K3 configuration.

Telemetry: one ``ModelInvocation`` per logical request and one
``ModelInvocationAttempt`` per provider attempt (retries, clarified-schema
retries, fallbacks). Attempt rows are written on a *separate* short-lived
session so a failed attempt survives the surrounding stage rollback. No prompt
or response text is stored: hashes and a bounded, redacted diagnostic only.

Budget: every provider attempt reserves, before it runs, its estimated input
tokens plus the **full** output allowance it may consume (``budget.reserve``
clamps the provider ``max_tokens`` to what the job limits still allow) and
reconciles actual usage afterwards. When no viable allowance remains the
attempt is refused with ``BUDGET_EXCEEDED`` and no provider call is made.
Retries, schema repairs and fallback attempts each reserve individually.

Provider boundary: ``LLMModelClient.provider_call`` is the only place that
talks to a provider; tests substitute it to exercise retry, fallback, timeout
and malformed-output paths without live credentials. Nothing here guarantees
identical prose across reruns; it guarantees recorded inputs, versions,
attempts and outcomes.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, Optional, Protocol, Tuple, Type, TypeVar

from loguru import logger
from pydantic import BaseModel, ValidationError
from sqlmodel import Session

from app.db.models import LLMConfig, ModelInvocation, ModelInvocationAttempt
from app.services.autonomous import budget as budget_mod
from app.services.autonomous import failures as fail

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class RolePolicy:
    role: str
    temperature: float
    max_tokens: int
    timeout: float
    max_retries: int


ROLE_POLICIES: Dict[str, RolePolicy] = {
    "source_extractor": RolePolicy("source_extractor", 0.2, 12000, 300, 3),
    "source_analyst": RolePolicy("source_analyst", 0.3, 16000, 300, 3),
    "fingerprint_synthesizer": RolePolicy("fingerprint_synthesizer", 0.3, 12000, 300, 3),
    "storyline_ideator": RolePolicy("storyline_ideator", 0.9, 16000, 420, 3),
    "originality_critic": RolePolicy("originality_critic", 0.2, 8000, 240, 2),
    "novel_architect": RolePolicy("novel_architect", 0.5, 20000, 600, 3),
    "chapter_planner": RolePolicy("chapter_planner", 0.5, 16000, 480, 3),
    "drafter": RolePolicy("drafter", 0.8, 12000, 480, 2),
    "claim_extractor": RolePolicy("claim_extractor", 0.1, 6000, 180, 2),
    "continuity_validator": RolePolicy("continuity_validator", 0.1, 6000, 180, 2),
    "independent_verifier": RolePolicy("independent_verifier", 0.1, 8000, 240, 2),
    "style_evaluator": RolePolicy("style_evaluator", 0.2, 4000, 180, 2),
    "repair_editor": RolePolicy("repair_editor", 0.4, 12000, 480, 2),
    "whole_novel_editor": RolePolicy("whole_novel_editor", 0.4, 16000, 600, 2),
    "preflight": RolePolicy("preflight", 0.0, 200, 60, 0),
}

# The legacy Forge pipeline roles map onto autonomous roles.
FORGE_ROLE_MAP = {"drafting": "drafter", "repair": "repair_editor", "analysis": "source_analyst", "planning": "chapter_planner", "validator": "continuity_validator", "evaluator": "style_evaluator"}

CLARIFIED_SCHEMA_SUFFIX = "\n\n[FORMAT REPAIR]\nYour previous answer did not validate against the required JSON schema{errors}. Return ONLY one JSON object that validates against the schema: no prose, no markdown fences, no comments, every required field present."
JSON_MODE_SUFFIX = "Return ONLY one JSON object (no prose, no markdown fences) that validates against this JSON schema:\n{schema}"
JSON_SCHEMA_PROMPT_CHARS = 12000
DIAGNOSTIC_CHARS = 300
# Provider statuses for which a JSON-mode retry of a failed native structured call is pointless.
NO_JSON_FALLBACK_STATUSES = ("401", "403", "429", "timeout")


class ModelClient(Protocol):
    async def structured(self, *, role: str, schema: Type[T], system_prompt: str, user_prompt: str, prompt_version: str, stage: str = "") -> T: ...

    async def text(self, *, role: str, system_prompt: str, user_prompt: str, prompt_version: str, stage: str = "") -> str: ...


def _estimate_tokens(*texts: str) -> int:
    from app.services.ai.core.token_utils import estimate_tokens

    return sum(estimate_tokens(t or "") for t in texts)


def _sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


_SECRET_RX = re.compile(r"(?i)(bearer\s+[a-z0-9._\-]+|sk-[a-z0-9]{8,}|nvapi-[a-z0-9\-_]{8,}|api[_-]?key[\"']?\s*[:=]\s*[\"']?[a-z0-9._\-]{8,})")


def redact(text: str, *secrets: Optional[str], limit: int = DIAGNOSTIC_CHARS) -> str:
    """Bounded diagnostic with credentials removed. Never contains a full prompt or response."""
    s = str(text or "")
    for sec in secrets:
        if sec and len(sec) >= 6:
            s = s.replace(sec, "***")
    s = _SECRET_RX.sub("***", s)
    return s[:limit]


def classify_provider_error(exc: BaseException) -> Tuple[str, Optional[str], Optional[float]]:
    """(failure category, provider status, retry-after seconds) from a provider exception."""
    text = f"{type(exc).__name__}: {exc}".lower()
    status: Optional[str] = None
    m = re.search(r"\b(401|403|404|408|409|413|422|429|500|502|503|504)\b", text)
    if m:
        status = m.group(1)
    retry_after: Optional[float] = None
    m2 = re.search(r"retry[- ]after[:=]?\s*(\d+(?:\.\d+)?)", text)
    if m2:
        retry_after = float(m2.group(1))
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)) or "timed out" in text or "timeout" in text:
        return fail.PROVIDER_FAILURE, status or "timeout", retry_after
    if status in ("401", "403") or "unauthorized" in text or "invalid api key" in text or "authentication" in text:
        return fail.PROVIDER_FAILURE, status or "401", retry_after
    if status == "429" or "rate limit" in text or "ratelimit" in text:
        return fail.PROVIDER_FAILURE, "429", retry_after or 5.0
    if status == "404" or "model not found" in text or "unknown model" in text or "does not exist" in text and "model" in text:
        return fail.PROVIDER_FAILURE, status or "404", retry_after
    return fail.classify_exception(exc), status, retry_after


def is_auth_error(status: Optional[str], exc: BaseException) -> bool:
    text = str(exc).lower()
    return status in ("401", "403") or "unauthorized" in text or "invalid api key" in text


def _message_text(result: Any) -> str:
    content = getattr(result, "content", result)
    if isinstance(content, list):
        content = "".join(str(c.get("text", "") if isinstance(c, dict) else c) for c in content)
    return str(content)


def _provider_result(content: Any, message: Any) -> ProviderResult:
    """Build a ``ProviderResult`` from a LangChain message, marking whether usage metadata was present."""
    usage = getattr(message, "usage_metadata", None) or {}
    meta = getattr(message, "response_metadata", None) or {}
    reported = isinstance(usage, dict) and ("input_tokens" in usage or "output_tokens" in usage)
    finish = None
    if isinstance(meta, dict):
        finish = meta.get("finish_reason") or meta.get("stop_reason")
    return ProviderResult(
        content=content,
        input_tokens=int(usage.get("input_tokens") or 0) if reported else 0,
        output_tokens=int(usage.get("output_tokens") or 0) if reported else 0,
        provider_request_id=(str(meta.get("id") or meta.get("request_id") or "") or None) if isinstance(meta, dict) else None,
        usage_reported=reported,
        finish_reason=str(finish) if finish else None,
    )


_FENCE_RX = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def _extract_json_object(text: str) -> Optional[Any]:
    """First JSON object in ``text`` (raw, fenced, or embedded in prose); ``None`` when nothing parses."""
    candidates = [text.strip()] + [m.strip() for m in _FENCE_RX.findall(text)]
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
    for cand in candidates:
        if not cand:
            continue
        try:
            return json.loads(cand)
        except json.JSONDecodeError:
            continue
    try:
        from json_repair import repair_json

        repaired = repair_json(text[start:end + 1] if start != -1 and end > start else text, return_objects=True)
        return repaired if isinstance(repaired, (dict, list)) else None
    except Exception:  # noqa: BLE001 - repair is best effort; the caller reports malformed output
        return None


# ---------------------------------------------------------------- telemetry

class InvocationRecorder:
    """Persists ``ModelInvocation`` / ``ModelInvocationAttempt`` rows on their own sessions.

    ``session_factory`` defaults to a fresh session on the application engine so
    writes commit independently of the stage transaction. Tests may pass a factory
    that yields the shared test session.
    """

    def __init__(self, session: Optional[Session] = None, *, job_id: Optional[int] = None, project_id: Optional[int] = None, session_factory: Optional[Callable[[], Session]] = None):
        self.job_id = job_id
        self.project_id = project_id
        self._factory = session_factory or self._default_factory
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0

    @staticmethod
    def _default_factory() -> Session:
        from app.db.session import engine

        return Session(engine)

    def open_invocation(self, **fields: Any) -> int:
        with self._factory() as s:
            row = ModelInvocation(job_id=self.job_id, project_id=self.project_id, started_at=datetime.now(), validation_status="pending", **fields)
            s.add(row)
            s.commit()
            s.refresh(row)
            return int(row.id)

    def record_attempt(self, invocation_id: int, **fields: Any) -> int:
        with self._factory() as s:
            row = ModelInvocationAttempt(invocation_id=invocation_id, job_id=self.job_id, **fields)
            s.add(row)
            s.commit()
            s.refresh(row)
            return int(row.id)

    def close_invocation(self, invocation_id: int, **fields: Any) -> None:
        with self._factory() as s:
            row = s.get(ModelInvocation, invocation_id)
            if row is None:
                return
            for k, v in fields.items():
                setattr(row, k, v)
            row.finished_at = datetime.now()
            s.add(row)
            s.commit()
        self.calls += 1
        self.input_tokens += int(fields.get("input_tokens") or fields.get("input_tokens_estimate") or 0)
        self.output_tokens += int(fields.get("output_tokens") or 0)

    def record(self, **fields: Any) -> int:
        """Backward-compatible one-shot record (single attempt)."""
        inv = self.open_invocation(**{k: v for k, v in fields.items() if k not in ("input_tokens", "output_tokens", "latency_ms", "retries", "validation_status", "error")})
        self.close_invocation(inv, **{k: v for k, v in fields.items() if k in ("input_tokens", "output_tokens", "latency_ms", "retries", "validation_status", "error")})
        return inv


@dataclass
class ProviderResult:
    """What a provider attempt returns to the client: text (or parsed object), usage and ids.

    ``usage_reported`` is ``None`` when the caller did not say; the client then
    infers it from non-zero token counts. ``False`` marks a response whose
    provider gave no usage metadata (budgets fall back to estimates).
    """

    content: Any
    input_tokens: int = 0
    output_tokens: int = 0
    provider_request_id: Optional[str] = None
    provider_status: Optional[str] = None
    usage_reported: Optional[bool] = None
    finish_reason: Optional[str] = None


ProviderCall = Callable[..., Awaitable[ProviderResult]]


# ------------------------------------------------------------------- client

class LLMModelClient:
    """Production client: resolves a role to an LLM config, enforces budgets, records attempts."""

    def __init__(self, session: Session, *, default_llm_config_id: int, role_llm_config_ids: Optional[Dict[str, int]] = None, fallback_llm_config_id: Optional[int] = None, recorder: Optional[InvocationRecorder] = None, job_id: Optional[int] = None, provider_call: Optional[ProviderCall] = None, budget_session_factory: Optional[Callable[[], Session]] = None):
        self.session = session
        self.default_llm_config_id = int(default_llm_config_id)
        self.role_llm_config_ids = {k: int(v) for k, v in (role_llm_config_ids or {}).items() if v}
        self.fallback_llm_config_id = int(fallback_llm_config_id) if fallback_llm_config_id else None
        self.recorder = recorder or InvocationRecorder(job_id=job_id)
        self.job_id = job_id if job_id is not None else self.recorder.job_id
        self.provider_call: ProviderCall = provider_call or self._default_provider_call
        self._budget_factory = budget_session_factory or (lambda: session)
        self.fallback_count = 0
        # Overrides set by recovery actions (scope reduction) — role -> max_tokens.
        self.max_tokens_override: Dict[str, int] = {}

    # ---------------------------------------------------------- resolution
    def config_for(self, role: str, *, fallback: bool = False) -> int:
        if fallback and self.fallback_llm_config_id:
            return self.fallback_llm_config_id
        return self.role_llm_config_ids.get(role) or self.default_llm_config_id

    def _config(self, cid: int) -> Optional[LLMConfig]:
        return self.session.get(LLMConfig, cid)

    def _model_name(self, cid: int) -> str:
        cfg = self._config(cid)
        return cfg.model_name if cfg else ""

    def _provider(self, cid: int) -> str:
        cfg = self._config(cid)
        return cfg.provider if cfg else ""

    # ------------------------------------------------------------ provider
    async def _default_provider_call(self, *, llm_config_id: int, system_prompt: str, user_prompt: str, schema: Optional[Type[BaseModel]], temperature: float, max_tokens: int, timeout: float) -> ProviderResult:
        """The only production path that touches a provider (through the LangChain factory).

        Structured requests first use the provider's native structured-output
        mechanism (tool calling / JSON schema). OpenAI-compatible gateways such
        as Kimi K3 do not all implement it: when the native path fails for a
        reason that is not auth/rate-limit/timeout, the request is retried once
        as a JSON-mode text completion and validated here with Pydantic. The
        bounded schema-repair loop in ``_invoke`` handles residual failures.
        """
        from langchain_core.messages import HumanMessage, SystemMessage

        from app.services.ai.core.chat_model_factory import build_chat_model

        model = build_chat_model(session=self.session, llm_config_id=int(llm_config_id), temperature=temperature, max_tokens=max_tokens, timeout=timeout)
        messages = [SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)] if system_prompt else [HumanMessage(content=user_prompt)]
        if schema is not None:
            try:
                envelope = await asyncio.wait_for(model.with_structured_output(schema, include_raw=True).ainvoke(messages), timeout=timeout + 5)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - decide whether a JSON-mode retry is worth trying
                _, status, _ = classify_provider_error(exc)
                if status in NO_JSON_FALLBACK_STATUSES:
                    raise
                return await self._json_mode_call(model, messages, schema, timeout, native_error=exc)
            raw = envelope.get("raw") if isinstance(envelope, dict) else None
            parsed = envelope.get("parsed") if isinstance(envelope, dict) else envelope
            perr = envelope.get("parsing_error") if isinstance(envelope, dict) else None
            if perr is not None or parsed is None:
                return await self._json_mode_call(model, messages, schema, timeout, native_error=perr or ValueError("empty parsed response"))
            return _provider_result(parsed, raw)
        result = await asyncio.wait_for(model.ainvoke(messages), timeout=timeout + 5)
        text = _message_text(result)
        if not text.strip():
            raise ValueError("LLM returned an empty response")
        return _provider_result(text, result)

    async def _json_mode_call(self, model: Any, messages: list, schema: Type[BaseModel], timeout: float, *, native_error: Any) -> ProviderResult:
        """JSON-mode fallback for providers without reliable native structured output."""
        from langchain_core.messages import HumanMessage

        schema_json = json.dumps(schema.model_json_schema(), ensure_ascii=False)
        suffix = HumanMessage(content=JSON_MODE_SUFFIX.format(schema=schema_json[:JSON_SCHEMA_PROMPT_CHARS]))
        result = await asyncio.wait_for(model.ainvoke(list(messages) + [suffix]), timeout=timeout + 5)
        text = _message_text(result)
        if not text.strip():
            raise ValueError(f"structured output invalid: empty response after native failure ({redact(str(native_error), limit=120)})")
        data = _extract_json_object(text)
        if data is None:
            raise ValueError(f"structured output invalid: no JSON object in response ({redact(str(native_error), limit=120)})")
        return _provider_result(schema.model_validate(data), result)

    # --------------------------------------------------------------- core
    async def _invoke(self, *, role: str, schema: Optional[Type[T]], system_prompt: str, user_prompt: str, prompt_version: str, stage: str) -> Any:
        policy = ROLE_POLICIES.get(role) or ROLE_POLICIES["source_analyst"]
        max_tokens = self.max_tokens_override.get(role, policy.max_tokens)
        primary_cid = self.config_for(role)
        cid = primary_cid
        prompt = user_prompt
        schema_name = schema.__name__ if schema is not None else "text"
        inv_id = self.recorder.open_invocation(stage=stage, role=role, llm_config_id=cid, model_name=self._model_name(cid), prompt_version=prompt_version, schema_name=schema_name, temperature=policy.temperature, input_tokens_estimate=_estimate_tokens(system_prompt, user_prompt), prompt_hash=_sha(system_prompt + "\x1f" + user_prompt))
        started = time.monotonic()
        total_in = total_out = 0
        attempts = 0
        fallback_used = False
        last_exc: Optional[BaseException] = None
        last_category = fail.INTERNAL_ERROR
        max_attempts = policy.max_retries + 1
        while attempts < max_attempts:
            attempts += 1
            est_in = _estimate_tokens(system_prompt, prompt)
            attempt_max_tokens = max_tokens
            reservation: Optional[budget_mod.Reservation] = None
            if self.job_id is not None:
                try:
                    with self._budget_factory() as bs:
                        # Worst case: estimated input + the full output allowance this attempt may consume.
                        reservation = budget_mod.reserve(bs, int(self.job_id), role=role, stage=stage, estimated_input_tokens=est_in, requested_output_tokens=max_tokens, llm_config_id=cid)
                    attempt_max_tokens = reservation.max_output_tokens
                except budget_mod.BudgetExceeded as exc:
                    self.recorder.record_attempt(inv_id, attempt=attempts, provider=self._provider(cid), model_name=self._model_name(cid), llm_config_id=cid, fallback=cid != primary_cid, role=role, stage=stage, completed_at=datetime.now(), status="budget_refused", error_category=fail.BUDGET_EXCEEDED, diagnostic=redact(str(exc)))
                    self.recorder.close_invocation(inv_id, input_tokens=total_in, output_tokens=total_out, latency_ms=int((time.monotonic() - started) * 1000), retries=attempts - 1, total_attempts=attempts, fallback_used=fallback_used, validation_status="error", error=redact(str(exc)))
                    raise
            if reservation is not None:
                with self._budget_factory() as bs:
                    budget_mod.mark_dispatched(bs, reservation)
            a_started = time.monotonic()
            a_status, a_cat, a_pstatus, a_diag, a_retry_after, a_req_id = "ok", None, None, None, None, None
            a_in = a_out = 0
            a_usage_reported = True
            a_output_known = True
            try:
                pr = await self.provider_call(llm_config_id=cid, system_prompt=system_prompt, user_prompt=prompt, schema=schema, temperature=policy.temperature, max_tokens=attempt_max_tokens, timeout=policy.timeout)
                a_in, a_out, a_req_id, a_pstatus = int(pr.input_tokens or 0), int(pr.output_tokens or 0), pr.provider_request_id, pr.provider_status
                a_usage_reported = pr.usage_reported if pr.usage_reported is not None else bool(a_in or a_out)
                content = pr.content
                if schema is not None:
                    if isinstance(content, schema):
                        result: Any = content
                    elif isinstance(content, BaseModel):
                        result = schema.model_validate(content.model_dump())
                    elif isinstance(content, dict):
                        result = schema.model_validate(content)
                    else:
                        result = schema.model_validate(json.loads(str(content)))
                    if not a_out:
                        a_out = _estimate_tokens(json.dumps(result.model_dump(mode="json"), ensure_ascii=False))
                else:
                    result = str(content)
                    if not result.strip():
                        raise ValueError("LLM returned an empty response")
                    if not a_out:
                        a_out = _estimate_tokens(result)
                if not a_in:
                    a_in = est_in
                total_in += a_in
                total_out += a_out
                self._finish_reservation(reservation, a_in, a_out, True, usage_reported=a_usage_reported)
                self.recorder.record_attempt(inv_id, attempt=attempts, provider=self._provider(cid), model_name=self._model_name(cid), llm_config_id=cid, fallback=cid != primary_cid, role=role, stage=stage, completed_at=datetime.now(), latency_ms=int((time.monotonic() - a_started) * 1000), status="ok", provider_status=a_pstatus, provider_request_id=a_req_id, input_tokens=a_in, output_tokens=a_out, timeout_seconds=policy.timeout, response_hash=_sha(json.dumps(result.model_dump(mode="json"), ensure_ascii=False) if schema is not None else result), usage_reported=a_usage_reported, max_tokens=attempt_max_tokens)
                self.recorder.close_invocation(inv_id, llm_config_id=cid, model_name=self._model_name(cid), input_tokens=total_in, output_tokens=total_out, latency_ms=int((time.monotonic() - started) * 1000), retries=attempts - 1, total_attempts=attempts, selected_attempt=attempts, fallback_used=fallback_used, validation_status="ok")
                return result
            except asyncio.CancelledError:
                # Cancelled mid-flight: the provider may have produced the whole output.
                self._finish_reservation(reservation, a_in or est_in, a_out, False, output_known=False, usage_reported=False)
                self.recorder.record_attempt(inv_id, attempt=attempts, provider=self._provider(cid), model_name=self._model_name(cid), llm_config_id=cid, fallback=cid != primary_cid, role=role, stage=stage, completed_at=datetime.now(), latency_ms=int((time.monotonic() - a_started) * 1000), status="error", error_category="cancelled", max_tokens=attempt_max_tokens)
                self.recorder.close_invocation(inv_id, input_tokens=total_in, output_tokens=total_out, latency_ms=int((time.monotonic() - started) * 1000), retries=attempts - 1, total_attempts=attempts, fallback_used=fallback_used, validation_status="error", error="cancelled")
                raise
            except budget_mod.BudgetAccountingError:
                raise
            except (ValidationError, json.JSONDecodeError) as exc:
                last_exc, last_category = exc, fail.MALFORMED_OUTPUT
                a_status, a_cat = "invalid", fail.MALFORMED_OUTPUT
                a_diag = redact(str(exc)[:DIAGNOSTIC_CHARS])
            except Exception as exc:  # noqa: BLE001 - classified at the provider boundary
                last_exc = exc
                last_category, a_pstatus, a_retry_after = classify_provider_error(exc)
                a_status, a_cat = ("timeout" if a_pstatus == "timeout" else "error"), last_category
                cfg = self._config(cid)
                a_diag = redact(str(exc), cfg.api_key if cfg else None)
                # Timeouts and dropped connections: the output actually generated is unknown.
                a_output_known = a_pstatus not in ("timeout",) and "connection" not in str(exc).lower()
                a_usage_reported = False
            # Failed attempt: charge what we know (the full reserved output when unknown), record, decide on the next rung.
            a_in = a_in or est_in
            charged_out = a_out if a_output_known else max(a_out, reservation.output_tokens if reservation else a_out)
            total_in += a_in
            total_out += charged_out
            self._finish_reservation(reservation, a_in, a_out, False, output_known=a_output_known, usage_reported=a_usage_reported)
            self.recorder.record_attempt(inv_id, attempt=attempts, provider=self._provider(cid), model_name=self._model_name(cid), llm_config_id=cid, fallback=cid != primary_cid, role=role, stage=stage, completed_at=datetime.now(), latency_ms=int((time.monotonic() - a_started) * 1000), status=a_status, error_category=a_cat, provider_status=a_pstatus, retry_after_seconds=a_retry_after, input_tokens=a_in, output_tokens=charged_out, timeout_seconds=policy.timeout, diagnostic=a_diag, usage_reported=a_usage_reported, max_tokens=attempt_max_tokens)
            logger.warning(f"[Autonomous] {role} attempt {attempts}/{max_attempts} failed ({last_category}, status={a_pstatus}) for job {self.job_id}")
            if attempts >= max_attempts:
                break
            if last_category == fail.MALFORMED_OUTPUT and schema is not None:
                errs = ""
                if isinstance(last_exc, ValidationError):
                    errs = ": " + "; ".join(f"{'.'.join(str(p) for p in e.get('loc', ()))}: {e.get('msg')}" for e in last_exc.errors()[:5])
                prompt = user_prompt + CLARIFIED_SCHEMA_SUFFIX.format(errors=errs[:400])
            if last_category == fail.PROVIDER_FAILURE:
                if is_auth_error(a_pstatus, last_exc) and not (self.fallback_llm_config_id and cid != self.fallback_llm_config_id):
                    break  # retrying an invalid credential is pointless
                if self.fallback_llm_config_id and cid != self.fallback_llm_config_id and attempts >= 2:
                    cid = self.fallback_llm_config_id
                    fallback_used = True
                    self.fallback_count += 1
                    logger.info(f"[Autonomous] {role}: switching to fallback model {self._model_name(cid)} for job {self.job_id}")
                await asyncio.sleep(min(a_retry_after or 2 ** (attempts - 1), 8))
        self.recorder.close_invocation(inv_id, llm_config_id=cid, model_name=self._model_name(cid), input_tokens=total_in, output_tokens=total_out, latency_ms=int((time.monotonic() - started) * 1000), retries=attempts - 1, total_attempts=attempts, fallback_used=fallback_used, validation_status="error", error=redact(str(last_exc)))
        raise fail.StageFailure(last_category, f"{role} call failed after {attempts} attempt(s): {redact(str(last_exc))}", detail={"attempts": attempts, "fallback_used": fallback_used, "schema": schema_name})

    def _finish_reservation(self, reservation: Optional[budget_mod.Reservation], a_in: int, a_out: int, ok: bool, *, output_known: bool = True, usage_reported: bool = True) -> None:
        """Reconcile the attempt's reservation. An accounting failure is surfaced (typed) rather than logged away:
        the reservation stays open, which is the conservative state - recovery abandons it deterministically."""
        if reservation is None:
            return
        with self._budget_factory() as bs:
            budget_mod.reconcile(bs, reservation, input_tokens=a_in, output_tokens=a_out, succeeded=ok, output_known=output_known, usage_reported=usage_reported)

    async def structured(self, *, role: str, schema: Type[T], system_prompt: str, user_prompt: str, prompt_version: str, stage: str = "") -> T:
        return await self._invoke(role=role, schema=schema, system_prompt=system_prompt, user_prompt=user_prompt, prompt_version=prompt_version, stage=stage)

    async def text(self, *, role: str, system_prompt: str, user_prompt: str, prompt_version: str, stage: str = "") -> str:
        return await self._invoke(role=role, schema=None, system_prompt=system_prompt, user_prompt=user_prompt, prompt_version=prompt_version, stage=stage)


class ForgeDrafterAdapter:
    """Adapts a ``ModelClient`` to the Forge ``Drafter`` protocol used by ``pipeline.run_chapter``."""

    def __init__(self, client: ModelClient, *, stage: str = "CHAPTER_GENERATION_LOOP"):
        self.client = client
        self.stage = stage

    async def __call__(self, *, role: str, system_prompt: str, user_prompt: str, context: Any) -> str:
        from app.services.forge.pipeline import DRAFT_PROMPT_VERSION, REPAIR_PROMPT_VERSION

        auto_role = FORGE_ROLE_MAP.get(role, role)
        return await self.client.text(role=auto_role, system_prompt=system_prompt, user_prompt=user_prompt, prompt_version=DRAFT_PROMPT_VERSION if role == "drafting" else REPAIR_PROMPT_VERSION, stage=f"{self.stage}:ch{getattr(context, 'chapter_number', '?')}")


def validate_or_raise(schema: Type[T], data: Any) -> T:
    try:
        return schema.model_validate(data)
    except ValidationError as exc:
        raise fail.StageFailure(fail.MALFORMED_OUTPUT, f"{schema.__name__} validation failed: {exc.errors()[:3]}")


__all__ = ["CLARIFIED_SCHEMA_SUFFIX", "FORGE_ROLE_MAP", "ForgeDrafterAdapter", "InvocationRecorder", "LLMModelClient", "ModelClient", "ProviderCall", "ProviderResult", "ROLE_POLICIES", "RolePolicy", "classify_provider_error", "redact", "validate_or_raise"]
