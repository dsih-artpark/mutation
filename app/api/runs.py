"""Run submission and status.

One generic endpoint for every pipeline. `pipeline_id` selects the
implementation and `params` is validated against that pipeline's declared
schema, so a new pipeline needs no new route.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import FileResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth import Principal, require_principal, require_service
from ..config import MAX_INPUT_BYTES, PUBLIC_BASE_URL, RUNS_ROOT
from ..db import get_session
from ..delivery import CallbackRejected, validate_callback_url
from ..models import DeliveryStatus, Run, RunStatus, WebhookDelivery, utcnow
from ..pipelines import ParamsInvalid, PipelineNotFound, registry
from ..progress import build_progress, queue_snapshot
from ..runtime.context import RunPaths
from ..storage import KeyRejected, StorageError, get_store, validate_input_key
from ..schemas import ProgressOut, RunAccepted, RunDetail, RunRequest, RunSummary

router = APIRouter(prefix="/v1/runs", tags=["runs"])

# ==========================================================================
# Submission
# ==========================================================================

@router.post("", response_model=RunAccepted, status_code=202)
def create_run(
    request: RunRequest,
    response: Response,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_service),
):
    """Queue a run over an object already in storage.

    Every check below runs before anything is enqueued, cheapest first, so the
    common mistakes cost no network call and a bad submission is an error in
    milliseconds rather than a failure twenty minutes into a run.

    Service credential only. `params`, `input_key` and above all `callback_url`
    are trust-sensitive: letting a browser choose the callback URL would
    re-open the SSRF hole validate_callback_url exists to close, wearing a
    valid user's token.
    """
    # 1. Idempotency, before any work at all. A caller that timed out and
    #    retried gets its original run rather than a second hour of compute.
    existing = (
        session.query(Run).filter(Run.external_id == request.external_id).one_or_none()
    )
    if existing is not None:
        response.status_code = 200
        return _accepted(existing, registry.get(existing.pipeline_id), session)

    # 2. Pipeline.
    try:
        pipeline = registry.get(request.pipeline_id)
    except PipelineNotFound:
        known = ", ".join(p.id for p in registry.all())
        raise HTTPException(
            404, f"unknown pipeline '{request.pipeline_id}'. Available: {known}"
        ) from None

    # 3. Params, against the pipeline's own schema.
    try:
        validated = registry.validate_params(request.pipeline_id, request.params)
    except ParamsInvalid as exc:
        raise HTTPException(422, {"message": "invalid params", "errors": exc.errors}) from None

    # 4. Callback URL. Attacker-influenced input reaching a worker inside the VPC.
    callback_url = request.callback_url
    if callback_url:
        try:
            callback_url = validate_callback_url(callback_url)
        except CallbackRejected as exc:
            raise HTTPException(422, f"callback_url rejected: {exc}") from None

    # 5. Keys: well-formed and under the configured prefix.
    try:
        input_key = validate_input_key(request.input_key)
        extra_keys = {
            key: validate_input_key(value)
            for key, value in request.extra_input_keys.items()
        }
    except KeyRejected as exc:
        raise HTTPException(422, f"input key rejected: {exc}") from None

    # 6. Extra inputs the pipeline actually declares.
    _check_extra_inputs(pipeline, extra_keys)

    # 7. Extensions, from the display filename when given, else the key.
    display_name = request.input_filename or input_key.rsplit("/", 1)[-1]
    _check_extension(display_name, pipeline.accepted_extensions, "input")
    for key, object_key in extra_keys.items():
        _check_extension(
            object_key.rsplit("/", 1)[-1], pipeline.extra_inputs[key].extensions, key
        )

    # 8/9. Existence and size. The only network call in the ladder, and the
    #      reason a typo'd key is an instant error instead of a run that queues,
    #      waits behind three others, and dies twenty minutes later.
    input_bytes = _head_or_reject(input_key)
    for object_key in extra_keys.values():
        _head_or_reject(object_key)

    run = Run(
        pipeline_id=pipeline.id,
        pipeline_version=pipeline.version,
        external_id=request.external_id,
        owner_id=request.owner_id,
        submitted_by=principal.label,
        status=RunStatus.QUEUED,
        params=validated,
        input_key=input_key,
        extra_input_keys=extra_keys,
        input_filename=display_name,
        input_bytes=input_bytes,
        callback_url=callback_url,
    )
    session.add(run)

    try:
        session.commit()
    except IntegrityError:
        # Two concurrent submissions of the same external_id. The unique index
        # is the authority, not the SELECT above -- checking first and inserting
        # second is a race, and losing it must still return the original run.
        session.rollback()
        existing = (
            session.query(Run).filter(Run.external_id == request.external_id).one()
        )
        response.status_code = 200
        return _accepted(existing, pipeline, session)

    from ..tasks import execute_run

    execute_run.apply_async(args=[run.id], task_id=run.id)
    return _accepted(run, pipeline, session)


def _accepted(run: Run, pipeline, session: Session) -> RunAccepted:
    queued_before = (
        session.query(Run)
        .filter(Run.status == RunStatus.QUEUED, Run.created_at < run.created_at)
        .count()
    )
    wait = queue_snapshot(session, include_running=False).get("estimated_wait_s")
    return RunAccepted(
        run_id=run.id,
        external_id=run.external_id,
        pipeline_id=pipeline.id,
        pipeline_version=pipeline.version,
        status=run.status.value,
        stages=list(pipeline.effective_stages),
        queue_position=queued_before,
        estimated_start_s=wait,
    )


def _check_extra_inputs(pipeline, supplied: dict[str, str]) -> None:
    """Every supplied extra is declared, and every required one is supplied."""
    declared = pipeline.extra_inputs
    unknown = sorted(set(supplied) - set(declared))
    if unknown:
        raise HTTPException(
            422,
            f"{pipeline.id} declares no extra input(s) {', '.join(unknown)}. "
            f"Declared: {', '.join(sorted(declared)) or '(none)'}",
        )

    missing = sorted(
        key for key, extra in declared.items() if extra.required and key not in supplied
    )
    if missing:
        raise HTTPException(422, f"{pipeline.id} requires extra input(s): {', '.join(missing)}")


def _head_or_reject(key: str) -> int:
    """Confirm the object exists and is within the size ceiling.

    422 rather than 404: the route exists and the request is well-formed but
    unprocessable, and a 404 on a POST reads as a missing endpoint.
    """
    try:
        info = get_store().head(key)
    except StorageError as exc:
        raise HTTPException(422, f"could not read '{key}': {exc}") from None

    if info is None:
        raise HTTPException(422, {"message": "input object not found", "key": key})
    if info.size > MAX_INPUT_BYTES:
        raise HTTPException(
            413,
            f"'{key}' is {info.size / 10**9:.1f} GB, over the "
            f"{MAX_INPUT_BYTES / 10**9:.1f} GB limit",
        )
    return info.size


# ==========================================================================
# Status
# ==========================================================================

@router.get("", response_model=list[RunSummary])
def list_runs(
    pipeline_id: str | None = Query(None),
    status: RunStatus | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_principal),
):
    query = session.query(Run)
    if pipeline_id:
        query = query.filter(Run.pipeline_id == pipeline_id)
    if status:
        query = query.filter(Run.status == status)
    query = _visible_runs(query, principal)

    runs = query.order_by(Run.created_at.desc()).offset(offset).limit(limit).all()
    return [_summary(run) for run in runs]


@router.get("/{run_id}", response_model=RunDetail)
def get_run(
    run_id: str,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_principal),
):
    """Full run state.

    Retained deliberately alongside webhooks. Push gives Dharini low latency;
    this endpoint is how it reconciles after downtime, and how a support
    question about a six-week-old run gets answered.
    """
    run = _visible_run(session, run_id, principal)

    total = len(registry.get(run.pipeline_id).effective_stages) if run.pipeline_id in registry else 0
    done = sum(1 for stage in run.stages if stage.status.value in ("SUCCEEDED", "SKIPPED"))
    current = next((s.name for s in run.stages if s.status.value == "RUNNING"), None)

    detail = _summary(run).model_dump()
    detail.update(
        params={k: v for k, v in (run.params or {}).items() if not k.startswith("_")},
        metrics=run.metrics or {},
        error_stage=run.error_stage,
        error_message=run.error_message,
        artifacts_purged=run.artifacts_purged,
        progress=f"{done}/{total}" + (f" · {current}" if current else ""),
        stages=[
            {
                "name": s.name, "status": s.status.value,
                "started_at": s.started_at, "finished_at": s.finished_at,
                "duration_s": s.duration_s, "peak_memory_mb": s.peak_memory_mb,
                "message": s.message,
            }
            for s in run.stages
        ],
        artifacts=[
            {
                "kind": a.kind, "label": a.label, "filename": a.filename,
                "size_bytes": a.size_bytes, "content_type": a.content_type,
                "url": f"{PUBLIC_BASE_URL}/v1/runs/{run.id}/artifacts/{a.filename}",
            }
            for a in run.artifacts
        ],
        deliveries=[
            {
                "id": d.id, "event": d.event, "status": d.status.value,
                "attempts": d.attempts, "max_attempts": d.max_attempts,
                "last_status_code": d.last_status_code, "last_error": d.last_error,
                "next_attempt_at": d.next_attempt_at, "delivered_at": d.delivered_at,
            }
            for d in run.deliveries
        ],
    )
    return RunDetail(**detail)


@router.get("/{run_id}/progress", response_model=ProgressOut)
def get_progress(
    run_id: str,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_principal),
):
    """Small and fixed-size, built for a 3-5 second poll. `GET /v1/runs/{id}`
    drags params, artifacts and deliveries with it and is the wrong shape to
    hit that often.
    """
    run = _visible_run(session, run_id, principal)
    try:
        pipeline = registry.get(run.pipeline_id)
    except PipelineNotFound:
        raise HTTPException(404, f"pipeline {run.pipeline_id!r} is no longer registered") from None
    return build_progress(session, run, pipeline)


@router.get("/{run_id}/artifacts/{filename}")
def download_artifact(
    run_id: str,
    filename: str,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_principal),
):
    run = _visible_run(session, run_id, principal)

    # Only registered artifacts are served -- the filename never reaches the
    # filesystem unvalidated, so no traversal is possible.
    artifact = next((a for a in run.artifacts if a.filename == filename), None)
    if artifact is None:
        raise HTTPException(404, f"no artifact '{filename}' for this run")

    path = RunPaths.for_run(RUNS_ROOT, run_id).output / artifact.filename
    if not path.exists():
        detail = (
            "artifact was purged by retention"
            if run.artifacts_purged else "artifact file is missing"
        )
        raise HTTPException(410 if run.artifacts_purged else 404, detail)

    return FileResponse(path, filename=f"{run_id}_{artifact.filename}",
                        media_type=artifact.content_type)


@router.post("/{run_id}/redeliver", status_code=202, dependencies=[Depends(require_service)])
def redeliver(run_id: str, session: Session = Depends(get_session)):
    """Re-send the completion webhook.

    The manual recovery path for the case webhooks are worst at: the consumer
    was down for longer than the retry schedule. Service-only: like
    create_run, this is Dharini's server acting, not a browser reading.
    """
    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(404, f"unknown run: {run_id}")
    if not run.is_terminal:
        raise HTTPException(409, f"run is {run.status.value}; nothing to deliver yet")
    if not run.callback_url:
        raise HTTPException(422, "run has no callback_url")

    delivery = WebhookDelivery(
        run_id=run.id,
        url=run.callback_url,
        event="run.completed" if run.status == RunStatus.SUCCEEDED else "run.failed",
        status=DeliveryStatus.PENDING,
        next_attempt_at=utcnow(),
    )
    session.add(delivery)
    session.commit()

    from ..tasks import deliver_webhook

    deliver_webhook.apply_async(args=[delivery.id])
    return {"delivery_id": delivery.id, "status": "queued"}


# ==========================================================================
# Helpers
# ==========================================================================

def _visible_run(session: Session, run_id: str, principal: Principal) -> Run:
    """Fetch a run the caller is allowed to see.

    A run owned by someone else is reported as absent, not forbidden. A 403
    confirms the run exists, which turns id enumeration into a working
    membership oracle.
    """
    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(404, f"unknown run: {run_id}")
    if principal.owner_id is not None and run.owner_id != principal.owner_id:
        raise HTTPException(404, f"unknown run: {run_id}")
    return run


def _visible_runs(query, principal: Principal):
    """Scope a list query to what the caller may see.

    A service key's owner_id is None -- Dharini's server sees every run --
    so the filter keys off the principal, not off the run.
    """
    if principal.owner_id is not None:
        query = query.filter(Run.owner_id == principal.owner_id)
    return query


def _summary(run: Run) -> RunSummary:
    return RunSummary(
        id=run.id,
        pipeline_id=run.pipeline_id,
        pipeline_version=run.pipeline_version,
        status=run.status.value,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        duration_s=run.duration_s,
        input_filename=run.input_filename,
    )


def _check_extension(filename: str, accepted: tuple[str, ...], label: str) -> None:
    if not accepted:
        return
    lowered = filename.lower()
    if not any(lowered.endswith(ext) for ext in accepted):
        raise HTTPException(
            422,
            f"'{filename}' is not an accepted {label} for this pipeline. "
            f"Expected one of: {', '.join(accepted)}",
        )



@router.post("/{run_id}/reupload", status_code=202, dependencies=[Depends(require_service)])
def reupload(run_id: str, session: Session = Depends(get_session)):
    """Re-run artifact publication.

    The manual recovery path for the case retries are worst at: an outage
    longer than the roughly nine-hour schedule. Service-only for the same
    reason as redeliver -- this is Dharini's server acting, not a browser
    reading.
    """
    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(404, f"unknown run: {run_id}")
    if not run.is_terminal:
        raise HTTPException(409, f"run is {run.status.value}; nothing to publish yet")
    if run.artifacts_uploaded:
        return {"run_id": run.id, "status": "already_uploaded"}

    # Reset the counter: an operator asking for this has fixed whatever caused
    # the exhaustion, and should get the full retry schedule again rather than
    # one attempt against a spent budget.
    run.upload_attempts = 0
    run.upload_error = None
    run.next_upload_attempt_at = utcnow()
    session.commit()

    from ..tasks import upload_run_artifacts

    upload_run_artifacts.apply_async(args=[run.id])
    return {"run_id": run.id, "status": "queued"}
