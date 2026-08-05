"""Manifest parsing and validation."""

import pytest

from app.runtime.manifest import ManifestError, discover_manifests, load_manifest

VALID = """
schema_version: 1
scope: demo
images:
  core:
    source: build
    dockerfile: docker/core/Dockerfile
    repository: artpark/demo-core
    version: "1.0.0"
    build_args:
      FOO: bar
  upstream:
    source: registry
    repository: staphb/longshot
    version: "0.4.5"
assets:
  reference:
    url: "https://example.invalid/ref.fasta"
    filename: ref.fasta
    sha256: "abc123"
"""


def _write(tmp_path, text):
    path = tmp_path / "manifest.yaml"
    path.write_text(text)
    return path


def test_parses_images_and_assets(tmp_path):
    manifest = load_manifest(_write(tmp_path, VALID))

    assert manifest.scope == "demo"
    assert manifest.images["core"].source == "build"
    assert manifest.images["core"].build_args == {"FOO": "bar"}
    assert manifest.images["upstream"].tagged_ref == "staphb/longshot:0.4.5"
    assert manifest.assets["reference"].filename == "ref.fasta"
    assert manifest.dir == tmp_path


def test_context_defaults_to_the_dockerfile_directory(tmp_path):
    manifest = load_manifest(_write(tmp_path, VALID))
    assert manifest.images["core"].context == "docker/core"


def test_latest_tag_is_rejected(tmp_path):
    text = VALID.replace('version: "0.4.5"', 'version: "latest"')
    with pytest.raises(ManifestError, match="latest"):
        load_manifest(_write(tmp_path, text))


def test_unknown_source_is_rejected(tmp_path):
    text = VALID.replace("source: build", "source: magic")
    with pytest.raises(ManifestError, match="source"):
        load_manifest(_write(tmp_path, text))


def test_build_without_dockerfile_is_rejected(tmp_path):
    text = VALID.replace("    dockerfile: docker/core/Dockerfile\n", "")
    with pytest.raises(ManifestError, match="dockerfile"):
        load_manifest(_write(tmp_path, text))


def test_wrong_schema_version_is_rejected(tmp_path):
    text = VALID.replace("schema_version: 1", "schema_version: 99")
    with pytest.raises(ManifestError, match="schema_version"):
        load_manifest(_write(tmp_path, text))


def test_asset_filename_may_not_contain_a_path_separator(tmp_path):
    text = VALID.replace("filename: ref.fasta", "filename: ../../etc/passwd")
    with pytest.raises(ManifestError, match="filename"):
        load_manifest(_write(tmp_path, text))


def test_discovery_finds_the_shared_scope():
    manifests = discover_manifests()
    assert "shared" in manifests
