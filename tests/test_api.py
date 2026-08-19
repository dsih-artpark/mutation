"""End-to-end control plane, exercised through the HTTP API.

Uses synthetic pipelines built from ordinary shell tools, so the whole path --
submission, validation, execution, persistence, artifacts, failure handling --
is covered without needing snippy, minimap2 or the ONT images installed.
"""

import pytest

from app.pipelines import registry
from app.pipelines.base import Pipeline
from app.runtime.context import RunContext, RunResult
from app.runtime.spec import StageFailed


@pytest.fixture()
def client():
    """Overrides conftest's client: every /v1 route now needs a credential."""
    from fastapi.testclient import TestClient

    from app.main import app

    test_client = TestClient(app)
    test_client.headers.update({"X-API-Key": "test-key"})
    return test_client


# Real escapes, not the doubled ones printf used to interpret: this string is
# now written straight to disk, so the newlines and tabs must already be newlines
# and tabs or parse_annotated_vcf sees a single unparseable line.
VCF_BODY = (
    "##fileformat=VCFv4.2\n"
    "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
    "LSDV\t1042\t.\tA\tG\t228\tPASS\tDP=120;AF=0.98;"
    "ANN=G|missense_variant|MODERATE|LSDV008|gene|tx|p1|c|1|c.100A>G|p.Lys34Glu\n"
)


class _EchoPipeline(Pipeline):
    id = "test-echo"
    version = "0.0.1"
    display_name = "Test Echo"
    description = "Synthetic pipeline for control-plane tests."
    accepted_extensions = (".fasta",)
    input_filename = "assembly.fasta"
    stages = ["prepare", "call"]
    params_schema = {
        "type": "object", "additionalProperties": False,
        "properties": {"label": {"type": "string", "default": "run"}},
    }

    def run(self, ctx: RunContext) -> RunResult:
        from app.vcf import parse_annotated_vcf, summarise

        with ctx.stage("prepare"):
            ctx.require(ctx.input_path)

        with ctx.stage("call"):
            vcf = ctx.work_dir / "annotated.vcf"
            # Control-plane tests assert on persistence, artifacts and webhooks,
            # not on process execution -- that is test_runner.py's job. Writing
            # the file directly keeps this suite fast and Docker-free.
            vcf.write_text(VCF_BODY)
            published = ctx.artifact(ctx.require(vcf), kind="vcf", label="Annotated variants")

        rows = parse_annotated_vcf(published)
        ctx.set_variants(rows)
        for key, value in summarise(rows).items():
            ctx.metric(key, value)
        ctx.metric("label", ctx.param("label", "run"))
        return ctx.result


class _FailingPipeline(Pipeline):
    id = "test-fail"
    version = "0.0.1"
    display_name = "Test Failure"
    description = "Fails on purpose."
    accepted_extensions = (".fasta",)
    input_filename = "assembly.fasta"
    stages = ["boom"]
    params_schema = {"type": "object", "additionalProperties": False, "properties": {}}

    def run(self, ctx: RunContext) -> RunResult:
        with ctx.stage("boom"):
            # The exception the runner would have raised. Asserts the control
            # plane handles a StageError correctly, without depending on shell
            # semantics to produce it.
            raise StageFailed("boom", 3, "tool exploded")
        return ctx.result


for _pipeline in (_EchoPipeline(), _FailingPipeline()):
    if _pipeline.id not in registry:
        registry.register(_pipeline)


FASTA_BYTES = b">contig1\nACGTACGTACGT\n"


def put_object(key: str, data: bytes = FASTA_BYTES) -> str:
    """Stage an input the way the frontend does: in storage, before submitting."""
    import tempfile
    from pathlib import Path

    from app.storage import get_store

    with tempfile.NamedTemporaryFile(delete=False) as handle:
        handle.write(data)
        source = Path(handle.name)
    get_store().upload(source, key, "text/plain")
    source.unlink(missing_ok=True)
    return key


def submit(client, **overrides):
    """A valid submission body, with fields overridden per test.

    external_id defaults to a fresh uuid: it is UNIQUE and the suite shares one
    session-scoped database, so a literal would collide across tests.
    """
    import uuid

    key = overrides.pop("_key", None) or put_object(f"uploads/{uuid.uuid4()}/sample.fasta")
    body = {
        "pipeline_id": "test-echo",
        "external_id": str(uuid.uuid4()),
        "input_key": key,
        "input_filename": "sample.fasta",
    }
    body.update(overrides)
    return client.post("/v1/runs", json=body)


@pytest.fixture
def completed_run(client):
    response = submit(client, params={"label": "sample-42"})
    assert response.status_code == 202, response.text
    return response.json()["run_id"]


# ------------------------------------------------------------- discovery

def test_lists_pipelines(client):
    ids = {p["id"] for p in client.get("/v1/pipelines").json()}
    assert {"snippy-fasta", "ont-lsdv"} <= ids


