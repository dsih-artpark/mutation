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

import importlib
import pkgutil
import re
from pathlib import Path

import pytest

import app.pipelines as pipelines_package
from app.pipelines import registry
from app.runtime.spec import StageSpec
from tests.contract import (
    assert_accessions_agree,
    assert_assets_are_declared_fully,
    assert_images_are_declared_sanely,
    assert_pipeline_contract,
)

# Synthetic pipelines registered by tests/test_api.py for control-plane tests.
# They live outside app/pipelines, ship no manifest.yaml, and are not part of
# the "adding a pipeline is one new directory" contract this suite enforces.
_TEST_FIXTURE_IDS = {"test-echo", "test-fail"}


def _real_pipelines():
    return [p for p in registry.all() if p.id not in _TEST_FIXTURE_IDS]


def _pipeline_packages() -> list[str]:
    """Directory names under app/pipelines, not registered pipeline ids."""
    return sorted(
        module.name
        for module in pkgutil.iter_modules(pipelines_package.__path__)
        if module.ispkg and not module.name.startswith("_")
    )


@pytest.mark.parametrize("pipeline", _real_pipelines(), ids=lambda p: p.id)
def test_pipeline_satisfies_the_contract(pipeline):
    assert_pipeline_contract(pipeline)


def test_every_manifest_accession_agrees():
    assert_accessions_agree()


def test_no_image_uses_a_mutable_tag():
    assert_images_are_declared_sanely()


def test_every_asset_is_fully_declared():
    """Covers the shared manifest too, which belongs to no pipeline."""
    assert_assets_are_declared_fully()


def test_an_unpinned_asset_is_rejected_when_the_local_dev_flag_is_unset(tmp_path, monkeypatch):
    """The strict half of the checksum rule, proven on a synthetic manifest.

    Asserting it against shared/manifest.yaml would pass today and start
    failing the moment somebody does the right thing and pins those digests.
    """
    from types import SimpleNamespace

    from tests.contract import _assert_assets

    path = tmp_path / "manifest.yaml"
    path.write_text(
        "schema_version: 1\n"
        "scope: synthetic\n"
        "assets:\n"
        "  reference:\n"
        "    url: https://example.invalid/ref.fasta\n"
        "    filename: ref.fasta\n"
        '    sha256: ""\n'
    )
    manifest = SimpleNamespace(path=path)

    monkeypatch.delenv("ALLOW_UNPINNED_ASSETS", raising=False)
    with pytest.raises(AssertionError, match="empty sha256"):
        _assert_assets(manifest)

    monkeypatch.setenv("ALLOW_UNPINNED_ASSETS", "1")
    _assert_assets(manifest)


@pytest.mark.parametrize("missing", ["url", "filename", "sha256"])
def test_an_asset_missing_a_required_field_is_rejected(tmp_path, monkeypatch, missing):
    from types import SimpleNamespace

    from tests.contract import _assert_assets

    fields = {
        "url": "https://example.invalid/ref.fasta",
        "filename": "ref.fasta",
        "sha256": "d0d0" * 16,
    }
    del fields[missing]

    path = tmp_path / "manifest.yaml"
    path.write_text(
        "schema_version: 1\nscope: synthetic\nassets:\n  reference:\n"
        + "".join(f"    {k}: {v}\n" for k, v in fields.items())
    )

    # Set even for the sha256 case: a *missing* key must be rejected whether or
    # not unpinned assets are tolerated -- the flag excuses "not yet", not
    # "never declared".
    monkeypatch.setenv("ALLOW_UNPINNED_ASSETS", "1")
    with pytest.raises(AssertionError, match=f"declares no {missing}"):
        _assert_assets(SimpleNamespace(path=path))


def test_no_stage_spec_runs_outside_a_container():
    """The local execution path applied neither the CPU nor the memory cap."""
    for package in _pipeline_packages():
        try:
            specs = importlib.import_module(f"app.pipelines.{package}.specs")
        except ModuleNotFoundError:
            continue
        for name in dir(specs):
            value = getattr(specs, name)
            if isinstance(value, StageSpec):
                assert value.image, f"{package}.specs.{name} declares no image"


# ------------------------------------------------------- specs.X references

# Matches `specs.CORE_IMAGE`, `specs.ALIGN_TRIM`, and both references on a line
# that names two, such as `specs.SNPEFF.with_(image=specs.CORE_IMAGE)`.
_SPECS_ATTRIBUTE = re.compile(r"\bspecs\.([A-Z][A-Z0-9_]*)")


@pytest.mark.parametrize("package", _pipeline_packages())
def test_every_specs_reference_names_something_that_exists(package):
    """Renaming a constant in specs.py must not stay green until production.

    Renaming `CORE_IMAGE` to `CORE` earlier on this branch broke
    ont_lsdv/primer.py while the whole suite still reported 115 passed:
    importing a module does not evaluate `specs.CORE_IMAGE`, Python resolves
    the attribute only when that line runs, and no test executes ont-lsdv's
    run(). A manual grep step in the plan is not a guard -- this is.

    Static on purpose. Executing the pipelines would need Docker, real
    reference data and a FASTQ; every reference in the package is checkable
    from the source text alone.
    """
    directory = Path(pipelines_package.__path__[0]) / package

    referenced: dict[str, set[str]] = {}
    for source in sorted(directory.rglob("*.py")):
        for match in _SPECS_ATTRIBUTE.finditer(source.read_text()):
            referenced.setdefault(match.group(1), set()).add(source.name)

    if not referenced:
        return

    try:
        specs = importlib.import_module(f"app.pipelines.{package}.specs")
    except ModuleNotFoundError:  # pragma: no cover - a package would be broken
        raise AssertionError(
            f"{package} references {sorted(referenced)} on `specs` but has no specs module"
        ) from None

    missing = {
        name: sorted(files) for name, files in referenced.items() if not hasattr(specs, name)
    }
    assert not missing, (
        f"app/pipelines/{package}/specs.py declares no "
        + ", ".join(f"{name} (used by {', '.join(files)})" for name, files in missing.items())
        + ". A stage will raise AttributeError the first time it runs."
    )
