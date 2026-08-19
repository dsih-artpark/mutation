"""Webhook delivery: signing, SSRF rejection, retry, and recovery paths."""

from datetime import timedelta
from unittest.mock import patch
import uuid

import httpx
import pytest

from app.config import WEBHOOK_BACKOFF_S
from app.db import session_scope
from app.delivery import CallbackRejected, sign, validate_callback_url
from app.models import DeliveryStatus, Run, RunStatus, WebhookDelivery, utcnow


# ---------------------------------------------------------------- fixtures

def make_run(status=RunStatus.SUCCEEDED, url="https://consumer.example.org/hook"):
    with session_scope() as session:
        run = Run(
            pipeline_id="test", pipeline_version="1.0.0", status=status,
            # external_id is NOT NULL and unique; this fixture has no real
            # caller-supplied id to key off, so a fresh uuid4 satisfies the
            # constraint the same way app/api/runs.py's create_run does.
            external_id=str(uuid.uuid4()),
            callback_url=url, params={}, metrics={"variant_count": 3},
            started_at=utcnow(), finished_at=utcnow(),
        )
        session.add(run)
        session.flush()
        return run.id


def make_delivery(run_id, max_attempts=6):
    with session_scope() as session:
        delivery = WebhookDelivery(
            run_id=run_id, url="https://consumer.example.org/hook",
            event="run.completed", max_attempts=max_attempts,
            attempts=0, next_attempt_at=utcnow(),
        )
        session.add(delivery)
        session.flush()
        return delivery.id


class _Response:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


@pytest.fixture
def resolvable_host():
    """Stub DNS for the transport tests.

    They exercise retry and persistence semantics, not URL validation, and the
    test hostname deliberately does not resolve. Scheme and allowlist checks
    stay live; only the address lookup is stubbed. SSRF behaviour has its own
    tests below.
    """
    with patch("app.delivery._reject_internal_addresses", return_value=None):
        yield


# ------------------------------------------------------------------ SSRF

@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest/meta-data/",   # cloud metadata — credential theft
    "http://127.0.0.1:8000/internal",
    "http://localhost/admin",
    "ftp://example.org/x",
    "not-a-url",
])
def test_dangerous_callback_urls_are_rejected(url):
    with pytest.raises(CallbackRejected):
        validate_callback_url(url)


def test_public_https_url_is_accepted():
    with patch("app.delivery._reject_internal_addresses", return_value=None):
        assert validate_callback_url("https://example.org/hooks/dharini")


@pytest.mark.parametrize("address", [
    "10.0.0.5",        # RFC1918 — the network the worker actually sits in
    "192.168.1.10",
    "172.16.4.4",
    "127.0.0.1",       # loopback — this service itself
    "169.254.169.254",  # link-local — cloud metadata
    "0.0.0.0",         # unspecified
    "224.0.0.1",       # multicast
])
def test_internal_addresses_are_rejected_however_the_name_resolves(address):
    """A public-looking hostname that resolves inwards must still be refused.

    RFC1918 was the gap: blocking only loopback and link-local left every
    internal service on the private network reachable.
    """
    with patch("app.delivery.socket.getaddrinfo",
               return_value=[(2, 1, 6, "", (address, 443))]):
        with pytest.raises(CallbackRejected):
            validate_callback_url("https://totally-normal.example.org/hook")


def test_allowlisted_host_is_still_address_checked():
    """The allowlist must not be a bypass.

    Previously the allowlist branch returned early, so a permitted DNS name
    pointing at an internal address — by rebinding or a bad record — was
    delivered to without any address check.
    """
    with patch("app.delivery.WEBHOOK_ALLOWED_HOSTS", ["dharini.example.org"]):
        with patch("app.delivery.socket.getaddrinfo",
                   return_value=[(2, 1, 6, "", ("10.1.2.3", 443))]):
            with pytest.raises(CallbackRejected, match="private address"):
                validate_callback_url("https://dharini.example.org/hook")