def test_pipeline_detail_exposes_schema(client):
    """Dharini renders its parameter form from this."""
    body = client.get("/v1/pipelines/ont-lsdv").json()
    assert body["params_schema"]["properties"]["variant"]["properties"]["tool"]["enum"]
    # The platform's own bookends are reported alongside the author's stages, so
    # the denominator a client polls matches what will actually run.
    assert body["stages"][0] == "fetch"
    assert body["stages"][-1] == "publish"
    assert "prepare" in body["stages"]


def test_unknown_pipeline_is_404(client):
    assert client.get("/v1/pipelines/nope").status_code == 404


# ------------------------------------------------------------ validation

def test_rejects_wrong_extension(client):
    response = submit(client, _key=put_object("uploads/x/reads.fastq", b"@x\nACGT\n"),
                      input_filename="reads.fastq")
    assert response.status_code == 422
    assert "not an accepted input" in response.json()["detail"]


def test_rejects_invalid_params_before_enqueueing(client):
    """A bad parameter is a fast 422, not a job that dies eight minutes in."""
    response = submit(
        client, pipeline_id="ont-lsdv",
        _key=put_object("uploads/y/reads.fastq.gz", b"\x1f\x8b\x00"),
        input_filename="reads.fastq.gz",
        params={"variant": {"tool": "gatk"}},
    )
    assert response.status_code == 422
    assert "variant.tool" in response.json()["detail"]["errors"][0]


def test_rejects_ssrf_callback(client):
    response = submit(client, callback_url="http://169.254.169.254/latest/meta-data/")
    assert response.status_code == 422
    assert "callback_url rejected" in response.json()["detail"]


def test_unknown_pipeline_submission_is_404(client):
    assert submit(client, pipeline_id="nope").status_code == 404


def test_rejects_a_key_outside_the_allowed_prefix(client):
    """The key is caller-supplied. Without the prefix check this service will
    fetch any object in the bucket it is pointed at."""
    response = submit(client, input_key="runs/somebody-elses/annotated.vcf")
    assert response.status_code == 422
    assert "input key rejected" in response.json()["detail"]


@pytest.mark.parametrize("key", [
    "uploads/../runs/a.fasta", "/uploads/a.fasta", "uploads//a.fasta", "uploadsX/a.fasta",
])
def test_rejects_malformed_keys(client, key):
    assert submit(client, input_key=key).status_code == 422


def test_a_missing_object_is_rejected_before_the_run_is_queued(client):
    """Without this a typo'd key queues, waits behind three others, and dies
    twenty minutes later instead of erroring while the user is still looking."""
    response = submit(client, input_key="uploads/nothing/here.fasta")
    assert response.status_code == 422
    assert response.json()["detail"]["message"] == "input object not found"


def test_an_oversized_object_is_rejected(client, monkeypatch):
    monkeypatch.setattr("app.api.runs.MAX_INPUT_BYTES", 4)
    assert submit(client).status_code == 413


def test_an_undeclared_extra_input_is_rejected(client):
    response = submit(client, extra_input_keys={"primer_bed": "uploads/z/scheme.bed"})
    assert response.status_code == 422
    assert "declares no extra input" in response.json()["detail"]


def test_a_missing_field_is_a_422(client):
    """external_id is required: without it there is no idempotency key."""
    assert client.post("/v1/runs", json={"pipeline_id": "test-echo"}).status_code == 422


def test_an_unknown_field_is_rejected(client):
    """extra='forbid'. A silently-ignored field is a caller believing it set
    something that never took effect."""
    assert submit(client, nonsense="x").status_code == 422


def test_resubmitting_an_external_id_returns_the_original_run(client, monkeypatch):
    """A client timeout and retry must not start a second hour of compute."""
    import uuid

    import app.tasks as tasks

    started = []
    monkeypatch.setattr(tasks.execute_run, "apply_async",
                        lambda *a, **kw: started.append(kw.get("task_id")))

    external_id = str(uuid.uuid4())
    first = submit(client, external_id=external_id)
    second = submit(client, external_id=external_id)

    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["run_id"] == first.json()["run_id"]
    assert len(started) == 1, "a duplicate submission must not start a second run"


def test_the_response_reports_the_effective_stages(client):
    body = submit(client).json()
    assert body["stages"][0] == "fetch"
    assert body["stages"][-1] == "publish"
    assert body["external_id"]


# ------------------------------------------------------------- execution

def test_run_succeeds_and_records_stages(client, completed_run):
    body = client.get(f"/v1/runs/{completed_run}").json()
    assert body["status"] == "SUCCEEDED"
    # fetch is the platform's, prepare and call are the pipeline's.
    assert [s["name"] for s in body["stages"]] == ["fetch", "prepare", "call"]
    assert all(s["status"] == "SUCCEEDED" for s in body["stages"])
    assert all(s["duration_s"] is not None for s in body["stages"])
    # 3 of 4: publish is declared and not yet executed. Task 9 makes this 4/4.
    assert body["progress"] == "3/4"


