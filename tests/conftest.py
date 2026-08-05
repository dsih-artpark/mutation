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
    if not docker_available():
        pytest.skip("Docker is not available")
