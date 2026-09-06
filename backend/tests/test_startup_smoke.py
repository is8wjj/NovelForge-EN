"""Startup smoke: the complete application imports, runs its lifespan, and registers every router.

This is the regression test for the ``preflight`` import failure that made the
whole API unimportable: ``main.app`` must be constructible with all endpoint
modules loaded, and the root / OpenAPI / autonomous preflight routes must exist.
"""

from __future__ import annotations

import importlib
import os
import pkgutil
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

REQUIRED_PATHS = (
    "/api/autonomous/preflight",
    "/api/autonomous/jobs",
    "/api/autonomous/jobs/{job_id}",
    "/api/autonomous/jobs/{job_id}/select",
    "/api/autonomous/jobs/{job_id}/artifacts/{artifact_id}/download",
    "/api/autonomous/jobs/{job_id}/report",
    "/api/llm-configs/",
)


def test_every_endpoint_module_imports():
    import app.api.endpoints as pkg

    failures = {}
    for mod in pkgutil.iter_modules(pkg.__path__):
        try:
            importlib.import_module(f"{pkg.__name__}.{mod.name}")
        except Exception as exc:  # noqa: BLE001 - reported as a test failure with the module name
            failures[mod.name] = f"{type(exc).__name__}: {exc}"
    assert failures == {}


def test_app_starts_and_registers_all_routers(app_client):
    import main

    assert main.app.title.endswith("API")
    r = app_client.get("/")
    assert r.status_code == 200 and "version" in r.json()
    spec = app_client.get("/openapi.json")
    assert spec.status_code == 200
    paths = set(spec.json()["paths"])
    missing = [p for p in REQUIRED_PATHS if p not in paths]
    assert missing == [], missing
    # Every endpoint module contributed at least one route.
    tags = {p.split("/")[2] for p in paths if p.startswith("/api/")}
    for expected in ("autonomous", "projects", "cards", "workflows", "forge", "lab", "bible"):
        assert expected in tags, expected


def test_autonomous_service_package_exposes_preflight_api():
    from app.services.autonomous import preflight

    for name in ("validate_config", "run_preflight", "result_dict"):
        assert callable(getattr(preflight, name))
