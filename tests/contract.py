"""Reusable contract assertions.

A new pipeline is covered the moment it is registered -- no new test file. Every
check here corresponds to a mistake that has already been made, or to a
guarantee the platform advertises to Dharini.
"""

from __future__ import annotations

import os
import re

import yaml
from jsonschema import Draft202012Validator

from app.runtime.images import resolver
from app.runtime.manifest import discover_manifests

KEBAB = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
RESERVED_STAGES = {"fetch"}
ACCESSION = re.compile(r"\b[A-Z]{1,2}_?\d{5,9}(\.\d+)?\b")

# Spec §5.1: an empty `sha256` means "not yet pinned", and the contract rejects
# it "outside local development". The shared LSDV reference and GFF3 ship
# unpinned today because the tool that computes and writes their digests
# (`tools/assets.py fetch --write-checksums`) is a later phase, so enforcing it
# unconditionally would fail the build over work nobody has been asked to do
# yet. This flag is the seam: tests/conftest.py sets it, and a deployment or
# release pipeline that leaves it unset gets the strict check.
ALLOW_UNPINNED_ASSETS = "ALLOW_UNPINNED_ASSETS"


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

    # Top-level properties are the sections and fields Dharini draws. A title
    # with no description is a control whose only explanation is its own name,
    # which is exactly how a user picks Clair3 without knowing it costs hours.
    for name, subschema in (schema.get("properties") or {}).items():
        assert subschema.get("title"), (
            f"{pipeline.id}: property {name!r} has no title. Dharini renders it as a form label."
        )
        assert subschema.get("description"), (
            f"{pipeline.id}: property {name!r} has no description. Dharini renders it as the "
            f"field's help text, so it is user-facing copy, not a code comment."
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


def _unpinned_assets_allowed() -> bool:
    return os.getenv(ALLOW_UNPINNED_ASSETS, "").lower() in ("1", "true", "yes")


def _assert_assets(manifest) -> None:
    """Spec §13 #10: every asset declares `url`, `filename` and `sha256`.

    Read from the YAML rather than from the parsed AssetEntry on purpose. The
    parser fills a missing `sha256` with `""` and an AssetEntry cannot tell
    "declared empty" from "never declared", which is the difference between a
    deliberate not-yet-pinned asset and someone forgetting the field entirely.
    """
    raw = yaml.safe_load(manifest.path.read_text()) or {}
    allow_unpinned = _unpinned_assets_allowed()

    for key, value in (raw.get("assets") or {}).items():
        assert isinstance(value, dict), f"{manifest.path}: asset {key!r} must be a mapping"

        for field in ("url", "filename", "sha256"):
            assert field in value, f"{manifest.path}: asset {key!r} declares no {field}"

        for field in ("url", "filename"):
            assert str(value[field] or "").strip(), (
                f"{manifest.path}: asset {key!r} has an empty {field}"
            )

        if allow_unpinned:
            continue

        assert str(value["sha256"] or "").strip(), (
            f"{manifest.path}: asset {key!r} has an empty sha256, so what gets downloaded is "
            f"whatever the URL serves today. Pin it, or set {ALLOW_UNPINNED_ASSETS}=1 for "
            f"local development."
        )


def assert_assets_are_declared_fully() -> None:
    """Applies the asset contract to every scope, shared included.

    The shared manifest owns the LSDV reference and its GFF3 and belongs to no
    pipeline, so a per-pipeline check alone would never look at it.
    """
    for manifest in discover_manifests().values():
        _assert_assets(manifest)


def _assert_manifest(pipeline) -> None:
    manifests = discover_manifests()
    assert pipeline.id in manifests, (
        f"{pipeline.id} has no manifest.yaml declaring scope: {pipeline.id}"
    )
    _assert_assets(manifests[pipeline.id])

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


def _mutable_tag(ref: str) -> str | None:
    """The tag component of a resolved image ref, if it carries one at all.

    A digest-pinned ref (`repo@sha256:...`) has no tag. Otherwise the last
    colon is a tag separator only when nothing after it contains '/' -- that
    is what distinguishes an actual tag from a registry port
    (`localhost:5000/foo:1.0` still ends in the real tag `1.0`, found by the
    *last* colon) and from a build suffix that happens to contain colons.
    `quay.io/biocontainers/minimap2:2.28--he4a0461_0` is a single tag with a
    literal '--' in it, not a second separator. A ref with no colon at all has
    no tag to inspect, so it is left alone here.
    """
    if "@" in ref:
        return None
    _, sep, tag = ref.rpartition(":")
    if not sep or "/" in tag:
        return None
    return tag


def assert_images_are_declared_sanely() -> None:
    """No image may resolve to the mutable tag 'latest'.

    The loop below can never actually fire on its own: `_image()` in
    app/runtime/manifest.py already raises ManifestError for version ==
    "latest" before an ImageEntry is built, and discover_manifests() does not
    catch it -- a manifest that would trip this assertion crashes discovery
    first, taking every other contract test down with it rather than failing
    here. Kept anyway as a cheap, harmless sanity check on the manifests as
    parsed.

    The manifest guard does not cover the two paths that bypass it entirely:
    images.lock.json (a hand-edited or malformed lock entry whose `ref`
    carries `:latest` instead of a digest) and the
    DHARINI_IMAGE__<SCOPE>__<KEY> env override (deliberately unvalidated, as
    a break-glass mechanism). Both feed into the resolver, so this is checked
    against what the resolver actually produced -- the same way
    `_assert_manifest` checks resolved refs against declared repositories.
    """
    for manifest in discover_manifests().values():
        for key, entry in manifest.images.items():
            assert entry.version != "latest", (
                f"{manifest.path}: image {key!r} uses the mutable tag 'latest'"
            )

    for manifest in discover_manifests().values():
        for ref in resolver.refs_for(manifest.scope):
            tag = _mutable_tag(ref)
            assert tag != "latest", (
                f"{manifest.scope} resolved image {ref!r}, which carries the mutable tag 'latest'"
            )
