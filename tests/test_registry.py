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
    assert params["variant"]["tool"] == "bcftools"
    assert params["nanofilt"]["enabled"] is False
    # Haploid: LSDV is a virus. A diploid default would emit impossible calls.
    assert params["bcftools"]["ploidy"] == 1


def test_override_preserves_sibling_defaults():
    params = registry.validate_params("ont-lsdv", {"variant": {"tool": "clair3"}})
    assert params["variant"]["tool"] == "clair3"
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
