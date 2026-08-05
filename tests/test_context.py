"""RunContext: run isolation, artifact publishing, soft-failure detection."""

import pytest

from app.runtime.context import RunContext, RunPaths
from app.runtime.spec import MissingOutput, StageFailed


@pytest.fixture
def ctx(tmp_path):
    paths = RunPaths.for_run(tmp_path / "runs", "run-abc-123").create()
    return RunContext(
        run_id="run-abc-123",
        params={"variant": {"tool": "clair3"}, "threads": 6},
        paths=paths,
        refs_dir=tmp_path / "refs",
        stages=["one", "two"],
        input_name="reads.fastq.gz",
    )


def test_each_run_gets_isolated_directories(tmp_path):
    """The original wrote every run to a shared output/ directory, so two
    concurrent runs corrupted each other."""
    a = RunPaths.for_run(tmp_path, "run-a").create()
    b = RunPaths.for_run(tmp_path, "run-b").create()

    assert a.work != b.work
    (a.work / "x.bam").write_text("a")
    (b.work / "x.bam").write_text("b")
    assert (a.work / "x.bam").read_text() == "a"


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


def test_stage_records_success_with_timing(ctx):
    # No subprocess: this asserts the bookkeeping ctx.stage() does around a
    # body, not that a tool ran. Execution is test_runner.py's concern.
    with ctx.stage("one"):
        pass

    assert len(ctx.stage_log) == 1
    entry = ctx.stage_log[0]
    assert entry["status"] == "SUCCEEDED"
    assert entry["duration_s"] >= 0


def test_stage_records_failure_and_reraises(ctx):
    with pytest.raises(StageFailed):
        with ctx.stage("one"):
            raise StageFailed("one", 1, "")

    assert ctx.stage_log[0]["status"] == "FAILED"


def test_skip_is_recorded_not_silent(ctx):
    """Skipping primer trimming changes how variants must be read, so it has to
    be visible on the run rather than only printed as a warning."""
    ctx.skip("primer", "no BEDPE supplied")
    assert ctx.stage_log[0] == {
        "name": "primer", "status": "SKIPPED", "detail": "no BEDPE supplied",
    }


def test_metrics_and_variants_reach_the_result(ctx):
    ctx.metric("variant_count", 12)
    ctx.set_variants([{"pos": 1, "ref": "A", "alt": "G"}])

    assert ctx.result.metrics["variant_count"] == 12
    assert len(ctx.result.variants) == 1
