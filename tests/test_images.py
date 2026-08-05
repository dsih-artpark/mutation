"""Image key resolution: env override, then lock file, then manifest tag."""

import json

import pytest

from app.runtime.images import ImageResolutionError, ImageResolver, _load_lock
from app.runtime.manifest import ImageEntry, Manifest


def _manifest(scope, **images):
    return Manifest(
        scope=scope,
        path=__import__("pathlib").Path(f"/nowhere/{scope}/manifest.yaml"),
        images=images,
        assets={},
        fixtures={},
    )


@pytest.fixture
def resolver():
    manifests = {
        "shared": _manifest(
            "shared",
            snpeff=ImageEntry(
                key="snpeff", source="build",
                repository="artpark/dharini-snpeff", version="5.4-lsdv-1",
                dockerfile="docker/snpeff/Dockerfile",
            ),
        ),
        "ont-lsdv": _manifest(
            "ont-lsdv",
            core=ImageEntry(
                key="core", source="build",
                repository="artpark/dharini-ont-core", version="1.0.0",
                dockerfile="docker/core/Dockerfile",
            ),
        ),
    }
    return ImageResolver(manifests=manifests, lock={})


def test_falls_back_to_the_manifest_tag(resolver):
    assert resolver.resolve("ont-lsdv", "core") == "artpark/dharini-ont-core:1.0.0"


def test_lock_file_digest_wins_over_the_manifest_tag():
    manifests = {
        "ont-lsdv": _manifest(
            "ont-lsdv",
            core=ImageEntry(key="core", source="build",
                            repository="artpark/dharini-ont-core", version="1.0.0",
                            dockerfile="d/Dockerfile"),
        )
    }
    lock = {"ont-lsdv:core": {"ref": "artpark/dharini-ont-core@sha256:deadbeef"}}
    resolver = ImageResolver(manifests=manifests, lock=lock)

    assert resolver.resolve("ont-lsdv", "core") == "artpark/dharini-ont-core@sha256:deadbeef"


def test_environment_override_wins_over_the_lock_file(monkeypatch):
    manifests = {
        "ont-lsdv": _manifest(
            "ont-lsdv",
            core=ImageEntry(key="core", source="build",
                            repository="artpark/dharini-ont-core", version="1.0.0",
                            dockerfile="d/Dockerfile"),
        )
    }
    lock = {"ont-lsdv:core": {"ref": "artpark/dharini-ont-core@sha256:deadbeef"}}
    monkeypatch.setenv("DHARINI_IMAGE__ONT_LSDV__CORE", "localdev/core:wip")
    resolver = ImageResolver(manifests=manifests, lock=lock)

    assert resolver.resolve("ont-lsdv", "core") == "localdev/core:wip"


def test_unknown_scope_raises(resolver):
    with pytest.raises(ImageResolutionError, match="unknown scope"):
        resolver.resolve("nope", "core")


def test_unknown_key_lists_what_is_available(resolver):
    with pytest.raises(ImageResolutionError, match="core"):
        resolver.resolve("ont-lsdv", "typo")


def test_unpinned_reference_is_refused_when_pinning_is_required(resolver, monkeypatch):
    monkeypatch.setattr("app.runtime.images.REQUIRE_PINNED_IMAGES", True)
    with pytest.raises(ImageResolutionError, match="not digest-pinned"):
        resolver.resolve("ont-lsdv", "core")


def test_resolved_refs_are_recorded_against_the_requesting_scope(resolver):
    resolver.resolve("ont-lsdv", "core", requested_by="ont-lsdv")
    resolver.resolve("shared", "snpeff", requested_by="ont-lsdv")

    refs = resolver.refs_for("ont-lsdv")
    assert refs == {
        "artpark/dharini-ont-core:1.0.0",
        "artpark/dharini-snpeff:5.4-lsdv-1",
    }


def test_load_lock_returns_empty_dict_when_top_level_is_not_an_object(tmp_path, monkeypatch):
    lock_path = tmp_path / "images.lock.json"
    lock_path.write_text(json.dumps([1, 2, 3]))
    monkeypatch.setattr("app.runtime.images.LOCK_PATH", lock_path)

    assert _load_lock() == {}


def test_load_lock_returns_empty_dict_when_images_value_is_not_an_object(tmp_path, monkeypatch):
    lock_path = tmp_path / "images.lock.json"
    lock_path.write_text(json.dumps({"images": "oops"}))
    monkeypatch.setattr("app.runtime.images.LOCK_PATH", lock_path)

    assert _load_lock() == {}
