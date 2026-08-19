"""The fetch bookend: inputs arrive from the object store before the pipeline.

Exercises app/tasks.py directly rather than through the API, because the
submission endpoint is still multipart until Task 12 and this is the path an
object-store run takes.
"""

from __future__ import annotations

import uuid

import pytest

from app.db import session_scope
from app.models import Run, RunStatus
from app.pipelines import registry
from app.pipelines.base import ExtraInput, Pipeline
from app.runtime.context import RunContext, RunPaths, RunResult
from app.storage import get_store


class _FetchPipeline(Pipeline):
    """Records what it found on disk. No tools, so no images and no Docker."""

    id = "test-fetch"
    version = "0.0.1"
    display_name = "Test Fetch"
    description = "Synthetic pipeline for fetch-bookend tests."
    accepted_extensions = (".fasta",)
    input_filename = "assembly.fasta"
    stages = ["inspect"]
    extra_inputs = {
        "primer_bed": ExtraInput(filename="primers.bed", extensions=(".bed",)),
    }

    def run(self, ctx: RunContext) -> RunResult:
        with ctx.stage("inspect"):
            ctx.require(ctx.input_path)
            ctx.metric("input_bytes", ctx.input_path.stat().st_size)
            extra = ctx.extra_input("primer_bed")
            ctx.metric("has_primer_bed", bool(extra and extra.exists()))
        return ctx.result


if "test-fetch" not in registry:
    registry.register(_FetchPipeline())


@pytest.fixture()
def store_object():
    """Put bytes at a key in whatever backend the suite is configured for."""
    def _put(key: str, data: bytes, tmp_path) -> str:
        source = tmp_path / f"src-{uuid.uuid4().hex[:6]}"
        source.write_bytes(data)
        get_store().upload(source, key, "text/plain")
        return key
    return _put


def _queue(input_key: str | None, extra_keys: dict | None = None) -> str:
    with session_scope() as session:
        run = Run(
            pipeline_id="test-fetch",
            pipeline_version="0.0.1",
            external_id=str(uuid.uuid4()),
            status=RunStatus.QUEUED,
            params={},
            input_key=input_key,
            extra_input_keys=extra_keys or {},
            input_filename="Hostile Name; rm -rf.fasta",
        )
        session.add(run)
        session.flush()
        return run.id


def _execute(run_id: str):
    from app.tasks import execute_run

    execute_run(run_id)
    with session_scope() as session:
        run = session.get(Run, run_id)
        return {
            "status": run.status,
            "error_stage": run.error_stage,
            "error_message": run.error_message or "",
            "stages": [(s.name, s.status.value) for s in
                       sorted(run.stages, key=lambda s: s.id)],
            "metrics": dict(run.metrics or {}),
        }


def test_the_primary_input_lands_under_the_pipelines_declared_name(store_object, tmp_path):
    """The client's filename is metadata. What reaches a tool is the pipeline's
    own name, so spaces and shell metacharacters never get that far."""
    key = store_object("uploads/a/reads.fasta", b">chr1\nACGT\n", tmp_path)
    run_id = _queue(key)

    result = _execute(run_id)
    assert result["status"] == RunStatus.SUCCEEDED, result["error_message"]

    paths = RunPaths.for_run(__import__("app.config", fromlist=["RUNS_ROOT"]).RUNS_ROOT, run_id)
    assert (paths.input / "assembly.fasta").exists()
    assert not (paths.input / "Hostile Name; rm -rf.fasta").exists()


def test_an_extra_input_lands_under_its_declared_name(store_object, tmp_path):
    primary = store_object("uploads/b/reads.fasta", b">chr1\nACGT\n", tmp_path)
    bed = store_object("uploads/b/scheme.bed", b"chr1\t1\t20\tp1\n", tmp_path)

    result = _execute(_queue(primary, {"primer_bed": bed}))
    assert result["status"] == RunStatus.SUCCEEDED, result["error_message"]
    assert result["metrics"]["has_primer_bed"] is True


def test_fetch_is_recorded_as_the_first_stage(store_object, tmp_path):
    key = store_object("uploads/c/reads.fasta", b">chr1\nACGT\n", tmp_path)
    result = _execute(_queue(key))
    assert result["stages"][0] == ("fetch", "SUCCEEDED")


def test_a_missing_object_fails_the_run_at_fetch():
    """Cheap to redo -- nothing has been computed -- so this is a plain failure
    rather than the retry machinery the upload side gets."""
    result = _execute(_queue("uploads/absent.fasta"))
    assert result["status"] == RunStatus.FAILED
    assert result["error_stage"] == "fetch"


def test_an_oversized_object_fails_rather_than_filling_the_disk(
        store_object, tmp_path, monkeypatch):
    monkeypatch.setattr("app.tasks.MAX_INPUT_BYTES", 10)
    key = store_object("uploads/d/big.fasta", b"A" * 5000, tmp_path)

    result = _execute(_queue(key))
    assert result["status"] == RunStatus.FAILED
    assert result["error_stage"] == "fetch"
    assert "limit" in result["error_message"]


def test_a_run_with_no_input_key_still_executes(tmp_path):
    """The legacy multipart path puts its file on disk before the task starts.
    Both submission shapes must execute identically until Task 12 removes one.
    """
    run_id = _queue(None)
    paths = RunPaths.for_run(
        __import__("app.config", fromlist=["RUNS_ROOT"]).RUNS_ROOT, run_id).create()
    (paths.input / "assembly.fasta").write_bytes(b">chr1\nACGT\n")

    result = _execute(run_id)
    assert result["status"] == RunStatus.SUCCEEDED, result["error_message"]


def test_preflight_names_the_missing_image_and_the_fix():
    from app.runtime.runner import preflight
    from app.runtime.spec import PreflightFailed

    with pytest.raises(PreflightFailed, match="make images-pull"):
        preflight(["dsihartpark/definitely-not-present:0.0.0"])


def test_preflight_passes_when_a_pipeline_declares_no_images():
    """Synthetic test pipelines resolve no images, so preflight is a no-op for
    them -- which is what keeps the fast suite Docker-free."""
    from app.runtime.runner import preflight

    preflight(registry.get("test-fetch").required_images())