def test_hostile_filename_is_normalised_on_disk(client):
    """Client filenames must never reach a tool command line."""
    from app.config import RUNS_ROOT
    from app.runtime.context import RunPaths

    response = submit(client, input_filename="../../etc/passwd; rm -rf /.fasta")
    run_id = response.json()["run_id"]

    paths = RunPaths.for_run(RUNS_ROOT, run_id)
    assert (paths.input / "assembly.fasta").exists()
    assert [p.name for p in paths.input.iterdir()] == ["assembly.fasta"]

def test_metrics_are_computed_from_the_parsed_vcf(client, completed_run):
    metrics = client.get(f"/v1/runs/{completed_run}").json()["metrics"]
    assert metrics["variant_count"] == 1
    assert metrics["by_impact"] == {"MODERATE": 1}
    assert metrics["label"] == "sample-42"


def test_scratch_is_purged_but_artifacts_survive(client, completed_run):
    """Only published files survive; the rest dies with work/."""
    from app.config import RUNS_ROOT

    assert not (RUNS_ROOT / completed_run / "work").exists()
    assert (RUNS_ROOT / completed_run / "output" / "annotated.vcf").exists()


def test_reserved_params_are_hidden_from_the_api(client, completed_run):
    params = client.get(f"/v1/runs/{completed_run}").json()["params"]
    assert params == {"label": "sample-42"}
    assert not any(key.startswith("_") for key in params)


# ------------------------------------------------------------- artifacts

def test_artifact_is_downloadable(client, completed_run):
    detail = client.get(f"/v1/runs/{completed_run}").json()
    assert len(detail["artifacts"]) == 1
    artifact = detail["artifacts"][0]
    assert artifact["url"].endswith(f"/v1/runs/{completed_run}/artifacts/annotated.vcf")

    response = client.get(f"/v1/runs/{completed_run}/artifacts/annotated.vcf")
    assert response.status_code == 200
    assert "LSDV008" in response.text


@pytest.mark.parametrize("name", ["../../../etc/passwd", "..%2f..%2fetc%2fpasswd", "nope.vcf"])
def test_only_registered_artifacts_are_served(client, completed_run, name):
    """The filename never reaches the filesystem unvalidated."""
    assert client.get(f"/v1/runs/{completed_run}/artifacts/{name}").status_code == 404


# --------------------------------------------------------------- failure

def test_tool_failure_is_recorded_with_the_log_tail(client):
    run_id = submit(client, pipeline_id="test-fail").json()["run_id"]

    body = client.get(f"/v1/runs/{run_id}").json()
    assert body["status"] == "FAILED"
    assert "exit code 3" in body["error_message"]
    assert "tool exploded" in body["error_message"], "log tail not captured"
    # stages[0] is now the platform's fetch, which succeeded. The FAILED one is
    # the pipeline stage that actually broke.
    assert body["stages"][0]["name"] == "fetch"
    assert [s["status"] for s in body["stages"]][-1] == "FAILED"


def test_redeliver_rejects_a_run_with_no_callback(client, completed_run):
    assert client.post(f"/v1/runs/{completed_run}/redeliver").status_code == 422


# ---------------------------------------------------------------- listing

def test_lists_and_filters_runs(client, completed_run):
    assert any(r["id"] == completed_run for r in client.get("/v1/runs").json())

    filtered = client.get("/v1/runs", params={"pipeline_id": "test-echo"}).json()
    assert all(r["pipeline_id"] == "test-echo" for r in filtered)

    failed = client.get("/v1/runs", params={"status": "FAILED"}).json()
    assert all(r["status"] == "FAILED" for r in failed)


# ------------------------------------------------------------------ meta

def test_health_reports_disk(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["disk"]["free_gb"] > 0
    assert body["pipelines"] >= 2


def test_health_probes_storage_and_schema(client):
    """Credentials failing at 02:00 on a run that has been computing for forty
    minutes should not be the first sign of a misconfiguration."""
    body = client.get("/health").json()
    assert body["storage"]["reachable"] is True
    assert body["storage"]["backend"] == "local"
    # "the migration did not run" and "the code is old" look identical from
    # outside without this.
    assert body["schema_revision"] == "0002"


def test_health_stays_200_when_storage_is_unreachable(client, monkeypatch):
    """Taking every instance out of rotation because a dependency blipped turns
    a degradation into an outage. The body says which it is."""
    import app.main

    monkeypatch.setattr(app.main, "_storage_probe", None)
    monkeypatch.setattr(app.main, "get_store",
                        lambda: (_ for _ in ()).throw(RuntimeError("no credentials")))

    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["storage"]["reachable"] is False
    assert "no credentials" in response.json()["storage"]["error"]

    monkeypatch.setattr(app.main, "_storage_probe", None)


def test_health_needs_no_credential(client):
    """The load balancer has no API key. An empty one must not 401 it either."""
    assert client.get("/health", headers={"X-API-Key": ""}).status_code == 200
    assert client.get("/").status_code == 200
