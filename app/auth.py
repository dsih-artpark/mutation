"""Who is calling, and what they are allowed to do.

Two credentials with different powers. Dharini's server holds an API key and
may submit and administer. A browser presents the Dharini session token it
already has, and may only read runs it owns.

Tokens are verified locally, never introspected over the network: an auth
check that needs a round trip to Dharini fails when Dharini is down, and adds
that latency to every progress poll.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass

import jwt
from fastapi import Depends, HTTPException, Request

from .config import (
    ALLOW_UNAUTHENTICATED,
    API_KEYS,
    BEARER_AUTH_ENABLED,
    DHARINI_JWT_AUD,
    DHARINI_JWT_ISS,
    DHARINI_JWT_JWKS_URL,
    DHARINI_JWT_LEEWAY_S,
    DHARINI_JWT_SECRET,
)

API_KEY_HEADER = "X-API-Key"

# Seconds to wait on the JWKS endpoint. Deliberately short: this is in the path
# of every browser request, and failing fast beats holding a worker.
JWKS_TIMEOUT_S = 5

# Never taken from the token. Letting the token name its own algorithm is
# what makes alg:none and RS256/HS256 confusion possible.
_HS_ALGORITHMS = ["HS256"]
_RS_ALGORITHMS = ["RS256"]


@dataclass(frozen=True)
class Principal:
    kind: str            # "service" | "user"
    label: str           # API key label, or the token subject
    owner_id: str | None # None for a service key: it sees every run


ANONYMOUS = Principal(kind="service", label="anonymous", owner_id=None)


class AuthError(Exception):
    """Verification failed. Deliberately carries no detail to the client."""


# --------------------------------------------------------------------------
# Service key
# --------------------------------------------------------------------------

def verify_api_key(presented: str) -> Principal | None:
    """Match against every configured key in constant time.

    Every key is compared even after a match, so the time taken does not depend
    on which key matched or how far comparison got. `==` on a secret leaks it
    one byte at a time.

    Compared as BYTES, not str. `hmac.compare_digest` refuses two str arguments
    unless both are ASCII, and Starlette decodes header bytes as latin-1 -- so a
    single high byte in the header raised TypeError out of an unauthenticated
    code path and turned into a 500. Any client could trigger it at will.
    """
    candidate = presented.encode("utf-8", "surrogateescape")
    matched: str | None = None
    for label, secret in API_KEYS.items():
        if hmac.compare_digest(candidate, secret.encode()):
            matched = label
    return Principal(kind="service", label=matched, owner_id=None) if matched else None


# --------------------------------------------------------------------------
# User token
# --------------------------------------------------------------------------

_jwks_client: "jwt.PyJWKClient | None" = None


def _signing_key(token: str):
    """Resolve the verification key.

    RS256 via JWKS is preferred: this service then holds only a public key, so
    compromising it cannot mint tokens for Dharini. PyJWKClient caches the key
    set and refetches when an unknown `kid` appears, so rotation on Dharini's
    side needs no deploy here.
    """
    global _jwks_client
    if DHARINI_JWT_JWKS_URL:
        if _jwks_client is None:
            # An explicit timeout: PyJWKClient defaults to urllib's, which is no
            # timeout at all. A JWKS host that accepts the connection and then
            # stalls would otherwise pin a worker thread per request until the
            # pool is gone -- an auth dependency taking the whole API down.
            _jwks_client = jwt.PyJWKClient(
                DHARINI_JWT_JWKS_URL, cache_keys=True, timeout=JWKS_TIMEOUT_S
            )
        return _jwks_client.get_signing_key_from_jwt(token).key, _RS_ALGORITHMS
    return DHARINI_JWT_SECRET, _HS_ALGORITHMS


def verify_bearer(token: str) -> Principal:
    if not BEARER_AUTH_ENABLED:
        raise AuthError("bearer authentication is not configured")

    try:
        key, algorithms = _signing_key(token)
        claims = jwt.decode(
            token,
            key,
            algorithms=algorithms,          # explicit; never from the token
            audience=DHARINI_JWT_AUD,
            issuer=DHARINI_JWT_ISS,
            leeway=DHARINI_JWT_LEEWAY_S,
            options={"require": ["exp", "iss", "aud", "sub"]},
        )
    except Exception as exc:  # PyJWTError and JWKS transport alike
        raise AuthError(str(exc)) from exc

    subject = str(claims.get("sub") or "")
    if not subject:
        # Without a subject there is no owner to scope reads to, and an
        # unscoped user token would see every run in the database.
        raise AuthError("token has no subject")

    return Principal(kind="user", label=subject, owner_id=subject)


# --------------------------------------------------------------------------
# FastAPI dependencies
# --------------------------------------------------------------------------

def require_principal(request: Request) -> Principal:
    """Any valid credential. 401 otherwise.

    The failure carries no detail: distinguishing "unknown key" from "expired
    token" tells an attacker which half of the system to keep probing.
    """
    presented = request.headers.get(API_KEY_HEADER)
    if presented:
        principal = verify_api_key(presented)
        if principal:
            return principal
        raise HTTPException(401, "invalid credential")

    authorization = request.headers.get("Authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() == "bearer" and token:
        try:
            return verify_bearer(token)
        except AuthError:
            raise HTTPException(401, "invalid credential") from None

    if ALLOW_UNAUTHENTICATED and not API_KEYS:
        # Local development only; config.py refuses this combination unless it
        # was chosen explicitly.
        return ANONYMOUS

    raise HTTPException(401, "missing credential")


def require_service(principal: Principal = Depends(require_principal)) -> Principal:
    """A write. Service keys only.

    A browser token must never reach these: `params`, `input_key` and above all
    `callback_url` are trust-sensitive, and letting a browser choose the
    callback URL re-opens the SSRF hole validate_callback_url exists to close,
    this time wearing a valid user's token.
    """
    if principal.kind != "service":
        raise HTTPException(403, "this endpoint requires a service credential")
    return principal
