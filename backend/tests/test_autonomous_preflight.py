"""Provider preflight: static validation + live checks with a substituted provider boundary.

Only ``provider_call`` / ``list_models`` are faked; the preflight orchestration
itself (ordering, skipping, usage detection, fallback handling, redaction) runs
for real. No network, no credentials.
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any, Dict, List, Optional

from sqlmodel import Session

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

SECRET = "sk-preflight-secret-0123456789abcdef"


def _cfg(session: Session, **over: Any):
    from app.db.models import LLMConfig

    fields = {"provider": "openai_compatible", "model_name": "kimi-k3-test", "api_key": SECRET, "api_base": "https://gateway.example.test/v1", "display_name": "Kimi test"}
    fields.update(over)
    cfg = LLMConfig(**fields)
    session.add(cfg)
    session.commit()
    session.refresh(cfg)
    return cfg


def _good_provider(calls: Optional[List[Dict[str, Any]]] = None, *, usage: bool = True):
    from app.services.autonomous.model_client import ProviderResult

    async def provider(**kw):
        if calls is not None:
            calls.append(kw)
        if kw["schema"] is not None:
            return ProviderResult(content={"ok": True, "echo": "preflight"}, input_tokens=20 if usage else 0, output_tokens=8 if usage else 0, usage_reported=usage)
        return ProviderResult(content="OK", input_tokens=15 if usage else 0, output_tokens=1 if usage else 0, usage_reported=usage)

    return provider


async def _models_ok(cfg, timeout):
    return [cfg.model_name, "other-model"]


def _run(session, cfg_id, **kw):
    from app.services.autonomous import preflight

    return asyncio.run(preflight.run_preflight(session, cfg_id, timeout=kw.pop("timeout", 10.0), **kw))


# ------------------------------------------------------------------- static
def test_validate_config_valid_and_problems(app_client):
    from app.db.models import LLMConfig
    from app.db.session import engine
    from app.services.autonomous import preflight

    with Session(engine) as s:
        cfg = _cfg(s)
        assert preflight.validate_config(cfg) == (True, [])
        assert preflight.validate_config(None) == (False, ["LLM configuration does not exist"])
        ok, problems = preflight.validate_config(LLMConfig(provider="openai_compatible", model_name="m", api_key="", api_base="https://x.example/v1"))
        assert not ok and "API key is missing" in problems
        ok, problems = preflight.validate_config(LLMConfig(provider="openai_compatible", model_name="m", api_key="k", api_base="gateway.example/v1"))
        assert not ok and any("http://" in p for p in problems)
        ok, problems = preflight.validate_config(LLMConfig(provider="openai_compatible", model_name="m", api_key="k", api_base=""))
        assert not ok and any("requires an API base URL" in p for p in problems)
        ok, problems = preflight.validate_config(LLMConfig(provider="carrier_pigeon", model_name="m", api_key="k"))
        assert not ok and any("not supported" in p for p in problems)
        ok, problems = preflight.validate_config(LLMConfig(provider="openai", model_name="", api_key="k"))
        assert not ok and "model name is empty" in problems
        ok, problems = preflight.validate_config(cfg, timeout=1.0)
        assert not ok and any("timeout" in p for p in problems)
        ok, problems = preflight.validate_config(cfg, fallback=None, fallback_requested=True)
        assert not ok and "fallback LLM configuration does not exist" in problems
        bad_fb = _cfg(s, api_key="")
        ok, problems = preflight.validate_config(cfg, fallback=bad_fb, fallback_requested=True)
        assert not ok and any(p.startswith("fallback:") for p in problems)
        ok, problems = preflight.validate_config(cfg, fallback=cfg, fallback_requested=True)
        assert not ok and any("different configuration" in p for p in problems)
        for p in problems:
            assert SECRET not in p


def test_kimi_endpoint_semantics_warnings(app_client):
    from app.db.models import LLMConfig
    from app.services.autonomous import preflight

    assert preflight.kimi_warnings(LLMConfig(provider="openai_compatible", model_name="m", api_key="k", api_base="https://gw.example/v1")) == []
    assert any("/chat/completions" in w for w in preflight.kimi_warnings(LLMConfig(provider="openai_compatible", model_name="m", api_key="k", api_base="https://gw.example/v1/chat/completions")))
    assert any("no path" in w for w in preflight.kimi_warnings(LLMConfig(provider="openai_compatible", model_name="m", api_key="k", api_base="https://gw.example")))
    assert any("responses" in w for w in preflight.kimi_warnings(LLMConfig(provider="openai_compatible", model_name="m", api_key="k", api_base="https://gw.example/v1", api_protocol="responses")))
    assert preflight.endpoint_class(LLMConfig(provider="openai_compatible", model_name="m", api_key="k")) == "openai_chat_completions"


# --------------------------------------------------------------------- live
def test_preflight_passes_with_valid_config(app_client):
    from app.db.session import engine
    from app.services.autonomous import preflight

    calls: List[Dict[str, Any]] = []
    with Session(engine) as s:
        cfg = _cfg(s)
        res = _run(s, cfg.id, provider_call=_good_provider(calls), list_models=_models_ok)
        assert res.passed and res.failure_category is None
        assert res.provider == "openai_compatible" and res.model == "kimi-k3-test" and res.endpoint_class == "openai_chat_completions"
        assert res.usage_reporting == "reported"
        assert [c.name for c in res.checks] == ["static_validation", "model_availability", "text_generation", "structured_output", "usage_metadata"]
        assert all(c.passed for c in res.checks)
        assert len(calls) == 2 and calls[0]["schema"] is None and calls[1]["schema"] is preflight.PreflightProbe
        assert all(kw["max_tokens"] <= preflight.PROBE_MAX_TOKENS for kw in calls)  # minimal, inexpensive requests
        d = preflight.result_dict(res)
        assert d["passed"] and d["text_check"]["passed"] and d["structured_check"]["passed"] and d["timestamp"]
        assert d["fallback"] is None and d["fallback_checked"] is False
        assert SECRET not in repr(d) and "Authorization" not in repr(d)


def test_preflight_missing_config(app_client):
    from app.db.session import engine
    from app.services.autonomous import preflight

    with Session(engine) as s:
        res = _run(s, 999_999, provider_call=_good_provider())
        assert not res.passed and res.failure_category == preflight.CONFIG_INVALID
        assert "does not exist" in res.diagnostic


def test_preflight_static_failures_make_no_provider_call(app_client):
    from app.db.session import engine
    from app.services.autonomous import preflight

    calls: List[Dict[str, Any]] = []
    with Session(engine) as s:
        no_key = _cfg(s, api_key="")
        res = _run(s, no_key.id, provider_call=_good_provider(calls))
        assert not res.passed and res.failure_category == preflight.CONFIG_INVALID and "API key is missing" in res.diagnostic
        bad_url = _cfg(s, api_base="not a url")
        res = _run(s, bad_url.id, provider_call=_good_provider(calls))
        assert not res.passed and res.failure_category == preflight.CONFIG_INVALID and "http" in res.diagnostic
        assert calls == []


def test_preflight_auth_failure(app_client):
    from app.db.session import engine
    from app.services.autonomous import preflight

    async def provider(**kw):
        raise RuntimeError(f"HTTP 401 Unauthorized: invalid api key {SECRET}")

    with Session(engine) as s:
        cfg = _cfg(s)
        res = _run(s, cfg.id, provider_call=provider, list_models=_models_ok)
        assert not res.passed and res.failure_category == preflight.AUTH_FAILED
        assert res.check("text_generation").passed is False and res.check("structured_output").skipped
        assert SECRET not in res.diagnostic and "***" in res.diagnostic


def test_preflight_model_not_found_from_list_and_from_generation(app_client):
    from app.db.session import engine
    from app.services.autonomous import preflight

    async def listing(cfg, timeout):
        return ["some-other-model"]

    async def provider(**kw):
        raise RuntimeError("HTTP 404: model not found")

    with Session(engine) as s:
        cfg = _cfg(s)
        res = _run(s, cfg.id, provider_call=_good_provider(), list_models=listing)
        assert not res.passed and res.failure_category == preflight.MODEL_NOT_FOUND and res.check("model_availability").detail["models_listed"] == 1
        cfg2 = _cfg(s, provider="authnd", model_name="moonshotai/kimi-k3", api_key="", api_base=None)
        res2 = _run(s, cfg2.id, provider_call=provider)
        assert res2.check("model_availability").skipped and not res2.passed and res2.failure_category == preflight.MODEL_NOT_FOUND


def test_preflight_timeout(app_client):
    from app.db.session import engine
    from app.services.autonomous import preflight

    async def slow_list(cfg, timeout):
        raise asyncio.TimeoutError()

    async def slow_call(**kw):
        raise asyncio.TimeoutError()

    with Session(engine) as s:
        cfg = _cfg(s)
        res = _run(s, cfg.id, provider_call=_good_provider(), list_models=slow_list)
        assert not res.passed and res.failure_category == preflight.TIMEOUT and res.check("model_availability").category == preflight.TIMEOUT
        res = _run(s, cfg.id, provider_call=slow_call, list_models=_models_ok)
        assert not res.passed and res.failure_category == preflight.TIMEOUT and res.check("text_generation").category == preflight.TIMEOUT


def test_preflight_malformed_and_valid_structured_output(app_client):
    from app.db.session import engine
    from app.services.autonomous import preflight
    from app.services.autonomous.model_client import ProviderResult

    async def malformed(**kw):
        if kw["schema"] is None:
            return ProviderResult(content="OK", input_tokens=1, output_tokens=1)
        return ProviderResult(content='{"ok": "yes-but-string", "echo": 12}', input_tokens=1, output_tokens=1)

    async def wrong_echo(**kw):
        if kw["schema"] is None:
            return ProviderResult(content="OK", input_tokens=1, output_tokens=1)
        return ProviderResult(content={"ok": True, "echo": "something else"}, input_tokens=1, output_tokens=1)

    async def json_text(**kw):
        if kw["schema"] is None:
            return ProviderResult(content="OK", input_tokens=1, output_tokens=1)
        return ProviderResult(content='{"ok": true, "echo": "preflight"}', input_tokens=1, output_tokens=1)

    with Session(engine) as s:
        cfg = _cfg(s)
        res = _run(s, cfg.id, provider_call=malformed, list_models=_models_ok)
        assert not res.passed and res.failure_category == preflight.STRUCTURED_INVALID and res.check("text_generation").passed
        res = _run(s, cfg.id, provider_call=wrong_echo, list_models=_models_ok)
        assert not res.passed and res.failure_category == preflight.STRUCTURED_INVALID
        res = _run(s, cfg.id, provider_call=json_text, list_models=_models_ok)
        assert res.passed and res.check("structured_output").passed


def test_preflight_missing_usage_metadata_is_a_warning(app_client):
    from app.db.session import engine

    with Session(engine) as s:
        cfg = _cfg(s)
        res = _run(s, cfg.id, provider_call=_good_provider(usage=False), list_models=_models_ok)
        assert res.passed  # usage is a warning, not a failure
        assert res.usage_reporting == "missing" and res.check("usage_metadata").passed is False and res.check("usage_metadata").advisory
        assert any("did not report token usage" in w for w in res.warnings)


def test_preflight_fallback_pass_and_fail(app_client):
    from app.db.session import engine
    from app.services.autonomous import preflight
    from app.services.autonomous.model_client import ProviderResult

    with Session(engine) as s:
        primary = _cfg(s)
        fallback = _cfg(s, model_name="fallback-model")
        res = _run(s, primary.id, fallback_llm_config_id=fallback.id, provider_call=_good_provider(), list_models=_models_ok)
        assert res.passed and res.fallback_checked and res.fallback is not None and res.fallback.passed and res.fallback.model == "fallback-model"
        d = preflight.result_dict(res)
        assert d["fallback"]["passed"] and d["fallback"]["llm_config_id"] == fallback.id

        async def primary_ok_fallback_401(**kw):
            if int(kw["llm_config_id"]) == fallback.id:
                raise RuntimeError("401 unauthorized")
            return ProviderResult(content={"ok": True, "echo": "preflight"} if kw["schema"] else "OK", input_tokens=1, output_tokens=1)

        res = _run(s, primary.id, fallback_llm_config_id=fallback.id, provider_call=primary_ok_fallback_401, list_models=_models_ok)
        assert not res.passed and res.failure_category == preflight.FALLBACK_FAILED and res.fallback.failure_category == preflight.AUTH_FAILED
        assert any("fallback configuration failed" in w for w in res.warnings)
        res = _run(s, primary.id, fallback_llm_config_id=fallback.id, check_fallback=False, provider_call=primary_ok_fallback_401, list_models=_models_ok)
        assert res.passed and not res.fallback_checked and any("not checked" in w for w in res.warnings)
        broken = _cfg(s, api_key="")
        res = _run(s, primary.id, fallback_llm_config_id=broken.id, provider_call=_good_provider(), list_models=_models_ok)
        assert not res.passed and res.failure_category == preflight.CONFIG_INVALID and "fallback:" in res.diagnostic


def test_preflight_redacts_secrets_everywhere(app_client):
    from app.db.session import engine
    from app.services.autonomous import preflight

    async def leaky(**kw):
        raise RuntimeError(f"connection refused while sending Authorization: Bearer {SECRET} to host")

    with Session(engine) as s:
        cfg = _cfg(s)
        res = _run(s, cfg.id, provider_call=leaky, list_models=_models_ok)
        assert not res.passed
        blob = repr(preflight.result_dict(res))
        assert SECRET not in blob
        assert res.failure_category == preflight.UNREACHABLE


def test_preflight_endpoint_and_create_job_validation(app_client):
    """The HTTP route exists and static validation gates job creation."""
    import base64

    from app.db.session import engine
    from app.services.autonomous import preflight

    with Session(engine) as s:
        cfg = _cfg(s)
        broken = _cfg(s, api_base="ftp://nope")
        cid, bid = cfg.id, broken.id
    r = app_client.post("/api/autonomous/preflight", json={"llm_config_id": 999_999})
    assert r.status_code == 404
    r = app_client.post("/api/autonomous/preflight", json={"llm_config_id": cid, "timeout_seconds": 1})
    assert r.status_code == 422  # bounded by the request schema
    r = app_client.post("/api/autonomous/preflight", json={"llm_config_id": bid, "timeout_seconds": 5})
    assert r.status_code == 200
    body = r.json()
    assert body["passed"] is False and body["failure_category"] == preflight.CONFIG_INVALID and SECRET not in r.text
    r = app_client.post("/api/autonomous/jobs", json={"filename": "x.txt", "content_base64": base64.b64encode(b"hello").decode(), "llm_config_id": bid})
    assert r.status_code == 400 and "invalid" in r.json()["detail"].lower()
