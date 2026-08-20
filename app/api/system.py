"""What this deployment actually is: where it stores things, which pipeline
versions it holds, and which image digests those resolve to.

Split out of `/health` on purpose. `/health` is the one route that ends up
exposed -- load balancer target groups, uptime monitors, container probes all
hit it, and it is the first thing anyone opens to the world. So it carries
liveness and nothing an attacker could use to aim: naming the bucket and its
prefixes there told a passer-by exactly where to point any S3 credential they
later obtained. Everything descriptive lives here instead, behind whatever
credential the deployment is configured with.

There is deliberately no "is this image pulled" field. Answering it means
talking to the Docker daemon, and the API container does not mount the socket
-- only the worker does, because that socket is root-equivalent access to the
host. Mounting it here to satisfy a status page would trade a real security
boundary for a convenience. `make images-list` on the host answers it.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..auth import Principal, require_principal
from ..config import (
    MAX_INPUT_BYTES,
    REQUIRE_PINNED_IMAGES,
    RETENTION_HOURS,
    S3_ALLOWED_PREFIX,
    S3_OUTPUT_PREFIX,
    STORAGE_BACKEND,
    WORKER_CONCURRENCY,
)
from ..db import current_revision
from ..pipelines import registry
from ..storage import bucket_name
from ..probe import storage_probe

router = APIRouter(prefix="/v1/system", tags=["meta"])


@router.get("")
def read_system(principal: Principal = Depends(require_principal)):
    pipelines = []
    for pipeline in registry.all():
        try:
            images = pipeline.required_images()
        except Exception as exc:  # noqa: BLE001 - one bad manifest must not blank the page
            images = [f"unresolved: {type(exc).__name__}: {exc}"]
        pipelines.append({
            "id": pipeline.id,
            "version": pipeline.version,
            "stages": list(pipeline.effective_stages),
            # Digest-pinned under REQUIRE_PINNED_IMAGES, which makes this the
            # answer to "which build of Medaka produced this result" -- the
            # question a variant call is worth nothing without.
            "images": images,
        })

    return {
        "schema_revision": current_revision(),
        "storage": {
            "backend": STORAGE_BACKEND,
            "bucket": bucket_name(),
            "input_prefix": S3_ALLOWED_PREFIX,
            "output_prefix": S3_OUTPUT_PREFIX,
            # The full exception, unlike /health's bare type name. A botocore
            # message names the bucket, the key and sometimes the account id --
            # which is exactly what an operator needs and exactly what an
            # unauthenticated caller should not get.
            "reachable": storage_probe()["reachable"],
            "error": storage_probe().get("error"),
        },
        "pipelines": pipelines,
        "limits": {
            "worker_concurrency": WORKER_CONCURRENCY,
            "max_input_bytes": MAX_INPUT_BYTES,
            "local_retention_hours": RETENTION_HOURS,
            "require_pinned_images": REQUIRE_PINNED_IMAGES,
        },
    }
