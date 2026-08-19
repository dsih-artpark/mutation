"""RunContext: run isolation, artifact publishing, soft-failure detection."""

from pathlib import Path

import pytest

from app.runtime.context import RunContext, RunPaths
from app.runtime.spec import CompletedStage, MissingOutput, StageFailed, StageSpec


class _StubRunner:
    """Stands in for StageRunner: records the call, runs no process.

    Honours this suite's no-subprocess rule while still exercising the part of
    RunContext.run that matters -- propagating peak memory and image out of a
    CompletedStage and into the run record.
    """

    def __init__(self, log_dir: Path):
        self.calls: list[dict] = []
        self._log_dir = log_dir

    def run(self, stage, spec, argv, env=None, stdout_path=None) -> CompletedStage:
        self.calls.append({"stage": stage, "spec": spec, "argv": list(argv)})
        return CompletedStage(
            name=stage,
            exit_code=0,
            duration_s=0.01,
            peak_memory_mb=512.0,
            log_path=self._log_dir / f"{stage}.log",
            stdout_tail="",
            image="x:1",
        )


class _RecordingRecorder:
    """Captures stage_finished. The image lands here, not in stage_log."""

    def __init__(self):
        self.finished: list[tuple] = []
        self.messages: list[str] = []

    def log(self, message): self.messages.append(message)

    def stage_started(self, name, ordinal): pass

    def stage_finished(self, name, duration_s, peak_memory_mb, image):
        self.finished.append((name, duration_s, peak_memory_mb, image))

    def stage_failed(self, name, message, exit_code): pass

    def stage_skipped(self, name, reason): pass


@pytest.fixture
def ctx(tmp_path):
    paths = RunPaths.for_run(tmp_path / "runs", "run-abc-123").create()
    return RunContext(
        run_id="run-abc-123",
        params={"variant": {"tool": "clair3"}, "threads": 6},
        paths=paths,
        refs_dir=tmp_path / "refs",
        recorder=_RecordingRecorder(),
        stages=["one", "two", "primer"],
        input_name="reads.fastq.gz",
    )


@pytest.fixture
def stub(ctx):
    """Swap the real runner for the stub; returned for call assertions."""
    ctx.runner = _StubRunner(ctx.paths.logs)
    return ctx.runner


def test_each_run_gets_isolated_directories(tmp_path):
    """The original wrote every run to a shared output/ directory, so two
    concurrent runs corrupted each other."""
    a = RunPaths.for_run(tmp_path, "run-a").create()
    b = RunPaths.for_run(tmp_path, "run-b").create()

    assert a.work != b.work
    (a.work / "x.bam").write_text("a")
    (b.work / "x.bam").write_text("b")
    assert (a.work / "x.bam").read_text() == "a"


@pytest.mark.parametrize(
    "run_id",
    ["..", "../escape", "../../etc", "a/../..", "", ".", "/etc"],
    ids=["parent", "parent-child", "two-levels", "normalises-out", "empty", "dot", "absolute"],
)
def test_a_run_id_that_escapes_the_runs_root_is_refused(tmp_path, run_id):
    """`GET /download/..%2Fsnps.vcf` served a file from outside RUNS_ROOT.

    Validated here rather than at each of the seven call sites, so an eighth
    caller cannot reintroduce it.
    """
    with pytest.raises(ValueError, match="invalid run id"):
        RunPaths.for_run(tmp_path / "runs", run_id)


def test_a_normal_run_id_still_resolves(tmp_path):
    runs_root = tmp_path / "runs"
    run_id = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"

    paths = RunPaths.for_run(runs_root, run_id)

    assert paths.root == (runs_root / run_id).resolve()
    assert paths.output == paths.root / "output"


def test_nested_param_lookup(ctx):
    assert ctx.param("variant.tool") == "clair3"
    assert ctx.param("threads") == 6
    assert ctx.param("missing.key", "fallback") == "fallback"
    assert ctx.param("variant.missing", "d") == "d"


def test_input_path_uses_the_normalised_name(ctx):
    assert ctx.input_path.name == "reads.fastq.gz"
    assert ctx.input_path.parent == ctx.input_dir


def test_require_rejects_a_missing_output(ctx):
    with pytest.raises(MissingOutput):
        ctx.require(ctx.work_dir / "never-created.bam")


def test_require_rejects_an_empty_output(ctx):
    """Tools here fail softly: exit 0 having written nothing."""
    empty = ctx.work_dir / "empty.vcf"
    empty.touch()
    with pytest.raises(MissingOutput):
        ctx.require(empty)


def test_artifact_moves_the_file_into_output(ctx):
    source = ctx.work_dir / "result.vcf"
    source.write_text("##fileformat=VCFv4.2\n")

    published = ctx.artifact(source, kind="vcf", label="Result")

    assert published.parent == ctx.output_dir
    assert not source.exists(), "file should move, not copy"
    assert len(ctx.result.artifacts) == 1
    assert ctx.result.artifacts[0].kind == "vcf"


