"""Run submission and status.

One generic endpoint for every pipeline. `pipeline_id` selects the
implementation and `params` is validated against that pipeline's declared
schema, so a new pipeline needs no new route.
"""

from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from ..auth import Principal, require_principal, require_service
from ..config import MAX_UPLOAD_BYTES, PUBLIC_BASE_URL, RUNS_ROOT
from ..db import get_session
from ..delivery import CallbackRejected, validate_callback_url
from ..models import DeliveryStatus, Run, RunStatus, WebhookDelivery, utcnow
from ..pipelines import ParamsInvalid, PipelineNotFound, registry
from ..runtime.context import RunPaths
from ..schemas import RunAccepted, RunDetail, RunSummary

router = APIRouter(prefix="/v1/runs", tags=["runs"])

_CHUNK = 1024 * 1024


# ==========================================================================
# Submission
# ==========================================================================

@router.post("", response_model=RunAccepted, status_code=202)
async def create_run(
    pipeline_id: str = Form(..., description="Pipeline id, e.g. 'ont-lsdv'"),
    file: UploadFile = File(..., description="Primary input (FASTA or FASTQ)"),
    params: str = Form("{}", description="JSON object matching the pipeline's params_schema"),
    callback_url: str | None = Form(None, description="Webhook called when the run finishes"),
    primer_bed: UploadFile | None = File(None, description="Optional ARTIC primer scheme BED"),
    session: Session = Depends(get_session),
):
    try:
        pipeline = registry.get(pipeline_id)
    except PipelineNotFound:
        known = ", ".join(p.id for p in registry.all())
        raise HTTPException(404, f"unknown pipeline '{pipeline_id}'. Available: {known}") from None

    try:
        parsed_params = json.loads(params) if params else {}
    except ValueError as exc:
        raise HTTPException(422, f"params is not valid JSON: {exc}") from None
    if not isinstance(parsed_params, dict):
        raise HTTPException(422, "params must be a JSON object")

    # Validated before anything is written to disk or enqueued, so a bad
    # parameter is a fast 422 rather than a job that dies mid-pipeline.
    try:
        validated = registry.validate_params(pipeline_id, parsed_params)
    except ParamsInvalid as exc:
        raise HTTPException(422, {"message": "invalid params", "errors": exc.errors}) from None

    if callback_url:
        try:
            callback_url = validate_callback_url(callback_url)
        except CallbackRejected as exc:
            raise HTTPException(422, f"callback_url rejected: {exc}") from None

    _check_extension(file.filename or "", pipeline.accepted_extensions)

    run = Run(
        pipeline_id=pipeline.id,
        pipeline_version=pipeline.version,
        # external_id is NOT NULL and unique -- it is meant to be the caller's
        # own idempotency key (a retried submission should return the original
        # run, not start a second one). Task 12 adds it to the request body;
        # until then there is no caller-supplied value to key off, so a fresh
        # uuid4 stands in, which satisfies the constraint without granting
        # idempotency.
        external_id=str(uuid.uuid4()),
        status=RunStatus.QUEUED,
        params=validated,
        input_filename=file.filename,
        callback_url=callback_url,
    )
    session.add(run)
    session.flush()

    paths = RunPaths.for_run(RUNS_ROOT, run.id).create()

    stored_name = _stored_name(pipeline.input_filename, file.filename or "")
    try:
        written = await _save_upload(file, paths.input / stored_name)
    except ValueError as exc:
        shutil.rmtree(paths.root, ignore_errors=True)
        raise HTTPException(413, str(exc)) from None

    extra_inputs: dict[str, str] = {}
    if primer_bed is not None and primer_bed.filename:
        # Dharini already posts this field as `primer_bed`, so the name stays
        # even though the file it carries is now an ARTIC primer scheme.
        await _save_upload(primer_bed, paths.input / "primers.bed")
        extra_inputs["primer_bed"] = "primers.bed"

    # Reserved keys are stripped before the pipeline sees its params.
    run.params = {**validated, "_input_name": stored_name, "_extra_inputs": extra_inputs}
    run.input_bytes = written

    queued_before = (
        session.query(Run)
        .filter(Run.status == RunStatus.QUEUED, Run.created_at < run.created_at)
        .count()
    )

    session.commit()

    from ..tasks import execute_run

    execute_run.apply_async(args=[run.id], task_id=run.id)

    return RunAccepted(
        run_id=run.id,
        pipeline_id=pipeline.id,
        pipeline_version=pipeline.version,
        status=run.status.value,
        stages=list(pipeline.effective_stages),
        queue_position=queued_before,
    )


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


def _check_extension(filename: str, accepted: tuple[str, ...]) -> None:
    if not accepted:
        return
    lowered = filename.lower()
    if not any(lowered.endswith(ext) for ext in accepted):
        raise HTTPException(
            422,
            f"'{filename}' is not an accepted input for this pipeline. "
            f"Expected one of: {', '.join(accepted)}",
        )


def _stored_name(declared: str, original: str) -> str:
    """Normalise the on-disk name, preserving only whether it is compressed.

    Client filenames carry spaces, unicode and shell metacharacters; none of
    that should ever reach a tool command line. The original is kept as run
    metadata for display.
    """
    if declared.endswith(".gz") and not original.lower().endswith(".gz"):
        return declared[:-3]
    return declared


async def _save_upload(upload: UploadFile, destination: Path) -> int:
    """Stream to disk, enforcing the size cap as we go.

    Checked during the write rather than from Content-Length, which a client
    controls and can understate.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    written = 0

    with open(destination, "wb") as out:
        while chunk := await upload.read(_CHUNK):
            written += len(chunk)
            if written > MAX_UPLOAD_BYTES:
                out.close()
                destination.unlink(missing_ok=True)
                raise ValueError(
                    f"upload exceeds the {MAX_UPLOAD_BYTES / 10**9:.1f} GB limit"
                )
            out.write(chunk)

    return written
