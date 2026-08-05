"""Every registered pipeline must satisfy the contract.

Parametrised over the registry, so a new pipeline is covered the moment it is
registered without anyone writing a test.

tests/test_api.py registers two synthetic pipelines (test-echo, test-fail)
into this same registry singleton to exercise the control plane end to end.
They have no manifest.yaml and declare no images, so the manifest half of the
contract does not apply to them -- they are not real pipelines shipped under
app/pipelines. Rather than weaken assert_pipeline_contract (and risk it going
quiet for an actual pipeline missing a manifest), the contract test is
parametrised over registry.all() filtered down to non-fixture ids; every real
pipeline still runs the full, unweakened contract.
"""

import pytest

from app.pipelines import registry
from app.runtime.spec import StageSpec
from tests.contract import (
    assert_accessions_agree,
    assert_images_are_declared_sanely,
    assert_pipeline_contract,
)

# Synthetic pipelines registered by tests/test_api.py for control-plane tests.
# They live outside app/pipelines, ship no manifest.yaml, and are not part of
# the "adding a pipeline is one new directory" contract this suite enforces.
_TEST_FIXTURE_IDS = {"test-echo", "test-fail"}


def _real_pipelines():
    return [p for p in registry.all() if p.id not in _TEST_FIXTURE_IDS]


@pytest.mark.parametrize("pipeline", _real_pipelines(), ids=lambda p: p.id)
def test_pipeline_satisfies_the_contract(pipeline):
    assert_pipeline_contract(pipeline)


def test_every_manifest_accession_agrees():
    assert_accessions_agree()


def test_no_image_uses_a_mutable_tag():
    assert_images_are_declared_sanely()


def test_no_stage_spec_runs_outside_a_container():
    """The local execution path applied neither the CPU nor the memory cap."""
    import importlib
    import pkgutil

    import app.pipelines as pipelines

    for module in pkgutil.iter_modules(pipelines.__path__):
        if not module.ispkg or module.name.startswith("_"):
            continue
        try:
            specs = importlib.import_module(f"app.pipelines.{module.name}.specs")
        except ModuleNotFoundError:
            continue
        for name in dir(specs):
            value = getattr(specs, name)
            if isinstance(value, StageSpec):
                assert value.image, f"{module.name}.specs.{name} declares no image"
