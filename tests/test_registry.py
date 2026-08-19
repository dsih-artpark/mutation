"""Pipeline registration and parameter validation."""

import json

import pytest

from app.pipelines import ParamsInvalid, registry


def test_both_pipelines_registered():
    ids = {p.id for p in registry.all()}
    assert {"snippy-fasta", "ont-lsdv"} <= ids


def test_descriptions_are_json_serialisable():
    """Dharini fetches these to render its forms."""
    json.dumps(registry.describe_all())


@pytest.mark.parametrize("pipeline_id", ["snippy-fasta", "ont-lsdv"])
def test_every_pipeline_declares_stages_and_schema(pipeline_id):
    pipeline = registry.get(pipeline_id)
    assert pipeline.stages, "stages drive progress reporting"
    assert pipeline.version
    assert pipeline.params_schema["type"] == "object"
    assert pipeline.params_schema.get("additionalProperties") is False


def test_defaults_are_applied_recursively():
    params = registry.validate_params("ont-lsdv", {})
    assert params["threads"] == 4
    assert params["variant"]["tool"] == "clair3"
    assert params["nanofilt"]["enabled"] is False
    # Haploid: LSDV is a virus. A diploid default would emit impossible calls.
    assert params["bcftools"]["ploidy"] == 1
    # align_trim assigns a read group per primer pool, and Medaka refuses to
    # combine them unless this is set.
    assert params["medaka"]["ignore_read_groups"] is True


def test_override_preserves_sibling_defaults():
    # bcftools rather than clair3: overriding a field with its own default
    # would pass whether or not the override was honoured.
    params = registry.validate_params("ont-lsdv", {"variant": {"tool": "bcftools"}})
    assert params["variant"]["tool"] == "bcftools"
    assert params["consensus"]["tool"] == "bcftools"


def test_typo_in_key_is_rejected():
    """Without additionalProperties:False this would be silently ignored."""
    with pytest.raises(ParamsInvalid) as exc:
        registry.validate_params("ont-lsdv", {"treads": 8})
    assert "treads" in str(exc.value)


def test_unknown_tool_is_rejected():
    with pytest.raises(ParamsInvalid) as exc:
        registry.validate_params("ont-lsdv", {"variant": {"tool": "gatk"}})
    assert "variant.tool" in str(exc.value)


def test_out_of_range_value_is_rejected():
    with pytest.raises(ParamsInvalid):
        registry.validate_params("ont-lsdv", {"threads": 999})


def test_unknown_pipeline_raises():
    from app.pipelines import PipelineNotFound

    with pytest.raises(PipelineNotFound):
        registry.get("does-not-exist")


def test_every_pipeline_package_is_discovered_without_an_import_line():
    """Adding a pipeline must not require editing app/pipelines/__init__.py."""
    import pkgutil

    import app.pipelines as pipelines
    from app.pipelines import registry

    packages = {
        module.name
        for module in pkgutil.iter_modules(pipelines.__path__)
        if module.ispkg and not module.name.startswith("_")
    }
    registered = {pipeline.id.replace("-", "_") for pipeline in registry.all()}

    # Subset, not equality: other test modules (e.g. test_api.py) register
    # synthetic pipelines straight into the shared registry singleton for
    # their own purposes, so `registered` legitimately grows beyond the real
    # packages when the whole suite runs together (test_both_pipelines_registered
    # above tolerates the same thing). What this test must catch is a real
    # subpackage that the scan silently failed to pick up -- that still shows
    # up as `packages` having a member `registered` lacks.
    assert packages <= registered, f"packages {packages} not <= registered {registered}"


def test_snippy_resources_are_clamped_to_what_the_container_grants(tmp_path):
    """The schema allows ram_gb up to 32, but the runner caps the container at
    MAX_STAGE_MEMORY_GB. Snippy must never be told it has more memory than the
    container will grant, or it is OOM-killed at exit 137 with nothing in the
    log explaining why. The schema maxima stay high deliberately -- the ceilings
    are env-configurable -- so the reconciliation belongs at the use site.
    """
    from app.config import MAX_STAGE_CPUS, MAX_STAGE_MEMORY_GB
    from app.runtime.context import RunContext, RunPaths
    from app.runtime.spec import CompletedStage, MissingOutput

    calls = []

    class _Stub:
        def run(self, stage, spec, argv, env=None, stdout_path=None):
            calls.append((spec, list(argv)))
            return CompletedStage(stage, 0, 0.0, None, tmp_path / "l.log", "", spec.image)

    paths = RunPaths.for_run(tmp_path / "runs", "run-clamp").create()
    ctx = RunContext(
        run_id="run-clamp",
        params={"cpus": 16, "ram_gb": 16},   # both above the shipped ceilings
        paths=paths,
        refs_dir=tmp_path / "refs",
        stages=["snippy", "annotate", "visualise"],
        input_name="assembly.fasta",
    )
    ctx.runner = _Stub()

    # The stub writes no snps.vcf, so the run stops at the first require().
    with pytest.raises(MissingOutput):
        registry.get("snippy-fasta").run(ctx)

    spec, argv = calls[0]
    told_cpus = int(argv[argv.index("--cpus") + 1])
    told_ram = int(argv[argv.index("--ram") + 1])
    granted_gb = min(spec.memory_gb, MAX_STAGE_MEMORY_GB)

    assert told_cpus <= MAX_STAGE_CPUS
    assert told_ram < granted_gb, "snippy was promised the whole container, leaving no headroom"


def test_effective_stages_bookend_the_declared_ones():
    """The platform fetches before and publishes after every pipeline, and both
    are visible as stages so a 90-second download does not look like a hang."""
    from app.pipelines.base import Pipeline

    class _P(Pipeline):
        id = "x"
        stages = ["a", "b"]

        def run(self, ctx):
            raise NotImplementedError

    assert _P().effective_stages == ("fetch", "a", "b", "publish")


def test_describe_reports_the_effective_stages():
    """The denominator a client polls must match what actually runs."""
    from app.pipelines import registry

    described = registry.get("ont-lsdv").describe()
    assert described["stages"][0] == "fetch"
    assert described["stages"][-1] == "publish"


def test_ont_lsdv_declares_its_primer_bed_as_an_extra_input():
    """Replaces the hardcoded `primer_bed` parameter on the submit endpoint."""
    from app.pipelines import registry

    extras = registry.get("ont-lsdv").extra_inputs
    assert "primer_bed" in extras
    assert extras["primer_bed"].filename == "primers.bed"
    assert extras["primer_bed"].required is False
    assert ".bed" in extras["primer_bed"].extensions


def test_a_pipeline_declaring_a_reserved_stage_fails_the_contract():
    from tests.contract import _assert_stages
    from app.pipelines.base import Pipeline

    class _Clash(Pipeline):
        id = "clash"
        stages = ["publish"]

        def run(self, ctx):
            raise NotImplementedError

    with pytest.raises(AssertionError, match="reserved"):
        _assert_stages(_Clash())