def test_stage_records_success_with_timing(ctx, stub):
    with ctx.stage("one"):
        ctx.run(StageSpec(image="x:1"), ["true"])

    assert len(ctx.stage_log) == 1
    entry = ctx.stage_log[0]
    assert entry["status"] == "SUCCEEDED"
    assert entry["duration_s"] >= 0
    # Real work happened, so the stage carries the runner's telemetry rather
    # than the None an empty stage body would leave behind.
    assert entry["peak_memory_mb"] == 512.0


def test_run_propagates_peak_memory_and_image_out_of_the_runner(ctx, stub):
    """CompletedStage telemetry has to reach the run record; conftest pins
    STATS_SAMPLE_INTERVAL_S=0, so a real container could never cover this."""
    with ctx.stage("one"):
        ctx.run(StageSpec(image="x:1"), ["true"])

    assert ctx.stage_log[0]["peak_memory_mb"] == 512.0
    # The image lands on the recorder, not in stage_log.
    name, _duration, peak, image = ctx.recorder.finished[0]
    assert (name, peak, image) == ("one", 512.0, "x:1")


def test_run_outside_a_stage_is_labelled_unnamed(ctx, stub):
    """The `self._current_stage or "unnamed"` fallback: a pipeline may call
    ctx.run() outside any stage block, and the log file still needs a name."""
    ctx.run(StageSpec(image="x:1"), ["true"])

    assert stub.calls[0]["stage"] == "unnamed"


def test_stage_records_failure_and_reraises(ctx):
    with pytest.raises(StageFailed):
        with ctx.stage("one"):
            raise StageFailed("one", 1, "")

    assert ctx.stage_log[0]["status"] == "FAILED"


def test_skip_is_recorded_not_silent(ctx):
    """Skipping primer trimming changes how variants must be read, so it has to
    be visible on the run rather than only printed as a warning."""
    ctx.skip("primer", "no primer BED supplied")
    assert ctx.stage_log[0] == {
        "name": "primer", "status": "SKIPPED", "detail": "no primer BED supplied",
    }


def test_metrics_and_variants_reach_the_result(ctx):
    ctx.metric("variant_count", 12)
    ctx.set_variants([{"pos": 1, "ref": "A", "alt": "G"}])

    assert ctx.result.metrics["variant_count"] == 12
    assert len(ctx.result.variants) == 1


def test_an_undeclared_stage_is_rejected(ctx):
    """`stages` drives the progress denominator a caller polls, and it is
    written in a different place from the ctx.stage() calls that must match it.
    A stage added to run() and forgotten in the list used to score ordinal -1:
    the run reported "4/7" while executing eight stages, and succeeded having
    silently skipped a position."""
    with pytest.raises(ValueError, match="not in the pipeline's declared stages"):
        with ctx.stage("nowhere"):
            pass


def test_an_undeclared_skip_is_rejected_too(ctx):
    """Same drift, quieter: a typo'd skip name would otherwise record a stage
    nobody declared and leave the real one looking like it never ran."""
    with pytest.raises(ValueError, match="not in the pipeline's declared stages"):
        ctx.skip("primerr", "typo")


def test_a_pipeline_declaring_no_stages_is_left_alone(tmp_path):
    """The synthetic contexts in this suite, and any pipeline that genuinely
    wants no progress reporting, must not be forced to declare stages."""
    paths = RunPaths.for_run(tmp_path / "runs", "run-no-stages").create()
    bare = RunContext(
        run_id="run-no-stages", params={}, paths=paths,
        refs_dir=tmp_path / "refs", recorder=_RecordingRecorder(),
    )
    with bare.stage("anything"):
        pass
    assert bare.stage_log[0]["name"] == "anything"


# ------------------------------------------------------------ content types

@pytest.mark.parametrize("filename,expected", [
    # The one that was actually wrong: mimetypes maps .vcf to text/x-vcard, a
    # vCard contact card. That value is stored as object metadata and decides
    # what a browser does when someone downloads a variant call file.
    ("annotated.vcf", "text/plain"),
    ("snps.vcf.gz", "application/gzip"),      # compression wins over .vcf
    ("reads.fastq.gz", "application/gzip"),
    ("consensus.fasta", "text/plain"),
    ("aligned.bam", "application/octet-stream"),
    ("aligned.bam.bai", "application/octet-stream"),
    ("primers.bed", "text/plain"),
    ("visualization.html", "text/html"),
    ("run.json", "application/json"),
    ("something.unknown", "application/octet-stream"),
])
def test_artifact_content_types_describe_genomics_formats(filename, expected):
    from app.runtime.context import _content_type

    assert _content_type(filename) == expected


def test_a_published_artifact_carries_the_corrected_type(tmp_path):
    from app.runtime.context import RunContext, RunPaths

    paths = RunPaths.for_run(tmp_path / "runs", "run-ct").create()
    ctx = RunContext(run_id="run-ct", params={}, paths=paths,
                     refs_dir=tmp_path, stages=["only"])

    source = paths.work / "annotated.vcf"
    source.write_text("##fileformat=VCFv4.2\n")
    with ctx.stage("only"):
        ctx.artifact(source, kind="vcf", label="Annotated variants")

    assert ctx.result.artifacts[0].content_type == "text/plain"
