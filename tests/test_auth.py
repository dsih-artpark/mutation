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
