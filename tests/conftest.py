"""Test configuration.

Environment is set before any `app` import, because config.py reads it at
import time and the runs root must not point at real data.
"""

import functools
import os
import shutil
import tempfile

import pytest


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "docker: test executes a real container; skipped when Docker is absent"
    )


@functools.lru_cache(maxsize=1)
def docker_available() -> bool:
    """Probed once, and only when a docker-marked test actually asks.

    Deliberately not evaluated at import: `docker info` costs about a second,
    and the fast suite must not pay it.
    """
    import subprocess

    try:
        return subprocess.run(
            ["docker", "info"], capture_output=True, timeout=15
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False

_TMP = tempfile.mkdtemp(prefix="dharini-tests-")

os.environ.setdefault("RUNS_ROOT", os.path.join(_TMP, "runs"))
os.environ.setdefault("REFS_ROOT", os.path.join(_TMP, "refs"))
os.environ.setdefault("DATABASE_URL", f"sqlite:///{os.path.join(_TMP, 'test.db')}")
os.environ.setdefault("PUBLIC_BASE_URL", "http://testserver")
os.environ.setdefault("WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("STATS_SAMPLE_INTERVAL_S", "0")

# The shared LSDV reference and GFF3 ship with `sha256: ""` because the tool
# that computes and writes those digests is a later phase (spec §5.1). Without
# this the asset contract fails the whole suite over work nobody has been asked
# to do yet. Unset it -- `ALLOW_UNPINNED_ASSETS= pytest tests/` -- to see which
# assets are still unpinned; a release build should leave it unset.
os.environ.setdefault("ALLOW_UNPINNED_ASSETS", "1")

os.makedirs(os.environ["RUNS_ROOT"], exist_ok=True)
os.makedirs(os.environ["REFS_ROOT"], exist_ok=True)


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_TMP, ignore_errors=True)


@pytest.fixture(scope="session", autouse=True)
def _database():
    from app.db import init_db

    init_db()


@pytest.fixture(scope="session", autouse=True)
def _eager_celery():
    """Run tasks inline so tests need no broker."""
    from app.worker import celery

    celery.conf.task_always_eager = True
    celery.conf.task_eager_propagates = False


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


@pytest.fixture
def docker_required():
    """Skip when Docker is absent -- unless REQUIRE_DOCKER says not to.

    Seven of the runner's safety properties only execute with a daemon, so a
    green run proves less than it looks like on a machine without one. CI sets
    REQUIRE_DOCKER=1 to turn that silent skip into a failure.
    """
    if docker_available():
        return
    if os.getenv("REQUIRE_DOCKER", "").lower() in ("1", "true", "yes"):
        pytest.fail("Docker is required (REQUIRE_DOCKER is set) but not available")
    pytest.skip("Docker is not available")