def test_private_addresses_allowed_only_by_explicit_opt_in():
    """Deployments where the consumer genuinely is on the private network."""
    resolved = [(2, 1, 6, "", ("10.1.2.3", 443))]

    with patch("app.delivery.socket.getaddrinfo", return_value=resolved):
        with patch("app.delivery.WEBHOOK_ALLOW_PRIVATE_IPS", True):
            assert validate_callback_url("https://internal.example.org/hook")

        # Loopback and metadata stay blocked even with the opt-in.
        with patch("app.delivery.WEBHOOK_ALLOW_PRIVATE_IPS", True):
            with patch("app.delivery.socket.getaddrinfo",
                       return_value=[(2, 1, 6, "", ("169.254.169.254", 80))]):
                with pytest.raises(CallbackRejected):
                    validate_callback_url("https://internal.example.org/hook")


def test_unresolvable_host_is_rejected():
    import socket as _socket

    with patch("app.delivery.socket.getaddrinfo", side_effect=_socket.gaierror("nope")):
        with pytest.raises(CallbackRejected, match="cannot resolve"):
            validate_callback_url("https://nonexistent.example.org/hook")


def test_url_is_revalidated_at_delivery_time():
    """Submission-time validation can be hours stale by the final retry, so the
    address is checked again immediately before connecting."""
    from app.delivery import post

    with patch("app.delivery.socket.getaddrinfo",
               return_value=[(2, 1, 6, "", ("10.0.0.9", 443))]):
        with patch("app.delivery.httpx.post") as sender:
            status, error = post("https://rebound.example.org/hook", {}, "d-1", "run.completed")

    assert status is None
    assert "rejected at delivery time" in error
    sender.assert_not_called()


# ------------------------------------------------------------- signature

def test_signature_binds_body_and_timestamp():
    base = sign(b'{"a":1}', "1700000000", secret="s")
    assert base == sign(b'{"a":1}', "1700000000", secret="s")
    assert base != sign(b'{"a":2}', "1700000000", secret="s")   # body changed
    assert base != sign(b'{"a":1}', "1700000001", secret="s")   # replay blocked


def test_consumer_can_reproduce_the_signature():
    """The exact check Dharini implements on receipt."""
    import hashlib
    import hmac

    body, timestamp, secret = b'{"run":{"id":"x"}}', "1700000000", "shared"
    header = sign(body, timestamp, secret=secret)
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + body,
                        hashlib.sha256).hexdigest()
    assert hmac.compare_digest(header.split("=", 1)[1], expected)


def test_no_configured_secret_means_no_signature_header():
    """Unsigned delivery is allowed for local development only."""
    with patch("app.delivery.WEBHOOK_SECRET", ""):
        assert sign(b"{}", "1700000000") == ""


# -------------------------------------------------------------- delivery

def test_successful_delivery_is_marked_and_sends_idempotency_headers(resolvable_host):
    from app.tasks import deliver_webhook

    captured = {}

    def fake_post(url, content=None, headers=None, **kwargs):
        captured.update(headers=headers, body=content)
        return _Response(200, "ok")

    delivery_id = make_delivery(make_run())
    with patch("app.delivery.httpx.post", side_effect=fake_post):
        result = deliver_webhook.apply(args=[delivery_id]).get()

    assert result["status"] == "delivered"
    assert captured["headers"]["X-Dharini-Delivery"] == delivery_id
    assert captured["headers"]["X-Dharini-Event"] == "run.completed"
    assert "X-Dharini-Signature" in captured["headers"]

    with session_scope() as session:
        delivery = session.get(WebhookDelivery, delivery_id)
        assert delivery.status == DeliveryStatus.DELIVERED
        assert delivery.attempts == 1


def test_server_error_schedules_a_retry(resolvable_host):
    from app.tasks import deliver_webhook

    delivery_id = make_delivery(make_run())
    scheduled = []

    with patch("app.delivery.httpx.post", return_value=_Response(503, "down")):
        with patch.object(deliver_webhook, "apply_async",
                          side_effect=lambda *a, **k: scheduled.append(k.get("countdown"))):
            result = deliver_webhook.apply(args=[delivery_id]).get()

    assert result["status"] == "retrying"
    assert scheduled == [WEBHOOK_BACKOFF_S[0]]

    with session_scope() as session:
        delivery = session.get(WebhookDelivery, delivery_id)
        assert delivery.status == DeliveryStatus.FAILED
        assert delivery.last_status_code == 503
        assert delivery.next_attempt_at is not None


