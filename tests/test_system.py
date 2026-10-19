"""/health is the route that ends up exposed. /v1/system is where the
descriptive detail lives instead."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    """conftest sets API_KEYS=test:test-key, so /v1 routes need the header.
    /health does not, which is half of what this file is about."""
    with TestClient(app) as c:
        c.headers.update({"X-API-Key": "test-key"})
        yield c


# --------------------------------------------------------------------------
# /health leaks nothing that helps someone aim
# --------------------------------------------------------------------------

def test_health_does_not_name_the_bucket_or_its_prefixes(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert set(body["storage"]) <= {"backend", "reachable", "error"}
    assert "bucket" not in body["storage"]
    assert "input_prefix" not in body["storage"]
    assert "output_prefix" not in body["storage"]


def test_health_keeps_what_an_operator_reads_at_2am(client):
    """Liveness alone cannot tell "the process is dead" apart from "the process
    is fine but cannot reach S3" apart from "the schema is behind the code"."""
    body = client.get("/health").json()
    assert body["storage"]["reachable"] is True
    assert body["schema_revision"]
    assert body["disk"]["free_gb"] >= 0


def test_a_storage_failure_reports_the_type_and_not_the_message(client, monkeypatch):
    """A botocore message carries the bucket, the key and sometimes the account
    id, and this endpoint is unauthenticated."""
    import app.probe

    app.probe.reset()
    monkeypatch.setattr(
        app.probe, "get_store",
        lambda: (_ for _ in ()).throw(RuntimeError("bucket secret-bucket-name key uploads/x")),
    )
    try:
        storage = client.get("/health").json()["storage"]
        assert storage["reachable"] is False
        assert storage["error"] == "RuntimeError"
        assert "secret-bucket-name" not in str(storage)

        # ...and the full message IS available, behind the credential.
        detailed = client.get("/v1/system").json()["storage"]
        assert "secret-bucket-name" in detailed["error"]
    finally:
        app.probe.reset()


# --------------------------------------------------------------------------
# /v1/system carries it instead
# --------------------------------------------------------------------------

def test_system_reports_the_effective_prefixes(client):
    # The diagnostic that separates a prefix mismatch from a typo'd key: the
    # service cannot list the bucket, so 403 and 404 are deliberately conflated
    # and both surface as "does not exist".
    storage = client.get("/v1/system").json()["storage"]
    assert storage["input_prefix"].endswith("uploads/")
    assert storage["output_prefix"].endswith("runs/")
    assert storage["bucket"]


def test_system_names_the_image_every_pipeline_resolved(client):
    body = client.get("/v1/system").json()
    by_id = {p["id"]: p for p in body["pipelines"]}
    # A superset: other test modules register synthetic pipelines into the same
    # process-wide registry, and asserting equality makes this pass alone and
    # fail in the suite.
    assert {"ont-lsdv", "snippy-fasta"} <= set(by_id)
    assert by_id["ont-lsdv"]["version"]
    # fetch/publish bookend every pipeline's own declared list.
    assert by_id["ont-lsdv"]["stages"][0] == "fetch"
    assert by_id["ont-lsdv"]["stages"][-1] == "publish"
    assert by_id["ont-lsdv"]["images"], "a pipeline that resolves no image cannot run"


def test_system_reports_the_limits_that_decide_whether_a_run_is_accepted(client):
    limits = client.get("/v1/system").json()["limits"]
    assert limits["worker_concurrency"] >= 1
    assert limits["max_input_bytes"] > 0
    assert "require_pinned_images" in limits


def test_system_needs_a_credential_and_health_does_not():
    """The whole point of the split. /health is what a load balancer and an
    exposed port reach; /v1/system is what a credential reaches."""
    with TestClient(app) as anonymous:
        assert anonymous.get("/health").status_code == 200
        assert anonymous.get("/v1/system").status_code == 401
