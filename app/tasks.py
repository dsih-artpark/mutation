"""Celery tasks: run execution, webhook delivery, retention."""

from __future__ import annotations

import shutil
import traceback
from datetime import datetime, timedelta, timezone

from .config import (
    MAX_INPUT_BYTES,
    REFS_ROOT,
    RETENTION_HOURS,
    RUNS_ROOT,
    WEBHOOK_BACKOFF_S,
    WEBHOOK_MAX_ATTEMPTS,
)
from .db import session_scope
from .delivery import build_payload, post
from .storage import StorageError, get_store
from .models import (
    DeliveryStatus,
    Run,
    RunArtifact,
    RunStage,
    RunStatus,
    StageStatus,
    WebhookDelivery,
    utcnow,
)
from .pipelines import registry
from .pipelines.base import FETCH_STAGE
from .runtime.context import RunContext, RunPaths
from .runtime.runner import preflight
from .runtime.spec import InputUnavailable, PreflightFailed, StageError
from .worker import celery


# ==========================================================================
# Stage recorder
# ==========================================================================

class DbRecorder:
    """Writes stage lifecycle events straight through to Postgres.

    Each event is its own short transaction so progress is visible to the
    status endpoint while the run is still executing -- a run that has been
    going for six minutes should be able to say which stage it is on.
    """

    def __init__(self, run_id: str):
        self.run_id = run_id

    def stage_started(self, name: str, ordinal: int) -> None:
        with session_scope() as session:
            session.add(RunStage(
                run_id=self.run_id, name=name, ordinal=ordinal,
                status=StageStatus.RUNNING, started_at=utcnow(),
            ))

    def stage_finished(self, name, duration_s, peak_memory_mb, image) -> None:
        with session_scope() as session:
            stage = self._current(session, name)
            if stage:
                stage.status = StageStatus.SUCCEEDED
                stage.finished_at = utcnow()
                stage.duration_s = duration_s
                stage.peak_memory_mb = peak_memory_mb
                stage.image = image

    def stage_failed(self, name, message, exit_code) -> None:
        with session_scope() as session:
            stage = self._current(session, name)
            if stage:
                stage.status = StageStatus.FAILED
                stage.finished_at = utcnow()
                stage.exit_code = exit_code
                stage.message = message[:8000]

    def stage_skipped(self, name, reason) -> None:
        with session_scope() as session:
            session.add(RunStage(
                run_id=self.run_id, name=name, ordinal=-1,
                status=StageStatus.SKIPPED, message=reason[:8000],
                started_at=utcnow(), finished_at=utcnow(),
            ))

    def log(self, message: str) -> None:
        print(f"[{self.run_id[:8]}] {message}", flush=True)

    def _current(self, session, name: str) -> RunStage | None:
        return (
            session.query(RunStage)
            .filter(RunStage.run_id == self.run_id, RunStage.name == name)
            .order_by(RunStage.id.desc())
            .first()
        )


# ==========================================================================
# Run execution
# ==========================================================================

