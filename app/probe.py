"""Can this process actually reach the object store?

Its own module because two routes need it and they need different amounts of
it: `/health` is unauthenticated and gets the exception TYPE, `/v1/system` sits
behind the credential and gets the full message. Importing `main` from a router
would be circular, so the probe lives here and both import it.
"""

from __future__ import annotations

import time

from .config import S3_ALLOWED_PREFIX
from .storage import get_store

# Probing costs a network round trip and a load balancer hits /health every few
# seconds. Cached briefly so the probe is real without being a self-inflicted
# request flood.
PROBE_TTL_S = 30.0

_cache: tuple[float, dict] | None = None


def storage_probe() -> dict:
    """`{"reachable": bool}`, plus `error` and `error_type` when it is not.

    A HEAD on a key that does not exist is enough: it exercises credential
    resolution and bucket reachability without needing an object to be there.
    Under a policy without ListBucket a missing object answers 403 rather than
    404 -- and storage.head already reads both as absence -- so "reachable" is
    the meaningful signal either way.

    Never raises. Losing the probe must not take the health endpoint with it.
    """
    global _cache

    now = time.monotonic()
    if _cache and now - _cache[0] < PROBE_TTL_S:
        return _cache[1]

    try:
        get_store().head(f"{S3_ALLOWED_PREFIX}.healthcheck-probe")
        result = {"reachable": True}
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        result = {
            "reachable": False,
            "error": f"{type(exc).__name__}: {exc}",
            "error_type": type(exc).__name__,
        }

    _cache = (now, result)
    return result


def reset() -> None:
    """Drop the cached result. For tests, and for a caller that has just
    changed configuration and wants the next read to be true."""
    global _cache
    _cache = None
