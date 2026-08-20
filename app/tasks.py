"""Celery tasks: run execution, webhook delivery, retention."""

from __future__ import annotations

import shutil
import time
import traceback
from datetime import datetime, timedelta, timezone

from .config import (
    MAX_INPUT_BYTES,
    REFS_ROOT,
    S3_UPLOAD_BACKOFF_S,
    S3_UPLOAD_MAX_ATTEMPTS,
    RETENTION_HOURS,
    RUNS_ROOT,
    WEBHOOK_BACKOFF_S,
    WEBHOOK_MAX_ATTEMPTS,
)
from .db import session_scope
from .delivery import build_payload, post
from .storage import StorageError, bucket_name, get_store, output_key
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
from .pipelines.base import FETCH_STAGE, PUBLISH_STAGE
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

    def heartbeat(self, name: str) -> None:
        """Its own short-lived session, per write.

        Sharing a session with the worker thread would let the sampling
        thread hold a connection for the length of a twenty-minute stage
        (G17). Errors are swallowed by the sampler that calls this, not here
        -- so a rollback stays local to this one write.
        """
        with session_scope() as session:
            stage = self._current(session, name)
            if stage:
                stage.last_heartbeat_at = utcnow()

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
    # name. Only a run queued before this deploy carries a separately stored one.
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

    # Publish, not notify. The webhook payload carries object keys, so a
    # delivery sent before the objects exist hands the consumer keys that 404.
    # Called unconditionally: a failed run has no artifacts, and publish then
    # writes its manifest and enqueues the delivery immediately.
    upload_run_artifacts.apply_async(args=[run_id])
    return {"run_id": run_id, "status": status}


