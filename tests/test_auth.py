"""Credential verification.

Every negative case here is a way JWT verification is classically got wrong.
"""

from __future__ import annotations

import time

import jwt
import pytest
from fastapi import FastAPI, Depends
from fastapi.testclient import TestClient

from app.auth import Principal, require_principal, require_service

SECRET = "test-jwt-secret"
ISS = "dharini-test"
AUD = "pipeline-test"


def _token(**overrides) -> str:
    claims = {
        "sub": "u17", "iss": ISS, "aud": AUD,
        "iat": int(time.time()), "exp": int(time.time()) + 300,
    }
    claims.update(overrides)
    key = overrides.pop("_key", SECRET)
    return jwt.encode(claims, key, algorithm=overrides.pop("_alg", "HS256"))


@pytest.fixture()
def client():
    app = FastAPI()

    @app.get("/read")
    def read(principal: Principal = Depends(require_principal)):
        return {"kind": principal.kind, "label": principal.label,
                "owner_id": principal.owner_id}

    @app.get("/write")
    def write(principal: Principal = Depends(require_service)):
        return {"label": principal.label}

    return TestClient(app)


# ------------------------------------------------------------ service key

def test_a_valid_api_key_authenticates_as_a_service(client):
    response = client.get("/read", headers={"X-API-Key": "test-key"})
    assert response.status_code == 200
    assert response.json() == {"kind": "service", "label": "test", "owner_id": None}


def test_a_wrong_api_key_is_401(client):
    assert client.get("/read", headers={"X-API-Key": "nope"}).status_code == 401


def test_no_credential_is_401(client):
    assert client.get("/read").status_code == 401


# -------------------------------------------------------------- user token

def test_a_valid_bearer_token_authenticates_as_its_subject(client):
    response = client.get("/read", headers={"Authorization": f"Bearer {_token()}"})
    assert response.status_code == 200
    assert response.json() == {"kind": "user", "label": "u17", "owner_id": "u17"}


def test_a_bearer_token_may_not_write(client):
    """Submission is service-only: callback_url is trust-sensitive."""
    response = client.get("/write", headers={"Authorization": f"Bearer {_token()}"})
    assert response.status_code == 403


def test_a_service_key_may_write(client):
    assert client.get("/write", headers={"X-API-Key": "test-key"}).status_code == 200


@pytest.mark.parametrize("token_kwargs,why", [
    # DHARINI_JWT_LEEWAY_S defaults to 30 (spec SS17), so the offset here has
    # to clear that tolerance or the token is legitimately still valid.
    ({"exp": int(time.time()) - 40},        "expired"),
    ({"iss": "somebody-else"},              "wrong issuer"),
    ({"aud": "another-service"},            "wrong audience"),
    ({"_key": "a-different-secret"},        "signed by an unrelated key"),
])
def test_invalid_tokens_are_401(client, token_kwargs, why):
    response = client.get("/read",
                          headers={"Authorization": f"Bearer {_token(**token_kwargs)}"})
    assert response.status_code == 401, why


def test_an_unsigned_token_is_rejected(client):
    """alg:none is the oldest JWT attack. jwt.decode must be given an explicit
    algorithm list so the token cannot choose its own verification."""
    unsigned = jwt.encode({"sub": "u17", "iss": ISS, "aud": AUD,
                           "exp": int(time.time()) + 300},
                          key="", algorithm="none")
    response = client.get("/read", headers={"Authorization": f"Bearer {unsigned}"})
    assert response.status_code == 401


def test_a_token_missing_sub_is_rejected(client):
    """Without a subject there is no owner to scope reads to, so accepting it
    would silently grant access to every run."""
    bad = jwt.encode({"iss": ISS, "aud": AUD, "exp": int(time.time()) + 300},
                     SECRET, algorithm="HS256")
    assert client.get("/read", headers={"Authorization": f"Bearer {bad}"}).status_code == 401


def test_a_malformed_bearer_value_is_401(client):
    assert client.get("/read", headers={"Authorization": "Bearer not.a.jwt"}).status_code == 401
    assert client.get("/read", headers={"Authorization": "Basic abc"}).status_code == 401


# --------------------------------------------------------------- timing

def test_api_keys_are_compared_in_constant_time():
    """A `==` on a secret leaks it one byte at a time under timing analysis."""
    import inspect

    import app.auth

    source = inspect.getsource(app.auth)
    assert "compare_digest" in source


# ------------------------------------------------------------------ CORS

def test_the_app_does_not_allow_every_origin():
    import app.main

    for middleware in app.main.app.user_middleware:
        assert middleware.kwargs.get("allow_origins") != ["*"], (
            "allow_origins=['*'] with credentials is exactly the configuration "
            "that lets any page read authenticated responses."
        )


# ------------------------------------------------------- ownership scoping

