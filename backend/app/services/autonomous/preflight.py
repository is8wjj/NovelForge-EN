"""Provider preflight: prove an LLM configuration works before a long autonomous job.

Two layers:

- ``validate_config`` is static: it never touches the network and reports
  user-actionable problems (missing key, malformed base URL, unsupported
  provider, ...).
- ``run_preflight`` is live: reachability + authentication + model availability
  (model list when the provider exposes one), one minimal plain-text
  generation, one minimal structured generation validated by Pydantic, usage
  metadata presence, timeout behaviour and (optionally) the same checks on the
  fallback configuration.

Requests are deliberately tiny (a one-word echo); preflight never asks the model
for novel content. The result never carries prompts, raw responses, keys or
authorization headers - only sanitized categories and bounded diagnostics.

Provider boundary: ``provider_call`` (same signature as
``LLMModelClient.provider_call``) and ``list_models`` are the only functions
that reach a provider; tests substitute them and exercise the orchestration
unchanged.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from pydantic import BaseModel, ValidationError
from sqlmodel import Session

from app.db.models import LLMConfig
from app.services import llm_config_service
from app.services.autonomous.model_client import ProviderCall, ProviderResult, classify_provider_error, is_auth_error, redact
from app.services.forge.models import API_KEY_PROVIDERS

SUPPORTED_PROVIDERS: Tuple[str, ...] = tuple(API_KEY_PROVIDERS) + ("authnd", "nvidia_authnd", "genspark")
# Providers that need an explicit HTTP(S) base URL (OpenAI-compatible gateways such as Kimi K3).
BASE_URL_REQUIRED: Tuple[str, ...] = ("openai_compatible",)
# Providers exposing an OpenAI-style ``GET /models`` list we can check the model against.
MODEL_LIST_PROVIDERS: Tuple[str, ...] = ("openai", "openai_compatible")
MIN_TIMEOUT_SECONDS = 5.0
MAX_TIMEOUT_SECONDS = 300.0
PROBE_MAX_TOKENS = 64
PROBE_ECHO = "preflight"

# Sanitized failure categories (stable strings for the UI).
CONFIG_INVALID = "config_invalid"
UNREACHABLE = "unreachable"
AUTH_FAILED = "auth_failed"
MODEL_NOT_FOUND = "model_not_found"
RATE_LIMITED = "rate_limited"
TIMEOUT = "timeout"
TEXT_FAILED = "text_generation_failed"
STRUCTURED_INVALID = "structured_output_invalid"
PROVIDER_ERROR = "provider_error"
FALLBACK_FAILED = "fallback_failed"


class PreflightProbe(BaseModel):
    """Minimal structured-output schema: the model must echo a fixed token."""

    ok: bool
    echo: str


TEXT_SYSTEM = "You are a connectivity probe. Follow the instruction exactly."
TEXT_PROMPT = "Reply with the single word OK and nothing else."
STRUCTURED_PROMPT = f'Return a JSON object with two fields: "ok" set to true and "echo" set to the string "{PROBE_ECHO}". Return only the JSON object.'

ListModels = Callable[[LLMConfig, float], Awaitable[Optional[List[str]]]]


@dataclass
class CheckResult:
    name: str
    passed: bool
    skipped: bool = False
    latency_ms: Optional[int] = None
    category: Optional[str] = None
    diagnostic: Optional[str] = None
    detail: Dict[str, Any] = field(default_factory=dict)
    # Advisory checks (usage metadata) produce warnings, never a failed preflight.
    advisory: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "skipped": self.skipped, "advisory": self.advisory, "latency_ms": self.latency_ms, "category": self.category, "diagnostic": self.diagnostic, **({"detail": self.detail} if self.detail else {})}


@dataclass
class PreflightResult:
    passed: bool
    llm_config_id: int
    provider: str
    model: str
    endpoint_class: str
    latency_ms: int
    checks: List[CheckResult]
    usage_reporting: str  # reported | missing | unknown
    warnings: List[str]
    failure_category: Optional[str]
    diagnostic: Optional[str]
    fallback: Optional["PreflightResult"] = None
    fallback_checked: bool = False
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def check(self, name: str) -> Optional[CheckResult]:
        return next((c for c in self.checks if c.name == name), None)


# ---------------------------------------------------------------- static

def endpoint_class(cfg: LLMConfig) -> str:
    provider = (cfg.provider or "").strip().lower()
    if provider in ("openai", "openai_compatible"):
        protocol = llm_config_service._normalize_protocol(getattr(cfg, "api_protocol", None))
        return "openai_responses" if protocol == "responses" else "openai_chat_completions"
    return provider or "unknown"


def _base_url_problems(cfg: LLMConfig, provider: str) -> List[str]:
    raw = (cfg.api_base or cfg.base_url or "").strip()
    if not raw:
        return [f"provider '{provider}' requires an API base URL (for example https://host/v1)"] if provider in BASE_URL_REQUIRED else []
    parsed = urlparse(raw)
    problems: List[str] = []
    if parsed.scheme not in ("http", "https"):
        problems.append("API base URL must start with http:// or https://")
    if not parsed.netloc or "." not in parsed.netloc and "localhost" not in parsed.netloc and not parsed.netloc.startswith("127."):
        problems.append("API base URL has no valid host")
    if parsed.query or parsed.fragment:
        problems.append("API base URL must not contain a query string or fragment")
    if any(ch.isspace() for ch in raw):
        problems.append("API base URL contains whitespace")
    return problems


def validate_config(cfg: Optional[LLMConfig], *, timeout: Optional[float] = None, fallback: Optional[LLMConfig] = None, fallback_requested: bool = False) -> Tuple[bool, List[str]]:
    """Static validation. Returns ``(ok, problems)``; problems are user-actionable and never contain the key."""
    problems: List[str] = []
    if cfg is None:
        return False, ["LLM configuration does not exist"]
    provider = (cfg.provider or "").strip().lower()
    if not provider:
        problems.append("provider is empty")
    elif provider not in SUPPORTED_PROVIDERS:
        problems.append(f"provider '{provider}' is not supported (supported: {', '.join(SUPPORTED_PROVIDERS)})")
    if not (cfg.model_name or "").strip():
        problems.append("model name is empty")
    if provider in API_KEY_PROVIDERS and not (cfg.api_key or "").strip():
        problems.append("API key is missing")
    if provider in API_KEY_PROVIDERS:
        problems.extend(_base_url_problems(cfg, provider))
    if timeout is not None and not (MIN_TIMEOUT_SECONDS <= float(timeout) <= MAX_TIMEOUT_SECONDS):
        problems.append(f"timeout must be between {MIN_TIMEOUT_SECONDS:g} and {MAX_TIMEOUT_SECONDS:g} seconds")
    if fallback_requested:
        if fallback is None:
            problems.append("fallback LLM configuration does not exist")
        else:
            ok_fb, fb_problems = validate_config(fallback)
            if not ok_fb:
                problems.extend(f"fallback: {p}" for p in fb_problems)
            if fallback.id is not None and cfg.id is not None and int(fallback.id) == int(cfg.id):
                problems.append("fallback must be a different configuration from the primary")
    return (not problems), problems


def kimi_warnings(cfg: LLMConfig) -> List[str]:
    """Endpoint-semantics warnings for OpenAI-compatible gateways (Kimi K3 is exposed this way)."""
    provider = (cfg.provider or "").strip().lower()
    if provider != "openai_compatible":
        return []
    out: List[str] = []
    raw = (cfg.api_base or cfg.base_url or "").strip()
    path = urlparse(raw).path.rstrip("/") if raw else ""
    if raw and not path:
        out.append("API base URL has no path; OpenAI-compatible gateways usually expect a versioned base such as .../v1")
    if path.endswith("/chat/completions"):
        out.append("API base URL already ends with /chat/completions; the client appends the request path itself")
    if llm_config_service._normalize_protocol(getattr(cfg, "api_protocol", None)) == "responses":
        out.append("api_protocol is 'responses'; most OpenAI-compatible gateways only implement chat completions")
    return out


# ------------------------------------------------------------------ live

async def default_list_models(cfg: LLMConfig, timeout: float) -> Optional[List[str]]:
    """``GET {api_base}/models`` for OpenAI-style providers; ``None`` when the provider has no list."""
    provider = (cfg.provider or "").strip().lower()
    if provider not in MODEL_LIST_PROVIDERS:
        return None
    import httpx

    transport = llm_config_service.resolve_transport_settings(provider=cfg.provider, api_base=cfg.api_base, base_url=cfg.base_url, api_protocol=getattr(cfg, "api_protocol", None), models_path=getattr(cfg, "models_path", None), user_agent=getattr(cfg, "user_agent", None))
    if not transport["models_url"]:
        return None
    headers = {"Authorization": f"Bearer {cfg.api_key}", **transport["default_headers"]}
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(transport["models_url"], headers=headers)
        response.raise_for_status()
        data = response.json()
    items = data.get("data") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    return [str(item.get("id")) for item in items if isinstance(item, dict) and item.get("id")]


async def _default_provider_call(session: Session, **kw: Any) -> ProviderResult:
    from app.services.autonomous.model_client import LLMModelClient

    client = LLMModelClient(session, default_llm_config_id=int(kw["llm_config_id"]), job_id=None)
    return await client._default_provider_call(**kw)


def _failure(exc: BaseException, cfg: LLMConfig) -> Tuple[str, str]:
    """(sanitized category, bounded redacted diagnostic) for a provider exception."""
    if isinstance(exc, asyncio.TimeoutError):
        return TIMEOUT, "provider did not answer within the preflight timeout"
    _, status, _ = classify_provider_error(exc)
    text = str(exc).lower()
    if status == "timeout":
        return TIMEOUT, redact(str(exc), cfg.api_key)
    if is_auth_error(status, exc) or "authentication" in text:
        return AUTH_FAILED, redact(str(exc), cfg.api_key)
    if status == "429":
        return RATE_LIMITED, redact(str(exc), cfg.api_key)
    if status == "404" or "model not found" in text or "unknown model" in text or ("does not exist" in text and "model" in text):
        return MODEL_NOT_FOUND, redact(str(exc), cfg.api_key)
    if any(k in text for k in ("connect", "name or service not known", "nodename", "refused", "unreachable", "network", "ssl", "certificate")):
        return UNREACHABLE, redact(str(exc), cfg.api_key)
    return PROVIDER_ERROR, redact(str(exc), cfg.api_key)


async def _timed(coro: Awaitable[Any], timeout: float) -> Tuple[Any, int]:
    started = time.monotonic()
    result = await asyncio.wait_for(coro, timeout=timeout)
    return result, int((time.monotonic() - started) * 1000)


async def _check_models(cfg: LLMConfig, list_models: ListModels, timeout: float, warnings: List[str]) -> CheckResult:
    provider = (cfg.provider or "").strip().lower()
    if provider not in MODEL_LIST_PROVIDERS:
        return CheckResult("model_availability", True, skipped=True, detail={"reason": "provider has no model list; availability is proven by the generation checks"})
    try:
        models, latency = await _timed(list_models(cfg, timeout), timeout)
    except Exception as exc:  # noqa: BLE001 - classified at the provider boundary
        category, diag = _failure(exc, cfg)
        if category in (AUTH_FAILED, UNREACHABLE, TIMEOUT):
            return CheckResult("model_availability", False, category=category, diagnostic=diag)
        warnings.append("model list could not be fetched; availability is proven by the generation checks")
        return CheckResult("model_availability", True, skipped=True, category=category, diagnostic=diag)
    if models is None:
        return CheckResult("model_availability", True, skipped=True, latency_ms=latency, detail={"reason": "no model list endpoint configured"})
    if not models:
        warnings.append("model list is empty; availability is proven by the generation checks")
        return CheckResult("model_availability", True, skipped=True, latency_ms=latency)
    wanted = (cfg.model_name or "").strip()
    if wanted in models:
        return CheckResult("model_availability", True, latency_ms=latency, detail={"listed": True, "models_listed": len(models)})
    return CheckResult("model_availability", False, latency_ms=latency, category=MODEL_NOT_FOUND, diagnostic=f"model is not in the provider's model list ({len(models)} models listed)", detail={"models_listed": len(models)})


def _usage_state(pr: ProviderResult) -> str:
    if pr.usage_reported is False:
        return "missing"
    if pr.usage_reported is True or int(pr.input_tokens or 0) > 0 or int(pr.output_tokens or 0) > 0:
        return "reported"
    return "missing"


async def _check_text(cfg: LLMConfig, provider_call: ProviderCall, timeout: float) -> Tuple[CheckResult, Optional[ProviderResult]]:
    try:
        pr, latency = await _timed(provider_call(llm_config_id=int(cfg.id), system_prompt=TEXT_SYSTEM, user_prompt=TEXT_PROMPT, schema=None, temperature=0.0, max_tokens=PROBE_MAX_TOKENS, timeout=timeout), timeout)
    except Exception as exc:  # noqa: BLE001
        category, diag = _failure(exc, cfg)
        return CheckResult("text_generation", False, category=category, diagnostic=diag), None
    text = str(pr.content or "").strip()
    if not text:
        return CheckResult("text_generation", False, latency_ms=latency, category=TEXT_FAILED, diagnostic="provider returned an empty response"), pr
    return CheckResult("text_generation", True, latency_ms=latency, detail={"chars": len(text)}), pr


async def _check_structured(cfg: LLMConfig, provider_call: ProviderCall, timeout: float) -> Tuple[CheckResult, Optional[ProviderResult]]:
    import json

    try:
        pr, latency = await _timed(provider_call(llm_config_id=int(cfg.id), system_prompt=TEXT_SYSTEM, user_prompt=STRUCTURED_PROMPT, schema=PreflightProbe, temperature=0.0, max_tokens=PROBE_MAX_TOKENS, timeout=timeout), timeout)
    except Exception as exc:  # noqa: BLE001
        category, diag = _failure(exc, cfg)
        if "structured output invalid" in str(exc).lower() or isinstance(exc, (ValidationError, json.JSONDecodeError)):
            category, diag = STRUCTURED_INVALID, redact(str(exc), cfg.api_key)
        return CheckResult("structured_output", False, category=category, diagnostic=diag), None
    content = pr.content
    try:
        if isinstance(content, PreflightProbe):
            probe = content
        elif isinstance(content, BaseModel):
            probe = PreflightProbe.model_validate(content.model_dump())
        elif isinstance(content, dict):
            probe = PreflightProbe.model_validate(content)
        else:
            probe = PreflightProbe.model_validate(json.loads(str(content)))
    except (ValidationError, json.JSONDecodeError, TypeError) as exc:
        return CheckResult("structured_output", False, latency_ms=latency, category=STRUCTURED_INVALID, diagnostic=redact(f"{type(exc).__name__}: {exc}", cfg.api_key, limit=200)), pr
    if not probe.ok or probe.echo.strip().lower() != PROBE_ECHO:
        return CheckResult("structured_output", False, latency_ms=latency, category=STRUCTURED_INVALID, diagnostic="structured output validated but did not follow the instruction"), pr
    return CheckResult("structured_output", True, latency_ms=latency), pr


async def _preflight_one(session: Session, cfg: LLMConfig, *, timeout: float, provider_call: ProviderCall, list_models: ListModels) -> PreflightResult:
    started = time.monotonic()
    warnings = kimi_warnings(cfg)
    checks: List[CheckResult] = []
    ok, problems = validate_config(cfg, timeout=timeout)
    checks.append(CheckResult("static_validation", ok, diagnostic="; ".join(problems) if problems else None, category=None if ok else CONFIG_INVALID))
    provider = (cfg.provider or "").strip().lower()
    model = (cfg.model_name or "").strip()
    usage = "unknown"
    if not ok:
        return PreflightResult(False, int(cfg.id), provider, model, endpoint_class(cfg), int((time.monotonic() - started) * 1000), checks, usage, warnings, CONFIG_INVALID, "; ".join(problems))
    models_check = await _check_models(cfg, list_models, timeout, warnings)
    checks.append(models_check)
    text_check, text_pr = await _check_text(cfg, provider_call, timeout)
    checks.append(text_check)
    structured_check: CheckResult
    structured_pr: Optional[ProviderResult] = None
    if text_check.passed:
        structured_check, structured_pr = await _check_structured(cfg, provider_call, timeout)
    else:
        structured_check = CheckResult("structured_output", False, skipped=True, diagnostic="skipped because text generation failed")
    checks.append(structured_check)
    reported = [p for p in (text_pr, structured_pr) if p is not None]
    if reported:
        states = {_usage_state(p) for p in reported}
        usage = "reported" if states == {"reported"} else "missing"
        if usage == "missing":
            warnings.append("provider did not report token usage; budgets fall back to conservative token estimates and cost is reported as estimated")
    checks.append(CheckResult("usage_metadata", usage == "reported", skipped=not reported, advisory=True, diagnostic=None if usage == "reported" else "no usage metadata in provider responses"))
    failing = next((c for c in checks if not c.passed and not c.skipped and not c.advisory), None)
    passed = failing is None
    return PreflightResult(passed, int(cfg.id), provider, model, endpoint_class(cfg), int((time.monotonic() - started) * 1000), checks, usage, warnings, failing.category if failing else None, failing.diagnostic if failing else None)


async def run_preflight(session: Session, llm_config_id: int, *, fallback_llm_config_id: Optional[int] = None, timeout: float = 45.0, check_fallback: bool = True, provider_call: Optional[ProviderCall] = None, list_models: Optional[ListModels] = None) -> PreflightResult:
    """Run every live check on the primary (and, when requested, the fallback) configuration."""
    timeout = float(timeout)
    cfg = session.get(LLMConfig, int(llm_config_id))
    fallback_cfg = session.get(LLMConfig, int(fallback_llm_config_id)) if fallback_llm_config_id else None
    now = datetime.now().isoformat(timespec="seconds")
    if cfg is None:
        return PreflightResult(False, int(llm_config_id), "", "", "unknown", 0, [CheckResult("static_validation", False, category=CONFIG_INVALID, diagnostic="LLM configuration does not exist")], "unknown", [], CONFIG_INVALID, "LLM configuration does not exist", timestamp=now)
    ok, problems = validate_config(cfg, timeout=timeout, fallback=fallback_cfg, fallback_requested=bool(fallback_llm_config_id))
    if not ok:
        return PreflightResult(False, int(cfg.id), (cfg.provider or "").lower(), cfg.model_name or "", endpoint_class(cfg), 0, [CheckResult("static_validation", False, category=CONFIG_INVALID, diagnostic="; ".join(problems))], "unknown", kimi_warnings(cfg), CONFIG_INVALID, "; ".join(problems), timestamp=now)

    async def bound_call(**kw: Any) -> ProviderResult:
        if provider_call is not None:
            return await provider_call(**kw)
        return await _default_provider_call(session, **kw)

    lister = list_models or default_list_models
    result = await _preflight_one(session, cfg, timeout=timeout, provider_call=bound_call, list_models=lister)
    if fallback_cfg is not None and check_fallback:
        fb = await _preflight_one(session, fallback_cfg, timeout=timeout, provider_call=bound_call, list_models=lister)
        result.fallback = fb
        result.fallback_checked = True
        if not fb.passed:
            result.warnings.append(f"fallback configuration failed preflight ({fb.failure_category})")
            if result.passed:
                result.passed = False
                result.failure_category = FALLBACK_FAILED
                result.diagnostic = f"fallback: {fb.diagnostic}" if fb.diagnostic else "fallback configuration failed preflight"
    elif fallback_cfg is not None:
        result.warnings.append("fallback configuration was not checked (check_fallback=false)")
    return result


def result_dict(result: PreflightResult) -> Dict[str, Any]:
    """Client-facing form: no prompts, responses, keys or headers."""
    text = result.check("text_generation")
    structured = result.check("structured_output")
    models = result.check("model_availability")
    return {
        "passed": result.passed,
        "llm_config_id": result.llm_config_id,
        "provider": result.provider,
        "model": result.model,
        "endpoint_class": result.endpoint_class,
        "latency_ms": result.latency_ms,
        "model_availability": models.as_dict() if models else None,
        "text_check": text.as_dict() if text else None,
        "structured_check": structured.as_dict() if structured else None,
        "usage_reporting": result.usage_reporting,
        "fallback_checked": result.fallback_checked,
        "fallback": result_dict(result.fallback) if result.fallback else None,
        "warnings": list(result.warnings),
        "failure_category": result.failure_category,
        "diagnostic": result.diagnostic,
        "checks": [c.as_dict() for c in result.checks],
        "timestamp": result.timestamp,
    }


__all__ = ["AUTH_FAILED", "CONFIG_INVALID", "CheckResult", "FALLBACK_FAILED", "MODEL_NOT_FOUND", "PROVIDER_ERROR", "PreflightProbe", "PreflightResult", "RATE_LIMITED", "STRUCTURED_INVALID", "SUPPORTED_PROVIDERS", "TEXT_FAILED", "TIMEOUT", "UNREACHABLE", "default_list_models", "endpoint_class", "kimi_warnings", "result_dict", "run_preflight", "validate_config"]