def test_connection_error_is_retried_not_lost(resolvable_host):
    from app.tasks import deliver_webhook

    delivery_id = make_delivery(make_run())
    scheduled = []

    with patch("app.delivery.httpx.post", side_effect=httpx.ConnectError("refused")):
        with patch.object(deliver_webhook, "apply_async",
                          side_effect=lambda *a, **k: scheduled.append(k.get("countdown"))):
            result = deliver_webhook.apply(args=[delivery_id]).get()

    assert result["status"] == "retrying"
    assert scheduled == [WEBHOOK_BACKOFF_S[0]]


def test_exhausted_delivery_does_not_fail_the_run(resolvable_host):
    """The property that matters most: the computation was valid. Only the
    notification failed, and re-running it would waste the compute."""
    from app.tasks import deliver_webhook

    run_id = make_run()
    delivery_id = make_delivery(run_id, max_attempts=1)

    with patch("app.delivery.httpx.post", return_value=_Response(500, "boom")):
        result = deliver_webhook.apply(args=[delivery_id]).get()

    assert result["status"] == "abandoned"
    with session_scope() as session:
        assert session.get(WebhookDelivery, delivery_id).status == DeliveryStatus.ABANDONED
        assert session.get(Run, run_id).status == RunStatus.SUCCEEDED


# -------------------------------------------------------------- recovery

def test_sweep_rearms_a_delivery_whose_retry_was_lost(resolvable_host):
    """Celery holds a scheduled retry in the broker; a Redis flush loses it."""
    from app.tasks import deliver_webhook, sweep_pending_deliveries

    delivery_id = make_delivery(make_run())
    with session_scope() as session:
        delivery = session.get(WebhookDelivery, delivery_id)
        delivery.status = DeliveryStatus.FAILED
        delivery.next_attempt_at = utcnow() - timedelta(minutes=10)

    requeued = []

    def capture(*args, **kwargs):
        requeued.extend(kwargs.get("args") or (args[0] if args else []))

    with patch.object(deliver_webhook, "apply_async", side_effect=capture):
        result = sweep_pending_deliveries.apply().get()

    assert result["requeued"] >= 1
    assert delivery_id in requeued


def test_retention_never_purges_an_undelivered_result():
    from app.tasks import cleanup_expired_runs

    run_id = make_run()
    with session_scope() as session:
        session.get(Run, run_id).created_at = utcnow() - timedelta(days=30)
    make_delivery(run_id)  # still PENDING

    cleanup_expired_runs.apply(args=[1]).get()

    with session_scope() as session:
        assert session.get(Run, run_id).artifacts_purged is False


def test_payload_is_bounded_and_complete():
    from app.delivery import build_payload

    run_id = make_run()
    delivery_id = make_delivery(run_id)

    with session_scope() as session:
        run = session.get(Run, run_id)
        delivery = session.get(WebhookDelivery, delivery_id)
        _ = run.artifacts, run.stages
        payload = build_payload(run, delivery, [{"pos": 1, "ref": "A", "alt": "G"}])

    assert payload["run"]["id"] == run_id
    assert payload["run"]["status"] == "SUCCEEDED"
    assert payload["metrics"]["variant_count"] == 3
    assert payload["variants_truncated"] is False
    assert payload["attempt"] == 1


def test_payload_truncates_large_variant_sets():
    from app.delivery import build_payload
    from app.config import WEBHOOK_MAX_INLINE_VARIANTS

    run_id = make_run()
    delivery_id = make_delivery(run_id)
    rows = [{"pos": i} for i in range(WEBHOOK_MAX_INLINE_VARIANTS + 500)]

    with session_scope() as session:
        run = session.get(Run, run_id)
        delivery = session.get(WebhookDelivery, delivery_id)
        _ = run.artifacts, run.stages
        payload = build_payload(run, delivery, rows)

    assert payload["variants_truncated"] is True
    assert len(payload["variants"]) == WEBHOOK_MAX_INLINE_VARIANTS
    assert payload["variants_total"] == len(rows)
