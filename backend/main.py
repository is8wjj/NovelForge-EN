import os, sys
from dotenv import load_dotenv

def _load_env_from_nearby():
    candidates = []
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(sys.executable)
        candidates.append(os.path.join(exe_dir, ".env"))
    backend_dir = os.path.abspath(os.path.dirname(__file__))
    candidates.append(os.path.join(backend_dir, ".env"))
    candidates.append(os.path.join(os.getcwd(), ".env"))
    for p in candidates:
        try:
            if os.path.isfile(p):
                load_dotenv(p, override=False)
        except Exception:
            pass

_load_env_from_nearby()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

from app.api.router import api_router
from app.core import settings
from app.core.startup import startup, shutdown


# Use a lifespan event handler
@asynccontextmanager
async def lifespan(app):
    # Execute on startup
    startup()
    
    # [Optimize] Clean up expired workflow run records on startup
    try:
        from app.db.session import engine
        from sqlmodel import Session
        from app.services.workflow.cleanup import cleanup_expired_runs
        
        with Session(engine) as session:
            cleanup_expired_runs(session)
    except Exception as e:
        print(f"Startup cleanup failed: {e}")

    # Autonomous novel jobs are durable: requeue anything a previous process left running.
    try:
        from app.db.session import engine
        from sqlmodel import Session
        from app.services.autonomous.worker import autonomous_worker

        with Session(engine) as session:
            autonomous_worker.recover_on_startup(session)
    except Exception as e:
        print(f"Autonomous job recovery failed: {e}")
        
    yield
    # Execute on shutdown
    shutdown()

# Create the FastAPI application instance and register lifespan
app = FastAPI(
    title=f"{settings.app.app_name} API",
    version=settings.app.app_version,
    openapi_url="/openapi.json",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan
)

# Register the workflow header middleware (register before CORS to ensure response headers are processed by CORS)
from app.core.middleware.workflow import WorkflowHeaderMiddleware
app.add_middleware(WorkflowHeaderMiddleware)

# Configure CORS middleware (local-only policy by default; see AppSettings.cors_origins)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.app.get_cors_origins_list(),
    allow_origin_regex=settings.app.get_cors_origin_regex(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Workflows-Started"],
)

# Include API routes
app.include_router(api_router, prefix=settings.app.api_prefix)


@app.get("/")
def read_root():
    return {
        "message": f"Welcome to {settings.app.app_name} API",
        "version": settings.app.app_version
    }

if __name__ == "__main__":
    import uvicorn
    # Local, single-user application: binds to loopback unless HOST is set explicitly.
    # There is no authentication layer, so exposing it on other interfaces is unsupported.
    if not settings.app.is_loopback_host():
        print(f"WARNING: HOST={settings.app.host} exposes an unauthenticated API beyond this machine; this deployment mode is unsupported.")
    uvicorn.run(
        "main:app",
        host=settings.app.host,
        port=settings.app.port,
        reload=True,
        timeout_graceful_shutdown=1,
    )

