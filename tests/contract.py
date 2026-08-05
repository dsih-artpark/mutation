"""Reusable contract assertions.

A new pipeline is covered the moment it is registered -- no new test file. Every
check here corresponds to a mistake that has already been made, or to a
guarantee the platform advertises to Dharini.
"""

from __future__ import annotations

import re

from jsonschema import Draft202012Validator

from app.runtime.images import resolver
from app.runtime.manifest import discover_manifests

KEBAB = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
RESERVED_STAGES = {"fetch"}
ACCESSION = re.compile(r"\b[A-Z]{1,2}_?\d{5,9}(\.\d+)?\b")


def assert_pipeline_contract(pipeline) -> None:
    _assert_identity(pipeline)
    _assert_params_schema(pipeline)
    _assert_stages(pipeline)
    _assert_filenames(pipeline)
    _assert_manifest(pipeline)


def _assert_identity(pipeline) -> None:
    assert pipeline.id, f"{type(pipeline).__name__} declares no id"
    assert KEBAB.match(pipeline.id), f"pipeline id {pipeline.id!r} must be kebab-case"
    assert pipeline.version, f"{pipeline.id} declares no version"
    assert pipeline.display_name, f"{pipeline.id} declares no display_name"
    assert pipeline.description, f"{pipeline.id} declares no description"
    assert pipeline.accepted_extensions, f"{pipeline.id} accepts no file extensions"


def _assert_params_schema(pipeline) -> None:
    schema = pipeline.params_schema
    Draft202012Validator.check_schema(schema)
    _assert_closed(schema, pipeline.id, path="(root)")

    for name, subschema in (schema.get("properties") or {}).items():
        assert subschema.get("title"), (
            f"{pipeline.id}: property {name!r} has no title. Dharini renders it as a form label."
        )


def _assert_closed(schema: dict, pipeline_id: str, path: str) -> None:
    """additionalProperties: False at every level, and a default on every leaf.

    Without the former, {"treads": 8} is silently accepted and ignored. Without
    the latter, ctx.param() has to carry a fallback at every use site.
    """
    if schema.get("type") != "object":
        return

    assert schema.get("additionalProperties") is False, (
        f"{pipeline_id}: {path} must set additionalProperties: False"
    )

    for name, subschema in (schema.get("properties") or {}).items():
        if not isinstance(subschema, dict):
            continue
        child = f"{path}.{name}"
        if subschema.get("type") == "object":
            _assert_closed(subschema, pipeline_id, child)
        else:
            assert "default" in subschema, f"{pipeline_id}: {child} has no default"


def _assert_stages(pipeline) -> None:
    stages = list(pipeline.stages)
    assert stages, f"{pipeline.id} declares no stages"
    assert len(stages) == len(set(stages)), f"{pipeline.id} declares a duplicate stage"

    clash = RESERVED_STAGES.intersection(stages)
    assert not clash, f"{pipeline.id} declares reserved stage name(s): {sorted(clash)}"


def _assert_filenames(pipeline) -> None:
    name = pipeline.input_filename
    assert name and "/" not in name and "\\" not in name, (
        f"{pipeline.id}: input_filename {name!r} may not contain a path separator"
    )


def _assert_manifest(pipeline) -> None:
    manifests = discover_manifests()
    assert pipeline.id in manifests, (
        f"{pipeline.id} has no manifest.yaml declaring scope: {pipeline.id}"
    )

    repositories = {
        entry.repository
        for manifest in manifests.values()
        for entry in manifest.images.values()
    }

    # Every image this pipeline resolved must trace back to a declared repository,
    # whether it came from the lock file, an env override or the manifest tag.
    for ref in resolver.refs_for(pipeline.id):
        repository = ref.split("@", 1)[0]
        if "@" not in ref:
            head, _, tail = repository.rpartition(":")
            # A colon is a tag separator only when it is not a registry port,
            # i.e. when no '/' follows it.
            if head and "/" not in tail:
                repository = head
        assert repository in repositories, (
            f"{pipeline.id} resolved image {ref!r}, whose repository {repository!r} "
            f"no manifest declares"
        )


def assert_accessions_agree() -> None:
    """Every organism accession in a manifest file must be the same one.

    The alignment reference and the snpEff database must derive from the same
    accession or variants are annotated against the wrong coordinates, and
    nothing errors.
    """
    for manifest in discover_manifests().values():
        text = manifest.path.read_text()
        found = {match.group(0) for match in ACCESSION.finditer(text)}
        assert len(found) <= 1, (
            f"{manifest.path} mentions more than one accession: {sorted(found)}. "
            f"The reference and the snpEff database must agree."
        )


def assert_images_are_declared_sanely() -> None:
    for manifest in discover_manifests().values():
        for key, entry in manifest.images.items():
            assert entry.version != "latest", (
                f"{manifest.path}: image {key!r} uses the mutable tag 'latest'"
            )
