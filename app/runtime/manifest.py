"""Parsing and validation of pipeline and shared manifests.

A manifest declares what a scope needs from the outside world -- container
images, reference assets, test fixtures -- and nothing about how stages run.
Execution logic stays in Python beside the argv it constrains.

Deliberately importable with only PyYAML, so `tools/` can read manifests
without pulling in FastAPI, Celery or SQLAlchemy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

SCHEMA_VERSION = 1
REPO_ROOT = Path(__file__).resolve().parents[2]


class ManifestError(ValueError):
    """A manifest is malformed. Raised at import time, never at request time."""


@dataclass(frozen=True)
class ImageEntry:
    key: str
    source: str                     # "build" | "registry"
    repository: str
    version: str
    dockerfile: str | None = None
    context: str | None = None
    platform: str = "linux/amd64"
    build_args: dict[str, str] = field(default_factory=dict)

    @property
    def tagged_ref(self) -> str:
        return f"{self.repository}:{self.version}"


@dataclass(frozen=True)
class AssetEntry:
    key: str
    url: str
    filename: str
    sha256: str = ""


@dataclass(frozen=True)
class Manifest:
    scope: str
    path: Path
    images: dict[str, ImageEntry]
    assets: dict[str, AssetEntry]
    fixtures: dict[str, AssetEntry]

    @property
    def dir(self) -> Path:
        return self.path.parent


def load_manifest(path: Path) -> Manifest:
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ManifestError(f"{path}: invalid YAML: {exc}") from exc

    if raw.get("schema_version") != SCHEMA_VERSION:
        raise ManifestError(
            f"{path}: schema_version must be {SCHEMA_VERSION}, "
            f"got {raw.get('schema_version')!r}"
        )

    scope = raw.get("scope")
    if not scope or not isinstance(scope, str):
        raise ManifestError(f"{path}: scope is required and must be a string")

    images = {
        key: _image(path, key, value)
        for key, value in (raw.get("images") or {}).items()
    }
    assets = {
        key: _asset(path, key, value)
        for key, value in (raw.get("assets") or {}).items()
    }
    fixtures = {
        key: _asset(path, key, value, require_checksum=False)
        for key, value in (raw.get("fixtures") or {}).items()
    }
    return Manifest(scope=scope, path=path, images=images, assets=assets, fixtures=fixtures)


def discover_manifests() -> dict[str, Manifest]:
    """Every manifest in the repository, keyed by scope."""
    paths = [REPO_ROOT / "shared" / "manifest.yaml"]
    paths += sorted((REPO_ROOT / "app" / "pipelines").glob("*/manifest.yaml"))

    found: dict[str, Manifest] = {}
    for path in paths:
        if not path.exists():
            continue
        manifest = load_manifest(path)
        if manifest.scope in found:
            raise ManifestError(
                f"duplicate scope {manifest.scope!r}: "
                f"{found[manifest.scope].path} and {path}"
            )
        found[manifest.scope] = manifest
    return found


# -- internals -------------------------------------------------------------

def _image(path: Path, key: str, value: dict) -> ImageEntry:
    if not isinstance(value, dict):
        raise ManifestError(f"{path}: image {key!r} must be a mapping")

    source = value.get("source")
    if source not in ("build", "registry"):
        raise ManifestError(
            f"{path}: image {key!r} source must be 'build' or 'registry', got {source!r}"
        )

    for required in ("repository", "version"):
        if not value.get(required):
            raise ManifestError(f"{path}: image {key!r} requires {required}")

    version = str(value["version"])
    if version == "latest":
        # Two runs a month apart would silently use different tool versions.
        raise ManifestError(f"{path}: image {key!r} may not use the mutable tag 'latest'")

    dockerfile = value.get("dockerfile")
    if source == "build" and not dockerfile:
        raise ManifestError(f"{path}: image {key!r} has source 'build' but no dockerfile")

    context = value.get("context")
    if context is None and dockerfile:
        context = str(Path(dockerfile).parent)

    return ImageEntry(
        key=key,
        source=source,
        repository=str(value["repository"]),
        version=version,
        dockerfile=dockerfile,
        context=context,
        platform=str(value.get("platform", "linux/amd64")),
        build_args={k: str(v) for k, v in (value.get("build_args") or {}).items()},
    )


def _asset(path: Path, key: str, value: dict, require_checksum: bool = True) -> AssetEntry:
    if not isinstance(value, dict):
        raise ManifestError(f"{path}: asset {key!r} must be a mapping")

    for required in ("url", "filename"):
        if not value.get(required):
            raise ManifestError(f"{path}: asset {key!r} requires {required}")

    filename = str(value["filename"])
    if "/" in filename or "\\" in filename or filename in (".", ".."):
        raise ManifestError(
            f"{path}: asset {key!r} filename may not contain a path separator: {filename!r}"
        )

    sha256 = str(value.get("sha256", "") or "")
    if require_checksum and "sha256" not in value:
        raise ManifestError(f"{path}: asset {key!r} requires a sha256 field (may be empty)")

    return AssetEntry(key=key, url=str(value["url"]), filename=filename, sha256=sha256)
