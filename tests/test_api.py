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


FASTA = ("sample.fasta", b">contig1\nACGTACGTACGT\n")


@pytest.fixture
def completed_run(client):
    response = client.post(
        "/v1/runs",
        data={"pipeline_id": "test-echo", "params": '{"label": "sample-42"}'},
        files={"file": FASTA},
    )
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
    assert body["stages"][0] == "prepare"


def test_unknown_pipeline_is_404(client):
    assert client.get("/v1/pipelines/nope").status_code == 404


# ------------------------------------------------------------ validation

def test_rejects_wrong_extension(client):
    response = client.post(
        "/v1/runs", data={"pipeline_id": "test-echo"},
        files={"file": ("reads.fastq", b"@x\nACGT\n")},
    )
    assert response.status_code == 422
    assert "not an accepted input" in response.json()["detail"]


def test_rejects_invalid_params_before_enqueueing(client):
    """A bad parameter is a fast 422, not a job that dies eight minutes in."""
    response = client.post(
        "/v1/runs",
        data={"pipeline_id": "ont-lsdv", "params": '{"variant": {"tool": "gatk"}}'},
        files={"file": ("reads.fastq.gz", b"\x1f\x8b\x00")},
    )
    assert response.status_code == 422
    assert "variant.tool" in response.json()["detail"]["errors"][0]


def test_rejects_malformed_params_json(client):
    response = client.post(
        "/v1/runs", data={"pipeline_id": "test-echo", "params": "{not json"},
        files={"file": FASTA},
    )
    assert response.status_code == 422


def test_rejects_ssrf_callback(client):
    response = client.post(
        "/v1/runs",
        data={"pipeline_id": "test-echo",
              "callback_url": "http://169.254.169.254/latest/meta-data/"},
        files={"file": FASTA},
    )
    assert response.status_code == 422
    assert "callback_url rejected" in response.json()["detail"]


def test_unknown_pipeline_submission_is_404(client):
    response = client.post("/v1/runs", data={"pipeline_id": "nope"}, files={"file": FASTA})
    assert response.status_code == 404


# ------------------------------------------------------------- execution

def test_run_succeeds_and_records_stages(client, completed_run):
    body = client.get(f"/v1/runs/{completed_run}").json()
    assert body["status"] == "SUCCEEDED"
    assert [s["name"] for s in body["stages"]] == ["prepare", "call"]
    assert all(s["status"] == "SUCCEEDED" for s in body["stages"])
    assert all(s["duration_s"] is not None for s in body["stages"])
    assert body["progress"] == "2/2"


def test_hostile_filename_is_normalised_on_disk(client):
    """Client filenames must never reach a tool command line."""
    response = client.post(
        "/v1/runs", data={"pipeline_id": "test-echo"},
        files={"file": ("my sample; rm -rf ~.fasta", b">x\nACGT\n")},
    )
    run_id = response.json()["run_id"]

    from app.config import RUNS_ROOT
    stored = list((RUNS_ROOT / run_id / "input").iterdir())
    assert [p.name for p in stored] == ["assembly.fasta"]

    # The original is retained as metadata for display.
    assert client.get(f"/v1/runs/{run_id}").json()["input_filename"] == "my sample; rm -rf ~.fasta"


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
    response = client.post("/v1/runs", data={"pipeline_id": "test-fail"}, files={"file": FASTA})
    run_id = response.json()["run_id"]

    body = client.get(f"/v1/runs/{run_id}").json()
    assert body["status"] == "FAILED"
    assert "exit code 3" in body["error_message"]
    assert "tool exploded" in body["error_message"], "log tail not captured"
    assert body["stages"][0]["status"] == "FAILED"


def test_redeliver_rejects_a_run_with_no_callback(client, completed_run):
    assert client.post(f"/v1/runs/{completed_run}/redeliver").status_code == 422


# ---------------------------------------------------------------- listing

def test_lists_and_filters_runs(client, completed_run):
    assert any(r["id"] == completed_run for r in client.get("/v1/runs").json())

    filtered = client.get("/v1/runs", params={"pipeline_id": "test-echo"}).json()
    assert all(r["pipeline_id"] == "test-echo" for r in filtered)

    failed = client.get("/v1/runs", params={"status": "FAILED"}).json()
    assert all(r["status"] == "FAILED" for r in failed)


# ----------------------------------------------------------------- legacy

def test_legacy_endpoints_still_respond(client):
    """Dharini is calling these today; they must keep working during migration."""
    response = client.post("/upload", files={"file": ("legacy.fasta", b">x\nACGT\n")})
    assert response.status_code == 200
    job_id = response.json()["job_id"]

    status = client.get(f"/status/{job_id}").json()
    assert status["job_id"] == job_id
    assert status["status"] in {"PENDING", "STARTED", "SUCCESS", "FAILURE"}


def test_legacy_status_of_unknown_job_is_404(client):
    assert client.get("/status/does-not-exist").status_code == 404


def test_legacy_download_cannot_traverse_out_of_the_runs_root(client):
    """`GET /download/..%2Fsnps.vcf` used to return a file from RUNS_ROOT's
    parent. The rejection must be a 4xx, not a 500 from an unhandled ValueError.
    """
    from app.config import RUNS_ROOT

    planted = RUNS_ROOT.parent / "snps.vcf"
    planted.write_text("outside the runs root\n")
    try:
        response = client.get("/download/..%2Fsnps.vcf")
        assert response.status_code == 400, response.text
        assert "outside the runs root" not in response.text
    finally:
        planted.unlink(missing_ok=True)


def test_legacy_visualization_cannot_traverse_out_of_the_runs_root(client):
    # Percent-encoded so the HTTP client does not normalise `..` away before
    # the request is sent; the ASGI layer decodes it back into the path param.
    response = client.get("/visualization/%2e%2e")
    assert response.status_code == 400, response.text


# ------------------------------------------------------------------ meta

def test_health_reports_disk(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["disk"]["free_gb"] > 0
    assert body["pipelines"] >= 2
