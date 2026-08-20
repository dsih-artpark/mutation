"""FastAPI application.

Thin by design: routing, health and startup only. Everything the service
actually does lives in pipelines/ (what to run), runtime/ (how to run it) and
tasks.py (when to run it).
"""

import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import pipelines_router, queue_router, runs_router, system_router
from .auth import require_principal
from .config import (
    CORS_ALLOWED_ORIGINS,
    RUNS_ROOT,
    STORAGE_BACKEND,
    WORKER_CONCURRENCY,
)
from .db import current_revision, init_db
from .pipelines import registry
from .probe import storage_probe


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
app.include_router(system_router, dependencies=[Depends(require_principal)])


@app.get("/", tags=["meta"])
def read_root():
    return {
        "service": "Dharini Pipeline Service",
        "version": app.version,
        "pipelines": [p.id for p in registry.all()],
        "docs": "/docs",
    }


def _storage() -> dict:
    """Backend and reachability. Nothing that helps a caller aim.

    The exception TYPE only -- NoCredentialsError, EndpointConnectionError and
    ClientError already separate the failure modes an operator cares about,
    while the message would hand an unauthenticated caller the bucket name, the
    key and sometimes the account id. GET /v1/system has the full text.
    """
    result = {"backend": STORAGE_BACKEND}
    probe = storage_probe()
    result["reachable"] = probe["reachable"]
    if not probe["reachable"]:
        result["error"] = probe["error_type"]
    return result


@app.get("/health", tags=["meta"])
def health_check():
    """Liveness plus the facts worth seeing when something is wrong.

    Always 200 while the process is up. Storage being unreachable is reported
    in the body rather than failing the check: taking every instance out of
    rotation because a dependency blipped turns a degradation into an outage,
    and this endpoint is what an operator reads to find out which it is.
    """
    import shutil

    usage = shutil.disk_usage(RUNS_ROOT)
    return {
        "status": "ok",
        "pipelines": len(registry.all()),
        "worker_concurrency": WORKER_CONCURRENCY,
        # "the migration did not run" and "the code is old" look identical from
        # outside. This is what tells them apart.
        "schema_revision": current_revision(),
        "storage": _storage(),
        "disk": {
            "free_gb": round(usage.free / 10**9, 1),
            "total_gb": round(usage.total / 10**9, 1),
            "used_percent": round(usage.used / usage.total * 100, 1),
        },
    }