def _fetch_inputs(ctx: RunContext, pipeline, input_key: str | None,
                  extra_input_keys: dict[str, str]) -> None:
    """Bring every declared input onto local disk under its normalised name.

    The client's filename never reaches this. Destinations come from
    `pipeline.input_filename` and `extra_inputs[key].filename`, both of which
    the contract test forbids from containing a path separator, so traversal
    via a crafted object key is structurally impossible rather than defended
    against.

    A run with no `input_key` predates object storage and already has its file
    on disk. Submission no longer produces such runs, but a queued one may
    survive the deploy, and re-running it must not fail on a missing key.
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
# Artifact publication
# ==========================================================================

@celery.task(name="app.tasks.upload_run_artifacts", bind=True, acks_late=True)
def upload_run_artifacts(self, run_id: str) -> dict:
    """Publish this run's artifacts to object storage, then notify.

    Deliberately outside execute_run's failure path. A run that computed
    correctly but whose artifacts could not be uploaded is a SUCCEEDED run with
    artifacts_uploaded False -- the same rule WebhookDelivery already follows,
    for the same reason: discarding twenty minutes of Clair3 because S3 blipped
    is the expensive mistake, not the cheap one.
    """
    store = get_store()
    started = time.monotonic()

    with session_scope() as session:
        run = session.get(Run, run_id)
        if run is None:
            return {"run_id": run_id, "status": "unknown_run"}
        if run.artifacts_uploaded:
            return {"run_id": run_id, "status": "already_uploaded"}
        pending = [(a.id, a.filename, a.content_type)
                   for a in run.artifacts if a.uploaded_at is None]
        attempts = run.upload_attempts or 0

    paths = RunPaths.for_run(RUNS_ROOT, run_id)
    uploaded: list[tuple[int, str]] = []
    failure: str | None = None

    for artifact_id, filename, content_type in pending:
        key = output_key(run_id, filename)
        try:
            store.upload(paths.output / filename, key, content_type)
        except (StorageError, OSError) as exc:
            failure = f"{filename}: {exc}"
            break
        uploaded.append((artifact_id, key))

    # Stamped per artifact, so a retry after four of five landed moves one file
    # rather than five. A 3 GB BAM is not re-sent because a 40 kB VCF failed.
    if uploaded:
        with session_scope() as session:
            for artifact_id, key in uploaded:
                artifact = session.get(RunArtifact, artifact_id)
                if artifact:
                    artifact.s3_key = key
                    artifact.uploaded_at = utcnow()

    if failure is None:
        failure = _write_run_manifest(run_id, store)

    countdown: int | None = None
    with session_scope() as session:
        run = session.get(Run, run_id)
        if run is None:
            return {"run_id": run_id, "status": "vanished"}

        run.output_bucket = bucket_name()
        run.upload_attempts = attempts + 1
        elapsed = time.monotonic() - started

        if failure is None:
            run.artifacts_uploaded = True
            run.upload_error = None
            run.next_upload_attempt_at = None
            _record_publish_stage(session, run, StageStatus.SUCCEEDED, elapsed)
        else:
            run.upload_error = failure[:8000]
            if run.upload_attempts >= S3_UPLOAD_MAX_ATTEMPTS:
                run.next_upload_attempt_at = None
                # The run stays SUCCEEDED; only the publication failed.
                _record_publish_stage(session, run, StageStatus.FAILED, elapsed, failure)
            else:
                index = min(run.upload_attempts - 1, len(S3_UPLOAD_BACKOFF_S) - 1)
                countdown = S3_UPLOAD_BACKOFF_S[index]
                run.next_upload_attempt_at = utcnow() + timedelta(seconds=countdown)

    if failure is None:
        _enqueue_delivery(run_id)
        return {"run_id": run_id, "status": "uploaded"}

    if countdown is None:
        # Retries exhausted. The run computed and the results are stranded on
        # this box; the consumer has to be told, or it waits forever for a
        # delivery that will never come. Retention never purges this run.
        print(f"[{run_id[:8]}] artifact upload abandoned: {failure}", flush=True)
        _enqueue_delivery(run_id)
        return {"run_id": run_id, "status": "abandoned", "error": failure}

    upload_run_artifacts.apply_async(args=[run_id], countdown=countdown)
    return {"run_id": run_id, "status": "retrying", "in_s": countdown}


def _record_publish_stage(session, run, status, duration_s, message=None) -> None:
    """Give `publish` a row like any other stage.

    Without this the stage is in the denominator -- `effective_stages` appends
    it -- and nothing ever records it, so a run that finished perfectly reports
    9/10 forever and a UI reads it as stuck. Exactly the drift `_ordinal_of`
    already refuses to allow inside a pipeline; this is the platform's own half
    of the same rule.

    Upserted rather than inserted: an upload that retries five times must not
    leave five rows.
    """
    pipeline = registry.get(run.pipeline_id) if run.pipeline_id in registry else None
    ordinal = len(pipeline.effective_stages) - 1 if pipeline else -1

    stage = (
        session.query(RunStage)
        .filter(RunStage.run_id == run.id, RunStage.name == PUBLISH_STAGE)
        .one_or_none()
    )
    if stage is None:
        stage = RunStage(run_id=run.id, name=PUBLISH_STAGE, ordinal=ordinal,
                         started_at=utcnow())
        session.add(stage)

    stage.status = status
    stage.finished_at = utcnow()
    stage.duration_s = duration_s
    if message:
        stage.message = message[:8000]


def _write_run_manifest(run_id: str, store) -> str | None:
    """Write run.json beside the artifacts. Returns an error string or None.

    The same document the webhook carries. It costs nothing, makes the output
    prefix self-describing to anyone browsing the bucket, and is the recovery
    path if a delivery is abandoned and the webhook is lost for good.
    """
    import json

    with session_scope() as session:
        run = session.get(Run, run_id)
        if run is None:
            return None
        _ = run.artifacts, run.stages
        # An unsaved delivery: build_payload only reads these fields, and a
        # manifest is not a delivery attempt, so persisting one would put a
        # phantom row in the run's delivery history.
        placeholder = WebhookDelivery(
            id="manifest",
            run_id=run_id,
            url="",
            event="run.completed" if run.status == RunStatus.SUCCEEDED else "run.failed",
            attempts=0,
        )
        payload = build_payload(run, placeholder, _read_variant_cache(run_id))

    payload.pop("delivery_id", None)
    payload.pop("attempt", None)
    # Written only after every artifact has landed, so by construction they are
    # uploaded -- the run row simply is not committed until after this returns.
    # Reading the flag straight off the uncommitted row made every manifest in
    # the bucket claim false, which is exactly backwards for the one document
    # someone reads when the webhook was lost.
    payload["artifacts_uploaded"] = True
    payload["artifacts_error"] = None

    path = RunPaths.for_run(RUNS_ROOT, run_id).root / "run.json"
    try:
        path.write_text(json.dumps(payload, indent=2, default=str))
        store.upload(path, output_key(run_id, "run.json"), "application/json")
    except (StorageError, OSError) as exc:
        return f"run.json: {exc}"
    return None


@celery.task(name="app.tasks.sweep_pending_uploads")
def sweep_pending_uploads() -> dict:
    """Re-arm uploads whose retry message was lost, e.g. to a broker restart.

    The same backstop sweep_pending_deliveries provides, for the same reason: a
    scheduled Celery countdown lives in the broker and vanishes with it.
    """
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        overdue = [r.id for r in session.query(Run).filter(
            Run.artifacts_uploaded.is_(False),
            Run.next_upload_attempt_at.isnot(None),
            Run.next_upload_attempt_at <= now,
        ).limit(100).all()]

    for run_id in overdue:
        upload_run_artifacts.apply_async(args=[run_id])
    return {"requeued": len(overdue)}


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

            # Until the upload lands, these local files are the only copy of a
            # successful result. Purging them would destroy it outright.
            #
            # Scoped to runs that were actually meant to upload. A run predating
            # object storage has input_key NULL and artifacts_uploaded False and
            # will never be published by anything -- without this it would be
            # exempt from retention forever, and every historical run's files
            # would sit on the disk permanently. Its contract was the ordinary
            # retention window, and it still is.
            if run.input_key and not run.artifacts_uploaded:
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
