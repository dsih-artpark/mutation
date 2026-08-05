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


# -- an unusable lock file must be loud -------------------------------------
#
# Falling back to a mutable tag is correct when there is no lock file and wrong
# to do silently when there is one: production believes it is digest-pinned.

def test_an_absent_lock_file_is_silent(tmp_path, monkeypatch, capsys):
    """The normal local-development state. Warning here would train people to
    ignore the warning that matters."""
    monkeypatch.setattr("app.runtime.images.LOCK_PATH", tmp_path / "images.lock.json")

    assert _load_lock() == {}
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    "content, expected",
    [
        ("{ not json", "not valid JSON"),
        (json.dumps([1, 2, 3]), "top-level list"),
        (json.dumps({"schema_version": 1}), "no 'images' object"),
        (json.dumps({"images": "oops"}), "'images' value of type str"),
    ],
    ids=["malformed-json", "top-level-list", "images-missing", "images-not-an-object"],
)
def test_a_corrupt_lock_file_warns_on_stderr(tmp_path, monkeypatch, capsys, content, expected):
    lock_path = tmp_path / "images.lock.json"
    lock_path.write_text(content)
    monkeypatch.setattr("app.runtime.images.LOCK_PATH", lock_path)

    assert _load_lock() == {}

    stderr = capsys.readouterr().err
    assert expected in stderr
    assert "NOT" in stderr and "digest-pinned" in stderr, (
        "the warning must say what the consequence is, not just that a file is bad"
    )
    assert str(lock_path) in stderr


def test_an_unreadable_lock_file_warns_rather_than_raising(tmp_path, monkeypatch, capsys):
    """A directory where the lock file should be: read_text raises OSError, not
    FileNotFoundError, so it must take the warn-and-degrade path."""
    lock_path = tmp_path / "images.lock.json"
    lock_path.mkdir()
    monkeypatch.setattr("app.runtime.images.LOCK_PATH", lock_path)

    assert _load_lock() == {}
    assert "could not be read" in capsys.readouterr().err
