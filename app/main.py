"""FastAPI application.

Thin by design: routing, health and startup only. Everything the service
actually does lives in pipelines/ (what to run), runtime/ (how to run it) and
tasks.py (when to run it).
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import legacy_router, pipelines_router, runs_router
from .config import RUNS_ROOT, WORKER_CONCURRENCY
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(pipelines_router)
app.include_router(runs_router)
app.include_router(legacy_router)


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