def _bearer(sub: str) -> dict[str, str]:
    token = jwt.encode(
        {"sub": sub, "iss": ISS, "aud": AUD,
         "iat": int(time.time()), "exp": int(time.time()) + 300},
        SECRET, algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def client_with_runs():
    """A real app -- not the toy one above -- with one run owned by u17 and
    one owned by u99, so ownership scoping can be exercised through HTTP."""
    import uuid

    from fastapi.testclient import TestClient

    from app.config import RUNS_ROOT
    from app.db import session_scope
    from app.main import app
    from app.models import Run, RunArtifact, RunStatus
    from app.runtime.context import RunPaths

    def _make_run(owner_id: str) -> str:
        with session_scope() as session:
            run = Run(
                pipeline_id="test", pipeline_version="1.0.0",
                # external_id is NOT NULL and unique; no real caller-supplied
                # id exists here, so a fresh uuid4 satisfies the constraint
                # the same way app/api/runs.py's create_run does.
                external_id=str(uuid.uuid4()),
                status=RunStatus.SUCCEEDED, params={}, owner_id=owner_id,
            )
            session.add(run)
            session.flush()
            return run.id

    mine = _make_run("u17")
    theirs = _make_run("u99")

    # A real artifact row *and* a real file on disk. Without the file on disk,
    # a cross-owner download would 404 for the wrong reason (missing file)
    # even before ownership scoping exists, and the test guarding against it
    # would pass vacuously.
    paths = RunPaths.for_run(RUNS_ROOT, theirs).create()
    (paths.output / "annotated.vcf").write_text("##fileformat=VCFv4.2\n")
    with session_scope() as session:
        session.add(RunArtifact(
            run_id=theirs, kind="vcf", label="Annotated variants",
            filename="annotated.vcf", size_bytes=22, content_type="text/plain",
        ))

    test_client = TestClient(app)
    return test_client, mine, theirs


def test_a_user_may_read_their_own_run(client_with_runs):
    client, mine, _theirs = client_with_runs
    response = client.get(f"/v1/runs/{mine}", headers=_bearer("u17"))
    assert response.status_code == 200


def test_a_user_reading_someone_elses_run_gets_404_not_403(client_with_runs):
    """403 confirms the run exists. Enumerating ids should yield nothing."""
    client, _mine, theirs = client_with_runs
    response = client.get(f"/v1/runs/{theirs}", headers=_bearer("u17"))
    assert response.status_code == 404


def test_listing_runs_under_a_user_token_shows_only_their_own(client_with_runs):
    client, mine, theirs = client_with_runs
    ids = {r["id"] for r in client.get("/v1/runs", headers=_bearer("u17")).json()}
    assert mine in ids and theirs not in ids


def test_a_service_key_sees_every_run(client_with_runs):
    client, mine, theirs = client_with_runs
    ids = {r["id"] for r in client.get("/v1/runs",
                                       headers={"X-API-Key": "test-key"}).json()}
    assert {mine, theirs} <= ids


def test_a_user_may_not_download_someone_elses_artifact(client_with_runs):
    client, _mine, theirs = client_with_runs
    response = client.get(f"/v1/runs/{theirs}/artifacts/annotated.vcf",
                          headers=_bearer("u17"))
    assert response.status_code == 404


def test_every_run_read_route_enforces_owner_scoping():
    """Driven by the router's own table, so a route added later fails this test
    until it is handled. Ownership checks are easy to add to the obvious
    endpoint and forget on the next one."""
    from app.api.runs import router

    unscoped = []
    for route in router.routes:
        if "GET" not in getattr(route, "methods", set()):
            continue
        source = route.endpoint.__code__.co_names
        if "_visible_run" not in source and "_visible_runs" not in source:
            unscoped.append(route.path)

    assert unscoped == [], f"GET routes without owner scoping: {unscoped}"


# ------------------------------------------------------- hostile header bytes

@pytest.mark.parametrize("raw", [
    b"caf\xe9",          # a single high byte
    b"\xff\xfe\x00",     # arbitrary binary
    b"\xc3\xa9" * 200,   # long and non-ASCII
])
def test_a_non_ascii_api_key_is_401_not_500(raw):
    """Regression: hmac.compare_digest refuses two str arguments unless both
    are ASCII, and Starlette decodes header bytes as latin-1. Comparing as str
    raised TypeError out of an unauthenticated code path, so any client could
    force a 500 with one header byte.
    """
    from fastapi import HTTPException
    from starlette.requests import Request

    from app.auth import require_principal

    request = Request({"type": "http", "headers": [(b"x-api-key", raw)]})
    with pytest.raises(HTTPException) as caught:
        require_principal(request)
    assert caught.value.status_code == 401


def test_a_non_ascii_bearer_token_is_401_not_500():
    from fastapi import HTTPException
    from starlette.requests import Request

    from app.auth import require_principal

    request = Request({
        "type": "http",
        "headers": [(b"authorization", b"Bearer caf\xe9.\xff\xfe.sig")],
    })
    with pytest.raises(HTTPException) as caught:
        require_principal(request)
    assert caught.value.status_code == 401


def test_the_jwks_client_is_given_a_timeout():
    """PyJWKClient defaults to urllib's timeout, which is none at all. A JWKS
    host that accepts the connection then stalls would pin a worker thread per
    request until the pool is gone -- an auth dependency taking the API down."""
    from app.auth import JWKS_TIMEOUT_S

    assert 0 < JWKS_TIMEOUT_S <= 10
