"""Backwards-compatible endpoints for the existing Dharini integration.

Dharini is deployed and calling /upload and /status today. These shims keep it
working unchanged while it migrates to /v1/runs, so the two sides can be
released independently rather than in lockstep.

Remove once Dharini is fully on /v1.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy.orm import Session

from ..config import RUNS_ROOT
from ..db import get_session
from ..models import Run, RunStatus
from ..runtime.context import RunPaths
from ..pipelines.snippy_fasta.visualization import generate_visualization
from .runs import create_run

router = APIRouter(tags=["legacy"], deprecated=True)

_STATUS_MAP = {
    RunStatus.QUEUED: "PENDING",
    RunStatus.RUNNING: "STARTED",
    RunStatus.SUCCEEDED: "SUCCESS",
    RunStatus.FAILED: "FAILURE",
    RunStatus.CANCELLED: "REVOKED",
}

_LEGACY_DOWNLOADS = {"snps.vcf", "annotated.vcf", "visualization.html"}


def _run_paths(run_id: str) -> RunPaths:
    """Resolve a run directory from a path parameter.

    These two endpoints take `run_id` straight off the URL without looking the
    run up first, so `RunPaths.for_run` is the only thing standing between a
    crafted id and a file outside RUNS_ROOT. It signals that with ValueError;
    unhandled, FastAPI would turn a rejected attack into a 500.
    """
    try:
        return RunPaths.for_run(RUNS_ROOT, run_id)
    except ValueError:
        raise HTTPException(400, "Invalid run id") from None


@router.post("/upload", summary="Deprecated — use POST /v1/runs")
async def upload_file(
    file: UploadFile = File(..., description="Select a FASTA file"),
    callback_url: str | None = Form(None),
    session: Session = Depends(get_session),
):
    accepted = await create_run(
        pipeline_id="snippy-fasta",
        file=file,
        params=json.dumps({}),
        callback_url=callback_url,
        primer_bed=None,
        session=session,
    )
    return {
        "job_id": accepted.run_id,
        "filename": file.filename,
        "status": "queued",
    }


@router.get("/status/{job_id}", summary="Deprecated — use GET /v1/runs/{run_id}")
def get_status(job_id: str, session: Session = Depends(get_session)):
    run = session.get(Run, job_id)
    if run is None:
        raise HTTPException(404, f"unknown job: {job_id}")

    payload: dict = {
        "job_id": run.id,
        "status": _STATUS_MAP.get(run.status, run.status.value),
        "result": None,
    }

    output = RunPaths.for_run(RUNS_ROOT, run.id).output

    if run.status == RunStatus.SUCCEEDED:
        payload["result"] = {
            "status": "completed",
            "run_id": run.id,
            "snps_vcf": str(output / "snps.vcf"),
            "annotated_vcf": str(output / "annotated.vcf"),
            "visualization": (
                f"/visualization/{run.id}"
                if (output / "visualization.html").exists() else None
            ),
        }
        # The original endpoint inlined whole VCF files. Preserved here for
        # compatibility only -- /v1/runs returns parsed rows and artifact URLs,
        # which is what new integrations should use.
        for name, key in (("snps.vcf", "snps_vcf_data"), ("annotated.vcf", "annotated_vcf_data")):
            path = output / name
            if path.exists():
                payload[key] = path.read_text(errors="replace")

    elif run.status == RunStatus.FAILED:
        payload["result"] = {
            "status": "failed",
            "error": run.error_message,
            "stage": run.error_stage,
        }

    return payload


@router.get("/download/{run_id}/{file_name}", summary="Deprecated — use /v1/runs/{id}/artifacts")
def download_file(run_id: str, file_name: str):
    if file_name not in _LEGACY_DOWNLOADS:
        raise HTTPException(400, "Invalid file name")

    paths = _run_paths(run_id)
    for candidate in (paths.output / file_name, paths.root / file_name):
        if candidate.exists():
            return FileResponse(candidate, filename=f"{run_id}_{file_name}")

    raise HTTPException(404, "File not found")


@router.get("/visualization/{run_id}", response_class=HTMLResponse)
def view_visualization(run_id: str):
    paths = _run_paths(run_id)

    for candidate in (paths.output / "visualization.html", paths.root / "visualization.html"):
        if candidate.exists():
            return HTMLResponse(candidate.read_text(errors="replace"))

    annotated = paths.output / "annotated.vcf"
    if not annotated.exists():
        annotated = paths.root / "annotated.vcf"
    if not annotated.exists():
        raise HTTPException(404, "Run not found or annotated VCF missing")

    target = paths.output / "visualization.html"
    try:
        generate_visualization(run_id, vcf_file=annotated, output_html=target)
    except FileNotFoundError:
        raise HTTPException(404, "Run not found or annotated VCF missing") from None
    except Exception as exc:
        raise HTTPException(500, f"Error generating visualization: {exc}") from None

    if not target.exists():
        raise HTTPException(404, "Visualization could not be generated")
    return HTMLResponse(target.read_text(errors="replace"))
