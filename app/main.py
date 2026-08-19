"""FastAPI application.

Thin by design: routing, health and startup only. Everything the service
actually does lives in pipelines/ (what to run), runtime/ (how to run it) and
tasks.py (when to run it).
"""

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import legacy_router, pipelines_router, queue_router, runs_router
from .auth import require_principal
from .config import CORS_ALLOWED_ORIGINS, RUNS_ROOT, WORKER_CONCURRENCY
from .db import init_db
from .pipelines import registry


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Dharini Pipeline Service",
    version="2.0.0",
    lifespan=lifespan,
    description=(
        "Runs bioinformatics pipelines asynchronously and notifies the caller "
        "by webhook when they finish.\n\n"
        "Submit with `POST /v1/runs`. Discover available pipelines and their "
        "parameter schemas with `GET /v1/pipelines`."
    ),
)

# An explicit allowlist, and only the methods a browser actually uses. The
# previous allow_origins=["*"] with credentials is precisely the configuration
# that lets any page read an authenticated response.
if CORS_ALLOWED_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ALLOWED_ORIGINS,
        allow_credentials=True,
        allow_methods=["GET", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )

app.include_router(pipelines_router, dependencies=[Depends(require_principal)])
app.include_router(runs_router, dependencies=[Depends(require_principal)])
app.include_router(queue_router, dependencies=[Depends(require_principal)])
# Deprecated shim (Task 12 removes it), but /upload creates runs and takes a
# callback_url, so it is exactly as trust-sensitive as /v1/runs and gets the
# same credential requirement rather than being left open on its way out.
app.include_router(legacy_router, dependencies=[Depends(require_principal)])


@app.get("/", tags=["meta"])
def read_root():
    return {
        "service": "Dharini Pipeline Service",
        "version": app.version,
        "pipelines": [p.id for p in registry.all()],
        "docs": "/docs",
    }


@app.get("/health", tags=["meta"])
def health_check():
    """Liveness plus the facts worth seeing when something is wrong."""
    import shutil

    usage = shutil.disk_usage(RUNS_ROOT)
    return {
        "status": "ok",
        "pipelines": len(registry.all()),
        "worker_concurrency": WORKER_CONCURRENCY,
        "disk": {
            "free_gb": round(usage.free / 10**9, 1),
            "total_gb": round(usage.total / 10**9, 1),
            "used_percent": round(usage.used / usage.total * 100, 1),
        },
    }