@celery.task(name="app.tasks.execute_run", bind=True, acks_late=True)
def execute_run(self, run_id: str) -> dict:
    """Execute a queued run.

    Deliberately does not re-raise. A pipeline failure is a normal, recordable
    outcome that the consumer must be told about -- not a Celery-level error
    that would retry eight minutes of compute or vanish into a dead-letter
    queue. Only the run record decides success.
    """
    with session_scope() as session:
        run = session.get(Run, run_id)
        if run is None:
            return {"status": "unknown_run", "run_id": run_id}
        if run.status != RunStatus.QUEUED:
            return {"status": "already_started", "run_id": run_id, "state": run.status.value}

        run.status = RunStatus.RUNNING
        run.started_at = utcnow()
        pipeline_id = run.pipeline_id
        params = dict(run.params or {})
        input_filename = run.input_filename
        stored_name = (run.params or {}).get("_input_name")
        extra_inputs = (run.params or {}).get("_extra_inputs", {})
        input_key = run.input_key
        extra_input_keys = dict(run.extra_input_keys or {})

    pipeline = registry.get(pipeline_id)
    paths = RunPaths.for_run(RUNS_ROOT, run_id).create()

    # Strip the reserved bookkeeping keys before the pipeline sees its params.
    clean_params = {k: v for k, v in params.items() if not k.startswith("_")}

    # An object-store run writes every input under the pipeline's own declared
    # name; only the legacy multipart path carries a separately stored name.
    if input_key:
        stored_name = pipeline.input_filename
        extra_inputs = {
            key: pipeline.extra_inputs[key].filename
            for key in extra_input_keys
            if key in pipeline.extra_inputs
        }

    ctx = RunContext(
        run_id=run_id,
        params=clean_params,
        paths=paths,
        refs_dir=REFS_ROOT,
        recorder=DbRecorder(run_id),
        stages=list(pipeline.effective_stages),
        input_name=stored_name or pipeline.input_filename,
        extra_inputs=extra_inputs,
    )

    failure: tuple[str | None, str] | None = None
    try:
        with ctx.stage(FETCH_STAGE):
            preflight(pipeline.required_images(), stage=FETCH_STAGE)
            _fetch_inputs(ctx, pipeline, input_key, extra_input_keys)
        result = pipeline.run(ctx)
    except StageError as exc:
        failure = (getattr(exc, "stage", None), str(exc))
        result = ctx.result
    except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
        failure = (None, f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")
        result = ctx.result

    _persist_result(run_id, pipeline, result, failure, input_filename)
    _purge_work_dir(paths)

    with session_scope() as session:
        run = session.get(Run, run_id)
        status = run.status.value if run else "UNKNOWN"

    _enqueue_delivery(run_id)
    return {"run_id": run_id, "status": status}


def _fetch_inputs(ctx: RunContext, pipeline, input_key: str | None,
                  extra_input_keys: dict[str, str]) -> None:
    """Bring every declared input onto local disk under its normalised name.

    The client's filename never reaches this. Destinations come from
    `pipeline.input_filename` and `extra_inputs[key].filename`, both of which
    the contract test forbids from containing a path separator, so traversal
    via a crafted object key is structurally impossible rather than defended
    against.

    A run with no `input_key` came through the legacy multipart path and
    already has its file on disk. Task 12 removes that path; until then this
    is what lets both submission shapes execute identically.
    """
    if not input_key:
        return

    store = get_store()
    wanted: list[tuple[str, str]] = [(input_key, pipeline.input_filename)]

    for key, object_key in extra_input_keys.items():
        extra = pipeline.extra_inputs.get(key)
        if extra is None:
            raise PreflightFailed(
                FETCH_STAGE,
                f"run supplies extra input {key!r}, which {pipeline.id} does not declare",
            )
        wanted.append((object_key, extra.filename))

    for object_key, filename in wanted:
        try:
            store.download(object_key, ctx.paths.input / filename, MAX_INPUT_BYTES)
        except StorageError as exc:
            # Re-raised as a StageError so the run record attributes it to
            # fetch. A StorageError reaching the generic handler produces a
            # FAILED run with error_stage None, which says nothing about where
            # it broke.
            raise InputUnavailable(FETCH_STAGE, f"{object_key}: {exc}") from exc


def _persist_result(run_id, pipeline, result, failure, input_filename) -> None:
    with session_scope() as session:
        run = session.get(Run, run_id)
        if run is None:
            return

        run.pipeline_version = pipeline.version
        run.finished_at = utcnow()
        run.metrics = result.metrics or {}

        if failure:
            run.status = RunStatus.FAILED
            run.error_stage, run.error_message = failure[0], failure[1][:16000]
        else:
            run.status = RunStatus.SUCCEEDED

        for artifact in result.artifacts:
            if not artifact.path.exists():
                continue
            session.add(RunArtifact(
                run_id=run_id,
                kind=artifact.kind,
                label=artifact.label,
                filename=artifact.path.name,
                size_bytes=artifact.size_bytes,
                content_type=artifact.content_type,
            ))

        # Variants are large and belong in the payload, not in a JSON column;
        # cache them next to the artifacts for redelivery.
        if result.variants:
            _write_variant_cache(run_id, result.variants)

        run.input_filename = input_filename


def _write_variant_cache(run_id: str, variants: list[dict]) -> None:
    import json

    path = RunPaths.for_run(RUNS_ROOT, run_id).root / "variants.json"
    try:
        path.write_text(json.dumps(variants, default=str))
    except OSError as exc:
        print(f"[{run_id[:8]}] could not cache variants: {exc}", flush=True)


def _read_variant_cache(run_id: str) -> list[dict]:
    import json

    path = RunPaths.for_run(RUNS_ROOT, run_id).root / "variants.json"
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return []


def _purge_work_dir(paths: RunPaths) -> None:
    """Scratch is deleted as soon as the run ends. Only files a pipeline
    explicitly published survive, which is what keeps a 320 GB disk viable."""
    try:
        shutil.rmtree(paths.work, ignore_errors=True)
    except OSError as exc:
        print(f"could not remove work dir {paths.work}: {exc}", flush=True)


# ==========================================================================
# Webhook delivery
# ==========================================================================

def _enqueue_delivery(run_id: str) -> None:
    with session_scope() as session:
        run = session.get(Run, run_id)
        if run is None or not run.callback_url:
            return
        delivery = WebhookDelivery(
            run_id=run_id,
            url=run.callback_url,
            event="run.completed" if run.status == RunStatus.SUCCEEDED else "run.failed",
            max_attempts=WEBHOOK_MAX_ATTEMPTS,
            next_attempt_at=utcnow(),
        )
        session.add(delivery)
        session.flush()
        delivery_id = delivery.id

    deliver_webhook.apply_async(args=[delivery_id])


@celery.task(name="app.tasks.deliver_webhook", bind=True, acks_late=True)
def deliver_webhook(self, delivery_id: str) -> dict:
    """Attempt one delivery, rescheduling on failure with backoff.

    A delivery that never succeeds leaves the run SUCCEEDED and the delivery
    ABANDONED. The computation was valid; only the notification failed, and
    /v1/runs/{id} plus the redeliver endpoint remain as recovery paths.
    """
    with session_scope() as session:
        delivery = session.get(WebhookDelivery, delivery_id)
        if delivery is None or delivery.status == DeliveryStatus.DELIVERED:
            return {"delivery_id": delivery_id, "status": "skipped"}

        run = session.get(Run, delivery.run_id)
        if run is None:
            delivery.status = DeliveryStatus.ABANDONED
            delivery.last_error = "run no longer exists"
            return {"delivery_id": delivery_id, "status": "abandoned"}

        # Touch the relationships while the session is open.
        _ = run.artifacts, run.stages
        payload = build_payload(run, delivery, _read_variant_cache(run.id))
        url, event, attempts, max_attempts = (
            delivery.url, delivery.event, delivery.attempts, delivery.max_attempts,
        )

    status_code, error = post(url, payload, delivery_id, event)

    with session_scope() as session:
        delivery = session.get(WebhookDelivery, delivery_id)
        if delivery is None:
            return {"delivery_id": delivery_id, "status": "vanished"}

        delivery.attempts = attempts + 1
        delivery.last_status_code = status_code
        delivery.last_error = error

        if error is None:
            delivery.status = DeliveryStatus.DELIVERED
            delivery.delivered_at = utcnow()
            delivery.next_attempt_at = None
            return {"delivery_id": delivery_id, "status": "delivered", "code": status_code}

        if delivery.attempts >= max_attempts:
            delivery.status = DeliveryStatus.ABANDONED
            delivery.next_attempt_at = None
            print(
                f"[webhook] abandoned {delivery_id} for run {delivery.run_id} "
                f"after {delivery.attempts} attempts: {error}",
                flush=True,
            )
            return {"delivery_id": delivery_id, "status": "abandoned", "error": error}

        index = min(delivery.attempts - 1, len(WEBHOOK_BACKOFF_S) - 1)
        countdown = WEBHOOK_BACKOFF_S[index]
        delivery.status = DeliveryStatus.FAILED
        delivery.next_attempt_at = utcnow() + timedelta(seconds=countdown)

    deliver_webhook.apply_async(args=[delivery_id], countdown=countdown)
    return {"delivery_id": delivery_id, "status": "retrying", "in_s": countdown}


@celery.task(name="app.tasks.sweep_pending_deliveries")
def sweep_pending_deliveries() -> dict:
    """Safety net for deliveries whose retry task was lost.

    Celery holds a scheduled retry in the broker; a Redis flush or a worker
    killed mid-countdown loses it silently. This re-arms anything overdue, so a
    lost message costs minutes rather than the delivery.
    """
    now = datetime.now(timezone.utc)
    requeued = []

    with session_scope() as session:
        overdue = (
            session.query(WebhookDelivery)
            .filter(
                WebhookDelivery.status.in_([DeliveryStatus.PENDING, DeliveryStatus.FAILED]),
                WebhookDelivery.next_attempt_at.isnot(None),
                WebhookDelivery.next_attempt_at <= now,
            )
            .limit(100)
            .all()
        )
        requeued = [d.id for d in overdue]

    for delivery_id in requeued:
        deliver_webhook.apply_async(args=[delivery_id])

    return {"requeued": len(requeued)}


# ==========================================================================
# Retention
# ==========================================================================

@celery.task(name="app.tasks.cleanup_expired_runs")
def cleanup_expired_runs(hours: int | None = None) -> dict:
    """Delete artifacts older than the retention window.

    Run *records* are kept indefinitely -- they are small, and the history is
    the point of a surveillance platform. Only the bulky files go.
    """
    hours = hours or RETENTION_HOURS
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    purged = 0
    with session_scope() as session:
        expired = (
            session.query(Run)
            .filter(
                Run.created_at < cutoff,
                Run.artifacts_purged.is_(False),
                Run.status.in_([RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED]),
            )
            .limit(500)
            .all()
        )

        for run in expired:
            # Never purge a run whose result has not yet reached the consumer.
            undelivered = [
                d for d in run.deliveries
                if d.status in (DeliveryStatus.PENDING, DeliveryStatus.FAILED)
            ]
            if undelivered:
                continue

            run_dir = RUNS_ROOT / run.id
            if run_dir.exists():
                shutil.rmtree(run_dir, ignore_errors=True)
            run.artifacts_purged = True
            purged += 1

    orphans = _sweep_orphan_uploads(cutoff)
    return {"purged_runs": purged, "removed_orphan_uploads": orphans}


def _sweep_orphan_uploads(cutoff: datetime) -> int:
    """Uploads that were staged but never became a run."""
    upload_dir = RUNS_ROOT / "uploads"
    if not upload_dir.exists():
        return 0

    removed = 0
    threshold = cutoff.timestamp()
    for path in upload_dir.iterdir():
        try:
            if path.is_file() and path.stat().st_mtime < threshold:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed
