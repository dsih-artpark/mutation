"""Webhook payload construction, signing and transport.

Replaces the 30-second cron poll: Dharini is told when a run finishes instead of
asking repeatedly whether anything has. The status endpoint remains as a
reconciliation path -- push for latency, pull for certainty.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import socket
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

from .config import (
    PUBLIC_BASE_URL,
    WEBHOOK_ALLOW_PRIVATE_IPS,
    WEBHOOK_ALLOWED_HOSTS,
    WEBHOOK_MAX_INLINE_VARIANTS,
    WEBHOOK_SECRET,
    WEBHOOK_TIMEOUT_S,
)
from .models import Run, WebhookDelivery

SIGNATURE_HEADER = "X-Dharini-Signature"
TIMESTAMP_HEADER = "X-Dharini-Timestamp"
DELIVERY_HEADER = "X-Dharini-Delivery"
EVENT_HEADER = "X-Dharini-Event"


class CallbackRejected(ValueError):
    """The supplied callback URL is not one we are willing to call."""


# --------------------------------------------------------------------------
# URL validation
# --------------------------------------------------------------------------

def validate_callback_url(url: str) -> str:
    """Reject callback URLs that would turn this service into an SSRF pivot.

    The URL arrives from a client, and the worker sits inside the VPC with
    access to internal services and the cloud metadata endpoint. Without this
    check, "notify me at http://169.254.169.254/..." is a credential leak and
    "http://10.0.0.5:6379/" reaches straight into the private network.

    Two independent controls, both applied:

      1. the hostname must be in WEBHOOK_ALLOWED_HOSTS, when configured;
      2. every address it resolves to must be acceptable.

    The address check runs even for allowlisted hosts, so a permitted DNS name
    pointing at an internal address -- whether by rebinding or a misconfigured
    record -- is still refused.
    """
    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        raise CallbackRejected("callback_url must be http or https")
    if not parsed.hostname:
        raise CallbackRejected("callback_url has no host")

    host = parsed.hostname.lower()

    if WEBHOOK_ALLOWED_HOSTS and not any(
        host == allowed or host.endswith("." + allowed) for allowed in WEBHOOK_ALLOWED_HOSTS
    ):
        raise CallbackRejected(f"host '{host}' is not in WEBHOOK_ALLOWED_HOSTS")

    _reject_internal_addresses(host)
    return url


def _reject_internal_addresses(host: str) -> None:
    """Refuse a host resolving to any address we should not be reaching."""
    try:
        resolved = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except socket.gaierror as exc:
        raise CallbackRejected(f"cannot resolve host '{host}': {exc}") from exc

    if not resolved:
        raise CallbackRejected(f"host '{host}' resolved to no addresses")

    for address in resolved:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            raise CallbackRejected(f"host '{host}' resolved to an unparseable address") from None

        # Never permitted, whatever the configuration: loopback reaches this
        # service itself, link-local reaches the cloud metadata endpoint.
        if ip.is_loopback or ip.is_link_local or ip.is_multicast \
                or ip.is_reserved or ip.is_unspecified:
            raise CallbackRejected(f"host '{host}' resolves to a non-routable address ({ip})")

        if ip.is_private and not WEBHOOK_ALLOW_PRIVATE_IPS:
            raise CallbackRejected(
                f"host '{host}' resolves to a private address ({ip}). "
                f"Set WEBHOOK_ALLOW_PRIVATE_IPS=true if the consumer is genuinely "
                f"inside this network, and pair it with WEBHOOK_ALLOWED_HOSTS."
            )


# --------------------------------------------------------------------------
# Payload
# --------------------------------------------------------------------------

def build_payload(run: Run, delivery: WebhookDelivery, variants: list[dict] | None = None) -> dict:
    """Metadata plus parsed variants inline; bulk files by URL.

    Bounded on purpose. Embedding raw VCF text made payload size scale with
    variant count, which for a deep FASTQ run means a multi-megabyte POST that
    then has to survive six retries.
    """
    variants = variants or []
    truncated = len(variants) > WEBHOOK_MAX_INLINE_VARIANTS

    return {
        "event": delivery.event,
        "delivery_id": delivery.id,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        # Column defaults are applied at INSERT, so a delivery that has not been
        # flushed yet still has attempts=None.
        "attempt": (delivery.attempts or 0) + 1,
        "run": {
            "id": run.id,
            "pipeline_id": run.pipeline_id,
            "pipeline_version": run.pipeline_version,
            "status": run.status.value,
            "params": run.params,
            "input_filename": run.input_filename,
            "input_bytes": run.input_bytes,
            "created_at": _iso(run.created_at),
            "started_at": _iso(run.started_at),
            "finished_at": _iso(run.finished_at),
            "duration_s": run.duration_s,
            "error": (
                {"stage": run.error_stage, "message": run.error_message}
                if run.error_message else None
            ),
        },
        "metrics": run.metrics or {},
        "stages": [
            {
                "name": stage.name,
                "status": stage.status.value,
                "duration_s": stage.duration_s,
                "peak_memory_mb": stage.peak_memory_mb,
            }
            for stage in run.stages
        ],
        "variants": variants[:WEBHOOK_MAX_INLINE_VARIANTS],
        "variants_truncated": truncated,
        "variants_total": len(variants),
        "artifacts": [
            {
                "kind": artifact.kind,
                "label": artifact.label,
                "filename": artifact.filename,
                "size_bytes": artifact.size_bytes,
                "content_type": artifact.content_type,
                "url": f"{PUBLIC_BASE_URL}/v1/runs/{run.id}/artifacts/{artifact.filename}",
            }
            for artifact in run.artifacts
        ],
    }


def sign(body: bytes, timestamp: str, secret: str = "") -> str:
    """HMAC-SHA256 over `timestamp.body`.

    Including the timestamp lets the receiver reject replayed deliveries; a
    signature over the body alone stays valid forever.
    """
    secret = secret or WEBHOOK_SECRET
    if not secret:
        return ""
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


# --------------------------------------------------------------------------
# Transport
# --------------------------------------------------------------------------

def post(url: str, payload: dict, delivery_id: str, event: str) -> tuple[int | None, str | None]:
    """Deliver once. Returns (status_code, error). Never raises.

    Re-validates the URL immediately before connecting. The submission-time
    check can be hours stale by the last retry, and a DNS record that pointed
    somewhere public then may point inside the network now.
    """
    try:
        validate_callback_url(url)
    except CallbackRejected as exc:
        # Reported as an ordinary failure, so the normal backoff applies. That
        # is deliberate: if the operator fixes the DNS record or the allowlist
        # during the retry window, a later attempt succeeds. No request is made
        # in the meantime -- each retry costs one resolution.
        return None, f"callback rejected at delivery time: {exc}"

    body = json.dumps(payload, separators=(",", ":"), default=str).encode()
    timestamp = str(int(datetime.now(timezone.utc).timestamp()))

    headers = {
        "Content-Type": "application/json",
        EVENT_HEADER: event,
        DELIVERY_HEADER: delivery_id,
        TIMESTAMP_HEADER: timestamp,
        "User-Agent": "dharini-pipeline/1.0",
    }
    signature = sign(body, timestamp)
    if signature:
        headers[SIGNATURE_HEADER] = signature

    try:
        response = httpx.post(
            url, content=body, headers=headers,
            timeout=WEBHOOK_TIMEOUT_S, follow_redirects=False,
        )
    except httpx.HTTPError as exc:
        return None, f"{type(exc).__name__}: {exc}"

    if 200 <= response.status_code < 300:
        return response.status_code, None
    return response.status_code, f"HTTP {response.status_code}: {response.text[:500]}"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None
