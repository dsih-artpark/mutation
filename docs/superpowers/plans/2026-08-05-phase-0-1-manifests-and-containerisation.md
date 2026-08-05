# Manifests and Full Containerisation — Implementation Plan (Phases 0–1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Declare every container image in a per-scope `manifest.yaml`, resolve images through a digest lock file, and move every pipeline stage into a container so the local execution path can be deleted.

**Architecture:** A `manifest.yaml` beside each pipeline (plus one shared manifest) declares images, assets and fixtures. `app/runtime/images.py` resolves a logical key (`"core"`, `"shared:snpeff-lsdv"`) to a pinned image reference via env override → `images.lock.json` → manifest tag. `StageSpec.image` becomes required, letting `StageRunner` drop its local-execution branch, `_conda_env()` and `_local_peak_mb()`. Custom images are built on micromamba + bioconda with pinned tool versions.

**Tech Stack:** Python 3.13, FastAPI, Celery, SQLAlchemy, pytest, PyYAML, Docker, micromamba/bioconda.

**Scope note:** This plan covers spec Phases 0 and 1 only. Phases 2–3 (assets, S3 storage, `external_id`, `fetch` stage) and Phases 4–5 (`extra_inputs`, `run_local`, fixtures, cleanup) are separate plans written after this one lands. Each produces working software on its own.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-08-04-multi-pipeline-architecture-design.md`
- `schema_version` for all manifests is `1`.
- Docker Hub namespace is `artpark/`. **Confirm before Task 11 pushes anything** — this string appears in every manifest.
- No image `version` may be `latest`.
- Self-built images use `mambaorg/micromamba:1.5-jammy` with pinned bioconda versions.
- snpEff is pinned at `5.4` — the version `snippy-fasta` uses today. Its output must not change.
- Snippy is pinned at `4.6.0`, matching the current conda environment.
- Organism accession is `NC_003027`, database name `LSDV`.
- `tools/*.py` may import only the standard library and `yaml`. Never `app.*`.
- Tests that execute a container use the `bash:5.2` image and are marked `@pytest.mark.docker`.
- Existing test suites must keep passing: `test_api`, `test_context`, `test_registry`, `test_vcf`, `test_webhooks`.

---

## File Structure

**Created:**

| Path | Responsibility |
|---|---|
| `app/runtime/manifest.py` | Parse and validate `manifest.yaml`; discover all manifests |
| `app/runtime/images.py` | Resolve logical image keys to pinned refs; record refs per scope |
| `tools/images.py` | CLI: build, push, lock, pull. Stdlib + yaml only |
| `shared/manifest.yaml` | Shared images (`snpeff-lsdv`, `snippy`) and LSDV assets |
| `shared/docker/snpeff/Dockerfile` | snpEff 5.4 + LSDV database |
| `shared/docker/snippy/Dockerfile` | Snippy 4.6.0, replicating today's conda env |
| `app/pipelines/ont_lsdv/manifest.yaml` | ONT images |
| `app/pipelines/ont_lsdv/docker/core/Dockerfile` | minimap2, samtools, bcftools, fastp, NanoPack |
| `app/pipelines/snippy_fasta/` | `snippy_fasta.py` becomes a package |
| `tests/contract.py` | `assert_pipeline_contract()` — reusable checks |
| `tests/test_all_pipelines.py` | Parametrised over `registry.all()` |
| `tests/test_manifest.py`, `tests/test_images.py` | Unit tests for the new modules |
| `Makefile` | `images-build`, `images-push`, `images-lock`, `images-pull`, `test` |
| `.github/workflows/images.yml` | Build, push, lock on merge |
| `images.lock.json` | Committed digest lock |

**Modified:**

| Path | Change |
|---|---|
| `app/runtime/spec.py` | `image` becomes a required `str` |
| `app/runtime/runner.py` | Extract `docker_argv()`; delete `_run_local`, `_conda_env`, `_local_peak_mb` |
| `app/config.py` | Delete `DOCKER_CONDA`, `DOCKER_CONDA_ALT`, `MAC_CONDA`; add `REQUIRE_PINNED_IMAGES` |
| `app/pipelines/__init__.py` | `pkgutil` scan replaces the import list |
| `app/pipelines/ont_lsdv/specs.py` | `os.getenv` block replaced by `images_for()` |
| `Dockerfile` | `anaconda/miniconda` → `python:3.13-slim` |
| `requirements.txt` | Add `PyYAML` |
| `tests/test_runner.py` | Execution tests containerised; clamping test made real |
| `tests/conftest.py` | Add the `docker` marker and skip logic |
| `.env.example` | Remove the six `ONT_IMAGE_*` vars |

**Deleted:** `setup_snpeff_lsdv.sh`, `Dockerfile_lsdv_core_v4` (content moves into `ont_lsdv/docker/core/Dockerfile`).

---

## Task 1: Manifest parsing and validation

**Files:**
- Create: `app/runtime/manifest.py`
- Test: `tests/test_manifest.py`
- Modify: `requirements.txt`

**Interfaces:**
- Consumes: nothing
- Produces: `ManifestError`, `ImageEntry(key, source, repository, version, dockerfile, context, platform, build_args)` with property `tagged_ref -> str`; `AssetEntry(key, url, filename, sha256)`; `Manifest(scope, path, images: dict[str, ImageEntry], assets: dict[str, AssetEntry], fixtures: dict[str, AssetEntry])` with property `dir -> Path`; `load_manifest(path: Path) -> Manifest`; `discover_manifests() -> dict[str, Manifest]`; `REPO_ROOT: Path`

- [ ] **Step 1: Add PyYAML to requirements**

In `requirements.txt`, after the `jsonschema` block, add:

```
# Manifest parsing. Deliberately also usable by tools/ with no app imports.
PyYAML==6.0.2
```

Then run: `pip install PyYAML==6.0.2`

- [ ] **Step 2: Write the failing tests**

Create `tests/test_manifest.py`:

```python
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
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `pytest tests/test_manifest.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.runtime.manifest'`

- [ ] **Step 4: Implement the module**

Create `app/runtime/manifest.py`:

```python
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
SHARED_SCOPE = "shared"


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
```

- [ ] **Step 5: Run the tests**

Run: `pytest tests/test_manifest.py -v`
Expected: all PASS except `test_discovery_finds_the_shared_scope`, which fails because `shared/manifest.yaml` does not exist yet (created in Task 3).

- [ ] **Step 6: Commit**

```bash
git add app/runtime/manifest.py tests/test_manifest.py requirements.txt
git commit -m "feat(runtime): manifest parsing and validation"
```

---

## Task 2: Image resolution

**Files:**
- Create: `app/runtime/images.py`
- Test: `tests/test_images.py`
- Modify: `app/config.py`

**Interfaces:**
- Consumes: `app.runtime.manifest.{Manifest, ManifestError, discover_manifests, REPO_ROOT, SHARED_SCOPE}`
- Produces: `ImageResolutionError`; `ImageResolver` with `resolve(scope, key, requested_by=None) -> str` and `refs_for(scope) -> set[str]`; `images_for(package: str) -> Callable[[str], str]`; `resolver: ImageResolver` (module singleton); `LOCK_PATH: Path`

- [ ] **Step 1: Add the config flag**

In `app/config.py`, inside the "Container execution" section after `DOCKER_BIN`, add:

```python
# Refuse to resolve an image that is not digest-pinned. Set true in production:
# an unpinned tag means a result cannot be traced to the code that produced it.
REQUIRE_PINNED_IMAGES = os.getenv("REQUIRE_PINNED_IMAGES", "false").lower() in (
    "1", "true", "yes",
)
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_images.py`:

```python
"""Image key resolution: env override, then lock file, then manifest tag."""

import json

import pytest

from app.runtime.images import ImageResolutionError, ImageResolver
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
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `pytest tests/test_images.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.runtime.images'`

- [ ] **Step 4: Implement the module**

Create `app/runtime/images.py`:

```python
"""Resolution of logical image keys to concrete, preferably pinned, references.

A pipeline never writes an image string. It writes `img("core")` or
`img("shared:snpeff-lsdv")`, and resolution happens here so that pinning is a
property of the deployment rather than a discipline someone has to remember.

Order: environment override, then images.lock.json, then the manifest tag.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from typing import Callable

from ..config import REQUIRE_PINNED_IMAGES
from .manifest import REPO_ROOT, Manifest, ManifestError, discover_manifests, load_manifest

LOCK_PATH = REPO_ROOT / "images.lock.json"


class ImageResolutionError(ValueError):
    """An image key cannot be resolved. Raised at import time."""


def _env_var(scope: str, key: str) -> str:
    normalise = lambda text: text.upper().replace("-", "_")  # noqa: E731
    return f"DHARINI_IMAGE__{normalise(scope)}__{normalise(key)}"


def _load_lock() -> dict[str, dict]:
    try:
        data = json.loads(LOCK_PATH.read_text())
    except (OSError, ValueError):
        return {}
    return data.get("images") or {}


class ImageResolver:
    def __init__(self, manifests: dict[str, Manifest] | None = None,
                 lock: dict[str, dict] | None = None):
        self._manifests = discover_manifests() if manifests is None else manifests
        self._lock = _load_lock() if lock is None else lock
        self._resolved: dict[str, set[str]] = {}

    def resolve(self, scope: str, key: str, requested_by: str | None = None) -> str:
        manifest = self._manifests.get(scope)
        if manifest is None:
            known = ", ".join(sorted(self._manifests)) or "(none)"
            raise ImageResolutionError(f"unknown scope {scope!r}. Known scopes: {known}")

        entry = manifest.images.get(key)
        if entry is None:
            known = ", ".join(sorted(manifest.images)) or "(none)"
            raise ImageResolutionError(
                f"scope {scope!r} declares no image {key!r}. Declared: {known}"
            )

        override = os.getenv(_env_var(scope, key))
        if override:
            ref = override
        else:
            locked = self._lock.get(f"{scope}:{key}")
            ref = locked["ref"] if locked and locked.get("ref") else entry.tagged_ref

        if REQUIRE_PINNED_IMAGES and "@sha256:" not in ref:
            raise ImageResolutionError(
                f"{scope}:{key} resolved to {ref!r}, which is not digest-pinned. "
                f"Run 'make images-lock', or unset REQUIRE_PINNED_IMAGES for local work."
            )

        self._resolved.setdefault(requested_by or scope, set()).add(ref)
        return ref

    def refs_for(self, scope: str) -> set[str]:
        """Every image reference this scope has resolved. Used by preflight."""
        return set(self._resolved.get(scope, set()))


resolver = ImageResolver()


def _scope_for_package(package: str) -> str:
    """Read the scope out of the manifest.yaml living in a package directory."""
    spec = importlib.util.find_spec(package)
    if spec is None or not spec.submodule_search_locations:
        raise ImageResolutionError(f"{package!r} is not an importable package")

    path = Path(list(spec.submodule_search_locations)[0]) / "manifest.yaml"
    if not path.exists():
        raise ImageResolutionError(
            f"{package!r} has no manifest.yaml. Every pipeline package needs one."
        )
    try:
        return load_manifest(path).scope
    except ManifestError as exc:
        raise ImageResolutionError(str(exc)) from exc


def images_for(package: str) -> Callable[[str], str]:
    """Return an `img(key)` bound to the calling package's manifest.

    A bare key resolves inside that package's own scope; a `shared:` prefix
    resolves against the shared manifest. There is no implicit fallback between
    the two -- where an image comes from is always explicit.
    """
    scope = _scope_for_package(package)

    def img(key: str) -> str:
        if ":" in key:
            other_scope, other_key = key.split(":", 1)
        else:
            other_scope, other_key = scope, key
        return resolver.resolve(other_scope, other_key, requested_by=scope)

    return img
```

- [ ] **Step 5: Run the tests**

Run: `pytest tests/test_images.py -v`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add app/runtime/images.py tests/test_images.py app/config.py
git commit -m "feat(runtime): resolve image keys via env, lock file, then manifest"
```

---

## Task 3: Write the manifests and Dockerfiles

**Files:**
- Create: `shared/manifest.yaml`, `shared/docker/snpeff/Dockerfile`, `shared/docker/snippy/Dockerfile`
- Create: `app/pipelines/ont_lsdv/manifest.yaml`, `app/pipelines/ont_lsdv/docker/core/Dockerfile`
- Delete: `Dockerfile_lsdv_core_v4`

**Interfaces:**
- Consumes: the manifest schema from Task 1
- Produces: scopes `shared` and `ont-lsdv`; image keys `shared:snpeff-lsdv`, `shared:snippy`, `ont-lsdv:{core,medaka,clair3,longshot,bamclipper}`

- [ ] **Step 1: Create the shared manifest**

Create `shared/manifest.yaml`:

```yaml
# Images and reference data used by more than one pipeline.
#
# NC_003027 appears twice below: once as the snpEff build argument and once in
# each asset URL. The alignment reference and the annotation database MUST come
# from the same accession or variants are annotated against the wrong
# coordinates and nothing errors. tests/contract.py asserts every occurrence
# agrees.

schema_version: 1
scope: shared

images:
  snpeff-lsdv:
    source: build
    dockerfile: docker/snpeff/Dockerfile
    context: docker/snpeff
    repository: artpark/dharini-snpeff
    version: "5.4-lsdv-1"
    platform: linux/amd64
    build_args:
      ACCESSION: NC_003027
      DB_NAME: LSDV

  snippy:
    source: build
    dockerfile: docker/snippy/Dockerfile
    context: docker/snippy
    repository: artpark/dharini-snippy
    version: "4.6.0-1"
    platform: linux/amd64

assets:
  reference:
    url: "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=nuccore&id=NC_003027&rettype=fasta&retmode=text"
    filename: lsdv_reference.fasta
    sha256: ""
  annotation:
    url: "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=nuccore&id=NC_003027&rettype=gff3&retmode=text"
    filename: lsdv_reference.gff3
    sha256: ""
```

- [ ] **Step 2: Create the snpEff Dockerfile**

Create `shared/docker/snpeff/Dockerfile`:

```dockerfile
# snpEff pinned at 5.4 -- the version snippy-fasta uses today -- with the LSDV
# database built in. Parameterised by accession so a second pathogen is a
# manifest entry rather than a second Dockerfile.
FROM mambaorg/micromamba:1.5-jammy

ARG ACCESSION=NC_003027
ARG DB_NAME=LSDV

USER root
RUN micromamba install -y -n base -c bioconda -c conda-forge \
      snpeff=5.4 openjdk=21 curl \
 && micromamba clean -a -y
ENV PATH=/opt/conda/bin:$PATH

# bioconda's install directory carries a build suffix (snpeff-5.4-<build>) that
# the version pin does not determine, so it is discovered rather than assumed --
# then symlinked to a stable path. /snpEff is what both pipelines already pass to
# `snpEff -c`, and is where the previous suhas0 image kept it, so this interface
# survives any future bioconda build-number change with no code change.
#
# `snpEff build` can exit 0 having produced no usable database, which would only
# surface during a real annotation. Assert on the artifact at build time.
RUN set -eux; \
    share="$(ls -d /opt/conda/share/snpeff-* | head -n1)"; \
    ln -s "$share" /snpEff; \
    cfg="/snpEff/snpEff.config"; \
    d="/snpEff/data/${DB_NAME}"; mkdir -p "$d"; \
    base="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=nuccore&id=${ACCESSION}"; \
    curl -fsSL --retry 3 --retry-delay 5 "${base}&rettype=fasta&retmode=text" -o "$d/sequences.fa"; \
    curl -fsSL --retry 3 --retry-delay 5 "${base}&rettype=gff3&retmode=text"  -o "$d/genes.gff"; \
    test -s "$d/sequences.fa"; \
    grep -q '##gff-version 3' "$d/genes.gff"; \
    grep -q "^${DB_NAME}\.genome" "$cfg" || echo "${DB_NAME}.genome : ${DB_NAME}" >> "$cfg"; \
    snpEff build -gff3 -noCheckCds -noCheckProtein -v "${DB_NAME}"; \
    test -s "$d/snpEffectPredictor.bin"; \
    test -s "$cfg"

ENV SNPEFF_CONFIG=/snpEff/snpEff.config
```

The symlink is load-bearing, not cosmetic. [annotate.py:20](app/pipelines/ont_lsdv/annotate.py#L20)
passes `-c /snpEff/snpEff.config` (the `snpeff.config` param default), and
`snippy-fasta` relies on snpEff self-locating its config. Both keep working
unchanged, and no build-number guess appears anywhere in the file.

- [ ] **Step 3: Create the Snippy Dockerfile**

Create `shared/docker/snippy/Dockerfile`:

```dockerfile
# Replicates the package set of the current application conda environment
# exactly, so snippy-fasta produces identical output after containerisation.
#
# snpeff is present because snippy declares it as a dependency; the annotate
# stage uses the separate shared snpeff image, which carries the LSDV database.
FROM mambaorg/micromamba:1.5-jammy

USER root
RUN micromamba install -y -n base -c bioconda -c conda-forge \
      snippy=4.6.0 \
      samtools \
      snpeff=5.4 \
      openjdk=21 \
      perl-bioperl \
      gffread \
      mummer4 \
 && micromamba clean -a -y

ENV PATH=/opt/conda/bin:$PATH
```

- [ ] **Step 4: Create the ONT manifest**

Create `app/pipelines/ont_lsdv/manifest.yaml`:

```yaml
schema_version: 1
scope: ont-lsdv

images:
  # minimap2 and samtools share an image because align.py pipes between them;
  # splitting them would mean materialising an intermediate SAM several times
  # the size of the BAM. Same reason bcftools and NanoPack are here.
  core:
    source: build
    dockerfile: docker/core/Dockerfile
    context: docker/core
    repository: artpark/dharini-ont-core
    version: "1.0.0"
    platform: linux/amd64

  # Separate because their Python, PyTorch/TensorFlow and htslib requirements
  # are irreconcilable. All upstream, so nothing to maintain.
  medaka:
    source: registry
    repository: ontresearch/medaka
    version: "2.0.1"
  clair3:
    source: registry
    repository: hkubal/clair3
    version: "v1.0.10"
  longshot:
    source: registry
    repository: staphb/longshot
    version: "0.4.5"
  bamclipper:
    source: registry
    repository: quay.io/biocontainers/bamclipper
    version: "1.0.0--hdfd78af_2"
```

- [ ] **Step 5: Create the core Dockerfile and delete the orphan**

Create `app/pipelines/ont_lsdv/docker/core/Dockerfile`:

```dockerfile
# Replaces the untracked Dockerfile_lsdv_core_v4, which pinned nothing and drew
# from Ubuntu apt -- so a rebuild silently changed tool versions, and the tools
# were several years old.
FROM mambaorg/micromamba:1.5-jammy

USER root
RUN micromamba install -y -n base -c bioconda -c conda-forge \
      minimap2=2.28 \
      samtools=1.21 \
      bcftools=1.21 \
      fastp=0.24.0 \
      nanofilt=2.8.0 \
      nanoplot=1.42.0 \
 && micromamba clean -a -y

ENV PATH=/opt/conda/bin:$PATH
```

Then: `git rm --cached Dockerfile_lsdv_core_v4 2>/dev/null; rm -f Dockerfile_lsdv_core_v4`

- [ ] **Step 6: Verify discovery finds both scopes**

Run: `pytest tests/test_manifest.py -v`
Expected: all PASS, including `test_discovery_finds_the_shared_scope`

Run: `python -c "from app.runtime.manifest import discover_manifests; print(sorted(discover_manifests()))"`
Expected: `['ont-lsdv', 'shared']`

- [ ] **Step 7: Commit**

```bash
git add shared/ app/pipelines/ont_lsdv/manifest.yaml app/pipelines/ont_lsdv/docker/
git rm -f --ignore-unmatch Dockerfile_lsdv_core_v4
git commit -m "feat(images): declare shared and ONT manifests with pinned bioconda Dockerfiles"
```

---

## Task 4: Convert `snippy_fasta` into a package

**Files:**
- Create: `app/pipelines/snippy_fasta/__init__.py`, `pipeline.py`, `specs.py`, `manifest.yaml`
- Move: `app/visualization.py` → `app/pipelines/snippy_fasta/visualization.py`
- Delete: `app/pipelines/snippy_fasta.py`
- Modify: `app/main.py` (import path for the legacy visualisation endpoint, if present)

**Interfaces:**
- Consumes: `images_for` from Task 2; scope `shared` from Task 3
- Produces: scope `snippy-fasta`; `app.pipelines.snippy_fasta.specs.{SNIPPY, SNPEFF}`

- [ ] **Step 1: Find every import of the visualisation module**

Run: `grep -rn "visualization" --include="*.py" app/ tests/`

Record each hit. `app/pipelines/snippy_fasta.py` imports `generate_visualization`; `app/main.py` and/or `app/api/legacy.py` may import it for the legacy `/visualization` endpoint.

- [ ] **Step 2: Create the package skeleton**

```bash
mkdir -p app/pipelines/snippy_fasta
git mv app/visualization.py app/pipelines/snippy_fasta/visualization.py
```

Create `app/pipelines/snippy_fasta/__init__.py`:

```python
from .pipeline import SnippyFastaPipeline

__all__ = ["SnippyFastaPipeline"]
```

- [ ] **Step 3: Create the manifest**

Create `app/pipelines/snippy_fasta/manifest.yaml`:

```yaml
# This pipeline declares no images of its own: both tools it uses are shared
# with ont-lsdv. The manifest still exists so the scope is declared and the
# contract test can bind the package to it.
schema_version: 1
scope: snippy-fasta

images: {}
```

- [ ] **Step 4: Create specs.py**

Create `app/pipelines/snippy_fasta/specs.py`:

```python
"""Stage images and resource declarations for the FASTA pipeline.

Both images are shared with ont-lsdv: one snpEff image means the same variant
gets the same annotation regardless of which pipeline produced it.
"""

from __future__ import annotations

from ...runtime.images import images_for
from ...runtime.spec import StageSpec

img = images_for(__package__)

_HOUR = 3600

SNIPPY = StageSpec(image=img("shared:snippy"), cpus=2, memory_gb=4, timeout_s=_HOUR)
SNPEFF = StageSpec(image=img("shared:snpeff-lsdv"), cpus=2, memory_gb=4, timeout_s=_HOUR)
```

- [ ] **Step 5: Move the pipeline and switch it to the new specs**

```bash
git mv app/pipelines/snippy_fasta.py app/pipelines/snippy_fasta/pipeline.py
```

In `app/pipelines/snippy_fasta/pipeline.py`, replace the import block and the `LOCAL` constant. Change:

```python
from ..config import REFERENCE, SNPEFF_DB
from ..runtime.context import RunContext, RunResult
from ..runtime.spec import StageSpec
from ..vcf import parse_annotated_vcf, summarise
from ..visualization import generate_visualization
from .base import Pipeline
from .registry import registry

LOCAL = StageSpec(image=None, cpus=2, memory_gb=4, timeout_s=3600)
```

to:

```python
from ...config import REFERENCE, SNPEFF_DB
from ...runtime.context import RunContext, RunResult
from ...vcf import parse_annotated_vcf, summarise
from ..base import Pipeline
from ..registry import registry
from . import specs
from .visualization import generate_visualization
```

Then replace the two `ctx.run` call sites. Change:

```python
            ctx.run(
                LOCAL.with_(cpus=cpus, memory_gb=ram + 1),
```

to:

```python
            ctx.run(
                specs.SNIPPY.with_(cpus=cpus, memory_gb=ram + 1),
```

and change:

```python
            ctx.run(
                LOCAL.with_(memory_gb=4),
                ["snpEff", "-v", SNPEFF_DB, str(raw_vcf)],
```

to:

```python
            ctx.run(
                specs.SNPEFF,
                ["snpEff", "-v", SNPEFF_DB, str(raw_vcf)],
```

- [ ] **Step 6: Fix the remaining visualisation imports**

For each hit recorded in Step 1 outside the pipeline package, change
`from .visualization import ...` or `from ..visualization import ...` to
`from .pipelines.snippy_fasta.visualization import ...` (adjusting the leading
dots for that file's depth).

- [ ] **Step 7: Verify the registry still loads both pipelines**

Run: `python -c "from app.pipelines import registry; print([p.id for p in registry.all()])"`
Expected: `['snippy-fasta', 'ont-lsdv']`

Run: `pytest tests/ -x -q`
Expected: PASS (the runner tests still pass — `image` is not yet required)

- [ ] **Step 8: Commit**

```bash
git add -A app/pipelines/snippy_fasta app/
git commit -m "refactor(snippy-fasta): make it a package with its own manifest and specs"
```

---

## Task 5: Auto-discover pipelines

**Files:**
- Modify: `app/pipelines/__init__.py`
- Test: `tests/test_registry.py`

**Interfaces:**
- Consumes: nothing new
- Produces: no API change — `registry` still exports the same names

- [ ] **Step 1: Write the failing test**

Append to `tests/test_registry.py`:

```python
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

    # Subset, not equality. `tests/test_api.py` registers two synthetic
    # pipelines into the shared registry singleton at module scope, and pytest
    # imports every test module during collection -- before any test body runs
    # -- so `registered` legitimately carries entries with no directory.
    # The direction that matters is preserved: a package on disk that fails to
    # register still fails this assertion.
    assert packages <= registered, f"packages {packages} not all registered: {registered}"
```

- [ ] **Step 2: Run it and note that it already passes**

Run: `pytest tests/test_registry.py::test_every_pipeline_package_is_discovered_without_an_import_line -v`
Expected: PASS

**This one is not a red-green cycle, and pretending otherwise would be
dishonest.** Both packages are currently listed by hand in `__init__.py`, so the
sets already agree. The test is a characterisation test: it locks in the
property so that when someone adds a third package *without* editing
`__init__.py`, the suite proves the scan found it. Step 3 removes the hand-
maintained list; the test passing afterwards is what matters.

- [ ] **Step 3: Replace the import list with a scan**

Replace the whole body of `app/pipelines/__init__.py` with:

```python
"""Pipeline registration by discovery.

Every subpackage of this package is imported at startup, and each one registers
itself by calling `registry.register()` at module scope. Adding a pipeline is
therefore a new directory and no edit to any existing file.

Import failures are re-raised with the offending package name rather than
swallowed: a pipeline that silently vanishes from the registry is far worse to
debug than one that fails loudly at startup.
"""

import pkgutil
from importlib import import_module

from .registry import ParamsInvalid, PipelineNotFound, registry


def _discover() -> None:
    for module in pkgutil.iter_modules(__path__):
        if not module.ispkg or module.name.startswith("_"):
            continue
        try:
            import_module(f".{module.name}", __name__)
        except Exception as exc:
            raise ImportError(
                f"pipeline package {module.name!r} failed to import: {exc}"
            ) from exc


_discover()

__all__ = ["registry", "ParamsInvalid", "PipelineNotFound"]
```

- [ ] **Step 4: Run the tests**

Run: `pytest tests/test_registry.py -v`
Expected: all PASS

Run: `pytest tests/ -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/pipelines/__init__.py tests/test_registry.py
git commit -m "feat(pipelines): discover pipeline packages instead of listing imports"
```

---

## Task 6: Switch the ONT specs to resolved image keys

**Files:**
- Modify: `app/pipelines/ont_lsdv/specs.py`
- Modify: `.env.example`

**Interfaces:**
- Consumes: `images_for` (Task 2), scope `ont-lsdv` (Task 3)
- Produces: `specs.{NANOPLOT, FASTP, NANOFILT, ALIGN, SAMTOOLS, BAMCLIPPER, BCFTOOLS, LONGSHOT, MEDAKA, CLAIR3, SNPEFF}` — same names as before, so no stage module changes

- [ ] **Step 1: Rewrite specs.py**

Replace the whole of `app/pipelines/ont_lsdv/specs.py` with:

```python
"""Tool images and per-stage resource declarations.

Images are named by logical key and resolved through the manifest and lock file
(see app/runtime/images.py), so this module contains no image strings and needs
no edit when an image is repinned.

Tool isolation by image is the reason this design works: Medaka, Clair3 and
Longshot have irreconcilable Python, PyTorch/TensorFlow and htslib
requirements. minimap2, samtools, bcftools and NanoPack share `core` because
the pipeline pipes between them.
"""

from __future__ import annotations

from ...runtime.images import images_for
from ...runtime.spec import StageSpec

img = images_for(__package__)

CORE = img("core")

_HOUR = 3600

# Memory figures are starting estimates for a ~150 kb genome. The run_stages
# table records real peak usage per stage -- after ~20 runs, replace these with
# measured values rather than guesses.
NANOPLOT = StageSpec(image=CORE, cpus=2, memory_gb=4, timeout_s=_HOUR)
FASTP = StageSpec(image=CORE, cpus=2, memory_gb=2, timeout_s=_HOUR)
NANOFILT = StageSpec(image=CORE, cpus=2, memory_gb=2, timeout_s=_HOUR)
ALIGN = StageSpec(image=CORE, cpus=4, memory_gb=6, timeout_s=2 * _HOUR)
SAMTOOLS = StageSpec(image=CORE, cpus=2, memory_gb=2, timeout_s=_HOUR)
BAMCLIPPER = StageSpec(image=img("bamclipper"), cpus=2, memory_gb=4, timeout_s=_HOUR)

BCFTOOLS = StageSpec(image=CORE, cpus=2, memory_gb=4, timeout_s=2 * _HOUR)
LONGSHOT = StageSpec(image=img("longshot"), cpus=2, memory_gb=4, timeout_s=2 * _HOUR)

# Neural-network callers. CPU-only inference on Lightsail (no GPU available),
# so these dominate wall-clock; budget accordingly.
MEDAKA = StageSpec(image=img("medaka"), cpus=4, memory_gb=8, timeout_s=4 * _HOUR)
CLAIR3 = StageSpec(image=img("clair3"), cpus=4, memory_gb=8, timeout_s=4 * _HOUR)

SNPEFF = StageSpec(image=img("shared:snpeff-lsdv"), cpus=2, memory_gb=4, timeout_s=_HOUR)
```

- [ ] **Step 2: Remove the stale environment variables**

In `.env.example`, delete the entire final block from the `# ONT tool images` comment header through `ONT_IMAGE_SNPEFF=suhas0/snpeff_lsdv:v1`, and replace it with:

```
# ---------------------------------------------------------------------------
# Container images
#
# Images are declared in shared/manifest.yaml and app/pipelines/*/manifest.yaml
# and pinned by digest in images.lock.json. Nothing to set here.
#
# Set true in production so the worker refuses to start on an unpinned image:
# REQUIRE_PINNED_IMAGES=true
#
# Break-glass override for a single image, no code change needed:
# DHARINI_IMAGE__ONT_LSDV__CORE=localdev/core:wip
# ---------------------------------------------------------------------------
REQUIRE_PINNED_IMAGES=false
```

- [ ] **Step 3: Verify resolution works end to end**

Run:
```bash
python -c "
from app.pipelines.ont_lsdv import specs
print(specs.ALIGN.image)
print(specs.SNPEFF.image)
from app.runtime.images import resolver
print(sorted(resolver.refs_for('ont-lsdv')))
"
```
Expected: `artpark/dharini-ont-core:1.0.0`, then `artpark/dharini-snpeff:5.4-lsdv-1`,
then a sorted list of the **six** distinct refs this pipeline resolves — `core`
(shared by five stages), `bamclipper`, `longshot`, `medaka`, `clair3`, and
`shared:snpeff-lsdv`.

Run: `pytest tests/ -q`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add app/pipelines/ont_lsdv/specs.py .env.example
git commit -m "feat(ont-lsdv): resolve tool images through the manifest"
```

---

## Task 7: Require an image on every stage and simplify the runner

**Files:**
- Modify: `app/runtime/spec.py:24`
- Modify: `app/runtime/runner.py`
- Modify: `app/config.py:144-146`
- Modify: `tests/test_runner.py`, `tests/conftest.py`

**Interfaces:**
- Consumes: nothing new
- Produces: `app.runtime.runner.docker_argv(container: str, workdir: Path, mounts: list[tuple[Path, bool]], spec: StageSpec, argv: list[str], env: dict[str, str]) -> list[str]` — a pure function, testable without Docker

- [ ] **Step 1: Add the docker marker to conftest**

In `tests/conftest.py`, after the imports, add:

```python
def pytest_configure(config):
    config.addinivalue_line(
        "markers", "docker: test executes a real container; skipped when Docker is absent"
    )


import functools


@functools.lru_cache(maxsize=1)
def docker_available() -> bool:
    """Probed once, and only when a docker-marked test actually asks.

    Deliberately not evaluated at import: `docker info` costs about a second,
    and the fast suite must not pay it.
    """
    import subprocess

    try:
        return subprocess.run(
            ["docker", "info"], capture_output=True, timeout=15
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
```

And add this fixture at the end of the file:

```python
@pytest.fixture
def docker_required():
    if not docker_available():
        pytest.skip("Docker is not available")
```

- [ ] **Step 2: Rewrite the runner tests**

Replace the whole of `tests/test_runner.py` with:

```python
"""Safety properties of the stage runner.

These are the tests that matter most: every one corresponds to a defect found in
the original ONT delivery. If one fails, a class of bug has come back.

Since every stage now runs in a container, the execution tests use a real one.
`bash:5.2` is tiny and provides bash, sh, echo, printf, sleep and false. Tests
that only exercise validation or command construction need no Docker at all.
"""

from pathlib import Path

import pytest

from app.config import MAX_STAGE_CPUS, MAX_STAGE_MEMORY_GB
from app.runtime.runner import StageRunner, docker_argv, sh
from app.runtime.spec import StageFailed, StageSpec, StageTimeout

BASH = "bash:5.2"


@pytest.fixture
def runner(tmp_path):
    return StageRunner("test1234", tmp_path, [(tmp_path, False)], tmp_path / "logs")


@pytest.fixture
def spec():
    return StageSpec(image=BASH, cpus=1, memory_gb=1, timeout_s=120)


# -- validation: no Docker needed -----------------------------------------

def test_shell_string_is_rejected(runner, spec):
    """argv must be a list. A string would imply shell interpretation."""
    with pytest.raises(TypeError, match="never a shell string"):
        runner.run("x", spec, "echo hello")


def test_non_string_argv_is_rejected(runner, spec):
    with pytest.raises(TypeError):
        runner.run("x", spec, ["echo", 42])


def test_malformed_env_key_is_rejected(runner, spec):
    with pytest.raises(ValueError, match="invalid environment variable name"):
        runner.run("x", spec, ["echo"], env={"BAD;NAME": "value"})


def test_sh_uses_strict_bash_flags():
    """pipefail matters: without it a failing tool mid-pipe is invisible."""
    argv = sh('echo "$X"')
    assert argv[0] == "bash"
    assert "-euo" in argv and "pipefail" in argv


# -- command construction: no Docker needed -------------------------------

def test_resources_are_clamped_to_the_ceiling(tmp_path):
    """A mis-declared spec must not be able to claim the whole box.

    Asserts on the command actually handed to Docker, not on a min() call.
    """
    cmd = docker_argv(
        "c", tmp_path, [], StageSpec(image=BASH, cpus=999, memory_gb=999), ["true"], {}
    )
    assert cmd[cmd.index("--cpus") + 1] == f"{MAX_STAGE_CPUS:g}"
    assert cmd[cmd.index("--memory") + 1] == f"{MAX_STAGE_MEMORY_GB:g}g"


def test_memory_swap_equals_memory_so_a_runaway_stage_is_oom_killed(tmp_path):
    cmd = docker_argv(
        "c", tmp_path, [], StageSpec(image=BASH, cpus=1, memory_gb=2), ["true"], {}
    )
    assert cmd[cmd.index("--memory") + 1] == cmd[cmd.index("--memory-swap") + 1]


def test_mounts_use_identical_host_and_container_paths(tmp_path):
    """Path parity: the daemon resolves bind sources on the host, so a path the
    worker computes must also be valid inside the container."""
    ro = tmp_path / "refs"
    ro.mkdir()
    cmd = docker_argv(
        "c", tmp_path, [(tmp_path, False), (ro, True)],
        StageSpec(image=BASH), ["true"], {},
    )
    assert f"{tmp_path}:{tmp_path}" in cmd
    assert f"{ro}:{ro}:ro" in cmd


def test_entrypoint_override_is_passed_through(tmp_path):
    cmd = docker_argv(
        "c", tmp_path, [], StageSpec(image=BASH, entrypoint="/bin/sh"), ["true"], {}
    )
    assert cmd[cmd.index("--entrypoint") + 1] == "/bin/sh"


def test_with_returns_a_modified_copy():
    base = StageSpec(image="a", cpus=2, memory_gb=4)
    bumped = base.with_(memory_gb=16)
    assert bumped.memory_gb == 16
    assert bumped.image == "a" and bumped.cpus == 2
    assert base.memory_gb == 4, "with_() mutated the original"


# -- execution: needs Docker ----------------------------------------------

@pytest.mark.docker
def test_shell_metacharacters_in_arguments_are_inert(runner, spec, tmp_path, docker_required):
    """The original interpolated filenames into a shell string; a filename
    containing `;` was remote code execution. Here it is just text."""
    output = tmp_path / "out.txt"
    canary = tmp_path / "canary"
    canary.write_text("untouched")

    runner.run("echo", spec, ["echo", f"x; rm -f {canary}"], stdout_path=output)

    assert output.read_text().strip() == f"x; rm -f {canary}"
    assert canary.read_text() == "untouched", "argument was interpreted by a shell"


@pytest.mark.docker
def test_env_values_are_not_reparsed(runner, spec, tmp_path, docker_required):
    """The sh() escape hatch: a constant script with data via quoted env vars."""
    output = tmp_path / "out.txt"
    canary = tmp_path / "canary2"
    canary.write_text("untouched")

    runner.run(
        "sh", spec, sh('printf "%s" "$VALUE"'),
        env={"VALUE": f"y; rm -f {canary}"},
        stdout_path=output,
    )

    assert output.read_text() == f"y; rm -f {canary}"
    assert canary.read_text() == "untouched"


@pytest.mark.docker
def test_non_zero_exit_raises_rather_than_exiting(runner, spec, docker_required):
    """The original called sys.exit(1), which kills the Celery worker."""
    with pytest.raises(StageFailed) as exc:
        runner.run("false", spec, ["false"])
    assert exc.value.exit_code == 1


@pytest.mark.docker
def test_failure_carries_the_log_tail(runner, spec, docker_required):
    with pytest.raises(StageFailed) as exc:
        runner.run("boom", spec, ["sh", "-c", "echo 'detailed reason' >&2; exit 7"])
    assert exc.value.exit_code == 7
    assert "detailed reason" in exc.value.log_tail


@pytest.mark.docker
def test_timeout_is_enforced(runner, docker_required):
    with pytest.raises(StageTimeout):
        runner.run("sleep", StageSpec(image=BASH, timeout_s=1), ["sleep", "30"])


@pytest.mark.docker
def test_pipefail_catches_a_failure_mid_pipe(runner, spec, docker_required):
    with pytest.raises(StageFailed):
        runner.run("pipe", spec, sh("false | cat"))


@pytest.mark.docker
def test_logs_are_written_per_stage(runner, spec, tmp_path, docker_required):
    runner.run("first", spec, ["echo", "hello"])
    logs = list((tmp_path / "logs").glob("*.log"))
    assert len(logs) == 1
    assert "first" in logs[0].name
    assert "echo" in logs[0].read_text()
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `pytest tests/test_runner.py -v`
Expected: FAIL — `ImportError: cannot import name 'docker_argv'`

- [ ] **Step 4: Make `image` required**

In `app/runtime/spec.py`, replace the `StageSpec` docstring and the `image` field. Change:

```python
    """How to execute one tool.

    `image=None` means "run on the worker host" -- used by pipelines whose tools
    are installed directly in the application image (snippy-fasta). Everything
    else names a container image, which is how tools with irreconcilable
    dependencies (Medaka vs Clair3 vs Longshot) coexist.

    cpus/memory_gb are advisory requests; the runner clamps them to the
    MAX_STAGE_* ceiling so a mis-declared spec cannot claim the whole box.
    """

    image: str | None = None
```

to:

```python
    """How to execute one tool.

    Every stage names a container image. That is how tools with irreconcilable
    dependencies (Medaka vs Clair3 vs Longshot) coexist, and it is what makes
    the resource caps below real: the local execution path applied neither, so
    a pipeline running on the host could claim the whole box.

    cpus/memory_gb are advisory requests; the runner clamps them to the
    MAX_STAGE_* ceiling so a mis-declared spec cannot claim the whole box.
    """

    image: str
```

Also change `CompletedStage.image` from `str | None = None` to `str = ""`.

- [ ] **Step 5: Extract `docker_argv` and delete the local path**

In `app/runtime/runner.py`:

Change the config import block from:

```python
from ..config import (
    DEFAULT_STAGE_TIMEOUT_S,
    DOCKER_BIN,
    DOCKER_CONDA,
    DOCKER_CONDA_ALT,
    MAC_CONDA,
    MAX_STAGE_CPUS,
    MAX_STAGE_MEMORY_GB,
    STATS_SAMPLE_INTERVAL_S,
)
```

to:

```python
from ..config import (
    DEFAULT_STAGE_TIMEOUT_S,
    DOCKER_BIN,
    MAX_STAGE_CPUS,
    MAX_STAGE_MEMORY_GB,
    STATS_SAMPLE_INTERVAL_S,
)
```

Delete the entire `_conda_env()` function and the entire `_local_peak_mb()` function.

Add this module-level function immediately after `_parse_mem`:

```python
def docker_argv(
    container: str,
    workdir: Path,
    mounts: list[tuple[Path, bool]],
    spec: StageSpec,
    argv: list[str],
    env: dict[str, str],
) -> list[str]:
    """Build the full `docker run` command for a stage.

    Pure and module-level so the safety properties -- resource clamping, path
    parity, swap denial -- can be asserted on the real command without starting
    a container.
    """
    cpus = min(spec.cpus, MAX_STAGE_CPUS)
    memory_gb = min(spec.memory_gb, MAX_STAGE_MEMORY_GB)

    cmd = [
        DOCKER_BIN, "run", "--rm",
        "--name", container,
        "--cpus", f"{cpus:g}",
        "--memory", f"{memory_gb:g}g",
        # Equal to --memory: denies the container swap, so a runaway stage is
        # OOM-killed promptly instead of thrashing the host.
        "--memory-swap", f"{memory_gb:g}g",
        "-w", str(workdir),
    ]

    seen: set[Path] = set()
    for path, read_only in [*mounts, *((p, True) for p in spec.extra_mounts)]:
        path = Path(path).resolve()
        if path in seen:
            continue
        seen.add(path)
        # Identical source and destination -- the path-parity contract.
        cmd += ["-v", f"{path}:{path}" + (":ro" if read_only else "")]

    for key, value in env.items():
        cmd += ["-e", f"{key}={value}"]

    if spec.entrypoint is not None:
        cmd += ["--entrypoint", spec.entrypoint]

    cmd.append(spec.image)
    cmd += list(argv)
    return cmd
```

Replace the dispatch in `StageRunner.run`. Change:

```python
        started = time.monotonic()
        if spec.image:
            exit_code, peak_mb = self._run_container(stage, spec, argv, env, log_path, stdout_path)
        else:
            exit_code, peak_mb = self._run_local(spec, argv, env, log_path, stdout_path)
        duration = time.monotonic() - started
```

to:

```python
        started = time.monotonic()
        exit_code, peak_mb = self._run_container(stage, spec, argv, env, log_path, stdout_path)
        duration = time.monotonic() - started
```

Replace the body of `_run_container` above the sampler block. Change:

```python
    def _run_container(self, stage, spec, argv, env, log_path, stdout_path):
        cpus = min(spec.cpus, MAX_STAGE_CPUS)
        memory_gb = min(spec.memory_gb, MAX_STAGE_MEMORY_GB)
        container = self._container_name(stage)

        cmd = [
            DOCKER_BIN, "run", "--rm",
            "--name", container,
            "--cpus", f"{cpus:g}",
            "--memory", f"{memory_gb:g}g",
            # Equal to --memory: denies the container swap, so a runaway stage
            # is OOM-killed promptly instead of thrashing the host.
            "--memory-swap", f"{memory_gb:g}g",
            "-w", str(self.workdir),
        ]

        seen: set[Path] = set()
        for path, read_only in [*self.mounts, *((p, True) for p in spec.extra_mounts)]:
            path = Path(path).resolve()
            if path in seen:
                continue
            seen.add(path)
            # Identical source and destination -- the path-parity contract.
            cmd += ["-v", f"{path}:{path}" + (":ro" if read_only else "")]

        for key, value in env.items():
            cmd += ["-e", f"{key}={value}"]

        if spec.entrypoint is not None:
            cmd += ["--entrypoint", spec.entrypoint]

        cmd.append(spec.image)
        cmd += list(argv)

        sampler = None
```

to:

```python
    def _run_container(self, stage, spec, argv, env, log_path, stdout_path):
        container = self._container_name(stage)
        cmd = docker_argv(container, self.workdir, self.mounts, spec, argv, env)

        sampler = None
```

Finally, delete the entire `_run_local` method and its `# -- local execution` comment header.

- [ ] **Step 6: Drop the conda prefixes from config**

In `app/config.py`, delete the final section entirely:

```python
# --------------------------------------------------------------------------
# Conda prefixes for locally-executed stages (snippy-fasta pipeline)
# --------------------------------------------------------------------------

DOCKER_CONDA = "/opt/miniconda3/envs/pipeline"
DOCKER_CONDA_ALT = "/opt/conda/envs/pipeline"
MAC_CONDA = "/opt/homebrew/Caskroom/miniconda/base/envs/lsdv_pipeline"
```

- [ ] **Step 7: Run the tests**

Run: `pytest tests/test_runner.py -v -m "not docker"`
Expected: all PASS

Run: `docker pull bash:5.2 && pytest tests/test_runner.py -v`
Expected: all PASS, including the `docker`-marked tests

Run: `pytest tests/ -q`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add app/runtime/spec.py app/runtime/runner.py app/config.py tests/test_runner.py tests/conftest.py
git commit -m "refactor(runtime): require a container image on every stage

Deletes _run_local, _conda_env and _local_peak_mb. The local path applied
neither the CPU nor the memory cap, so snippy-fasta's declared resources were
silently ignored and MAX_STAGE_MEMORY_GB did not bind. Extracts docker_argv()
so clamping and path parity are asserted on the real command."
```

---

## Task 8: Contract test kit

**Files:**
- Create: `tests/contract.py`, `tests/test_all_pipelines.py`

**Interfaces:**
- Consumes: `registry` (Task 5), `discover_manifests` (Task 1), `resolver` (Task 2)
- Produces: `assert_pipeline_contract(pipeline) -> None`

- [ ] **Step 1: Write the contract kit**

Create `tests/contract.py`:

```python
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
```

- [ ] **Step 2: Write the parametrised suite**

Create `tests/test_all_pipelines.py`:

```python
"""Every registered pipeline must satisfy the contract.

Parametrised over the registry, so a new pipeline is covered the moment it is
registered without anyone writing a test.
"""

import pytest

from app.pipelines import registry
from app.runtime.spec import StageSpec
from tests.contract import (
    assert_accessions_agree,
    assert_images_are_declared_sanely,
    assert_pipeline_contract,
)


@pytest.mark.parametrize("pipeline", registry.all(), ids=lambda p: p.id)
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
```

- [ ] **Step 3: Run the suite and expect the title check to fail**

Run: `pytest tests/test_all_pipelines.py -v`
Expected: FAIL for `ont-lsdv` — seven top-level properties in its `params_schema`
have no `title`, so Dharini would render them with a raw key as the form label.
`snippy-fasta` passes; all four of its properties already have titles.

- [ ] **Step 4: Add the seven missing titles**

In `app/pipelines/ont_lsdv/pipeline.py`, add a `"title"` to each of these
top-level properties. Do not weaken the assertion — these are user-facing form
labels.

| Property | Add |
|---|---|
| `filter` | `"title": "Read filtering"` |
| `fastp` | `"title": "fastp"` |
| `bcftools` | `"title": "bcftools"` |
| `medaka` | `"title": "Medaka"` |
| `clair3` | `"title": "Clair3"` |
| `consensus` | `"title": "Consensus"` |
| `snpeff` | `"title": "snpEff annotation"` |

For example, change:

```python
            "filter": {
                "type": "object", "additionalProperties": False,
                "properties": {
```

to:

```python
            "filter": {
                "type": "object", "additionalProperties": False,
                "title": "Read filtering",
                "properties": {
```

- [ ] **Step 5: Run the suite again**

Run: `pytest tests/test_all_pipelines.py -v`
Expected: all PASS

Run: `pytest tests/ -q -m "not docker"`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add tests/contract.py tests/test_all_pipelines.py app/pipelines/ont_lsdv/pipeline.py
git commit -m "test: contract kit parametrised over the registry

Also adds the seven missing form titles the kit surfaced in ont-lsdv."
```

---

## Task 9: The `tools/images.py` CLI

**Files:**
- Create: `tools/__init__.py`, `tools/images.py`

**Interfaces:**
- Consumes: `manifest.yaml` files directly via PyYAML. **Must not import `app.*`.**
- Produces: CLI `build | push | lock | pull`; writes `images.lock.json`

- [ ] **Step 1: Create the package marker**

```bash
mkdir -p tools && touch tools/__init__.py
```

- [ ] **Step 2: Write the CLI**

Create `tools/images.py`:

```python
#!/usr/bin/env python3
"""Build, push, pin and pull the container images every pipeline declares.

Deliberately imports only the standard library and PyYAML: CI builds a container
without installing FastAPI, Celery, SQLAlchemy or boto3.

    python tools/images.py build [--scope ont-lsdv]
    python tools/images.py push  [--scope ont-lsdv]
    python tools/images.py lock
    python tools/images.py pull
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = REPO_ROOT / "images.lock.json"


def manifest_paths() -> list[Path]:
    paths = [REPO_ROOT / "shared" / "manifest.yaml"]
    paths += sorted((REPO_ROOT / "app" / "pipelines").glob("*/manifest.yaml"))
    return [p for p in paths if p.exists()]


def load_images() -> dict[str, tuple[dict, Path]]:
    """Every declared image, keyed 'scope:key', with its manifest directory."""
    images: dict[str, tuple[dict, Path]] = {}
    for path in manifest_paths():
        raw = yaml.safe_load(path.read_text()) or {}
        scope = raw["scope"]
        for key, entry in (raw.get("images") or {}).items():
            images[f"{scope}:{key}"] = (entry, path.parent)
    return images


def tagged_ref(entry: dict) -> str:
    return f"{entry['repository']}:{entry['version']}"


def run(cmd: list[str]) -> None:
    print("$", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)


def capture(cmd: list[str]) -> str:
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def selected(scope: str | None) -> dict[str, tuple[dict, Path]]:
    images = load_images()
    if scope is None:
        return images
    chosen = {k: v for k, v in images.items() if k.split(":", 1)[0] == scope}
    if not chosen:
        sys.exit(f"no images declared for scope {scope!r}")
    return chosen


def cmd_build(args) -> None:
    for name, (entry, base) in selected(args.scope).items():
        if entry.get("source") != "build":
            continue
        dockerfile = base / entry["dockerfile"]
        context = base / (entry.get("context") or Path(entry["dockerfile"]).parent)
        cmd = [
            "docker", "build",
            "--platform", entry.get("platform", "linux/amd64"),
            "-f", str(dockerfile),
            "-t", tagged_ref(entry),
        ]
        for key, value in (entry.get("build_args") or {}).items():
            cmd += ["--build-arg", f"{key}={value}"]
        cmd.append(str(context))
        print(f"\n=== build {name} ===")
        run(cmd)


def cmd_push(args) -> None:
    for name, (entry, _) in selected(args.scope).items():
        if entry.get("source") != "build":
            continue
        print(f"\n=== push {name} ===")
        run(["docker", "push", tagged_ref(entry)])


def cmd_lock(args) -> None:
    """Resolve every image to a digest, uniformly for built and pulled images."""
    locked: dict[str, dict] = {}
    for name, (entry, _) in sorted(load_images().items()):
        ref = tagged_ref(entry)
        if entry.get("source") == "registry":
            run(["docker", "pull", "--platform", entry.get("platform", "linux/amd64"), ref])

        raw = capture(["docker", "inspect", "--format", "{{index .RepoDigests 0}}", ref])
        digest = raw.split("@", 1)[1] if "@" in raw else ""
        if not digest:
            sys.exit(
                f"{name}: {ref} has no repo digest. A built image must be pushed "
                f"before it can be locked."
            )
        locked[name] = {
            "repository": entry["repository"],
            "version": str(entry["version"]),
            "digest": digest,
            "ref": f"{entry['repository']}@{digest}",
        }
        print(f"{name} -> {locked[name]['ref']}")

    LOCK_PATH.write_text(json.dumps({"schema_version": 1, "images": locked}, indent=2) + "\n")
    print(f"\nwrote {LOCK_PATH}")


def cmd_pull(args) -> None:
    try:
        lock = json.loads(LOCK_PATH.read_text())["images"]
    except (OSError, KeyError, ValueError):
        sys.exit(f"{LOCK_PATH} is missing or malformed. Run 'make images-lock' first.")

    for name, entry in sorted(lock.items()):
        print(f"\n=== pull {name} ===")
        run(["docker", "pull", entry["ref"]])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    for name, handler in (
        ("build", cmd_build), ("push", cmd_push), ("lock", cmd_lock), ("pull", cmd_pull),
    ):
        child = sub.add_parser(name)
        child.set_defaults(handler=handler)
        if name in ("build", "push"):
            child.add_argument("--scope", default=None, help="limit to one manifest scope")

    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Verify it reads the manifests without importing the app**

Run: `python tools/images.py build --scope nope`
Expected: exits with `no images declared for scope 'nope'`

Run: `python -c "
import sys
sys.modules['fastapi'] = None
from tools.images import load_images
print(sorted(load_images()))
"`
Expected: prints all seven keys — proving no `app.*` import is triggered.

- [ ] **Step 4: Commit**

```bash
git add tools/
git commit -m "feat(tools): build, push, lock and pull images from manifests"
```

---

## Task 10: Slim the application image

**Files:**
- Modify: `Dockerfile`
- Delete: `setup_snpeff_lsdv.sh`

`docker-compose.yml` needs no change: the socket mount, `group_add` and path-
parity volumes are all still correct.

**Interfaces:**
- Consumes: nothing
- Produces: an application image with no conda and no bioinformatics tools

- [ ] **Step 1: Replace the Dockerfile**

Replace the whole of `Dockerfile` with:

```dockerfile
# The application image runs no bioinformatics tools -- every stage executes in
# its own container through the host Docker daemon. So this is a plain Python
# image plus the Docker CLI, which is why it builds in seconds and why the
# JAVA_HOME / PERL5LIB / conda-prefix handling the old image needed is gone.
FROM --platform=linux/amd64 python:3.13-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl gnupg \
 && rm -rf /var/lib/apt/lists/*

# Docker CLI only -- no daemon. The worker talks to the host's daemon over the
# mounted socket to start per-tool containers (see docker-compose.yml).
RUN install -m 0755 -d /etc/apt/keyrings \
 && curl -fsSL https://download.docker.com/linux/debian/gpg \
      | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
 && chmod a+r /etc/apt/keyrings/docker.gpg \
 && echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.gpg] \
    https://download.docker.com/linux/debian $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
    > /etc/apt/sources.list.d/docker.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends docker-ce-cli \
 && rm -rf /var/lib/apt/lists/*

# Dependencies before source, so a code change does not invalidate this layer.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app

ENV PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "info"]
```

- [ ] **Step 2: Delete the snpEff setup script**

The LSDV database is now built into `shared/docker/snpeff/Dockerfile`, and the
reference FASTA becomes a declared asset in Plan 2.

```bash
git rm -f setup_snpeff_lsdv.sh
```

- [ ] **Step 3: Verify the image builds and is small**

Run: `docker build -t dharini-app:test .`
Expected: SUCCESS, in well under a minute

Run: `docker images dharini-app:test --format '{{.Size}}'`
Expected: a few hundred MB, versus multiple GB for the conda image

- [ ] **Step 4: Commit**

```bash
git add Dockerfile
git rm -f --ignore-unmatch setup_snpeff_lsdv.sh
git commit -m "build: slim the application image to python:3.13-slim + docker CLI"
```

---

## Task 11: Makefile, CI workflow and the first lock file

**Files:**
- Create: `Makefile`, `.github/workflows/images.yml`, `images.lock.json`

**Interfaces:**
- Consumes: `tools/images.py` (Task 9)
- Produces: `make images-build|images-push|images-lock|images-pull|test|check`

- [ ] **Step 1: Confirm the Docker Hub namespace**

Before anything is pushed, confirm the organisation is `artpark` on Docker Hub.
If it differs, update `repository:` in all three manifests:

```bash
grep -rn "repository: artpark/" shared/manifest.yaml app/pipelines/*/manifest.yaml
```

- [ ] **Step 2: Write the Makefile**

Create `Makefile`:

```makefile
.PHONY: help images-build images-push images-lock images-pull test test-fast check

help:
	@grep -E '^[a-z-]+:.*?##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/'

images-build:  ## Build every image declared with source: build
	python tools/images.py build $(if $(SCOPE),--scope $(SCOPE),)

images-push:   ## Push every built image to the registry
	python tools/images.py push $(if $(SCOPE),--scope $(SCOPE),)

images-lock:   ## Resolve digests and write images.lock.json
	python tools/images.py lock

images-pull:   ## Pull every image at its locked digest
	python tools/images.py pull

test:          ## Full suite, including container-backed runner tests
	docker pull bash:5.2
	pytest tests/ -v

test-fast:     ## Skip anything that needs Docker
	pytest tests/ -q -m "not docker"

check:         ## Contract and manifest validation only
	pytest tests/test_all_pipelines.py tests/test_manifest.py tests/test_images.py -q
```

- [ ] **Step 3: Write the CI workflow**

Create `.github/workflows/images.yml`:

```yaml
name: images

on:
  push:
    branches: [main]
    paths:
      - 'shared/**'
      - 'app/pipelines/*/manifest.yaml'
      - 'app/pipelines/*/docker/**'
      - 'tools/images.py'
  workflow_dispatch:

jobs:
  build-push-lock:
    runs-on: ubuntu-latest
    permissions:
      contents: write
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: '3.13'

      - run: pip install PyYAML==6.0.2

      - uses: docker/login-action@v3
        with:
          username: ${{ secrets.DOCKERHUB_USERNAME }}
          password: ${{ secrets.DOCKERHUB_TOKEN }}

      - run: python tools/images.py build
      - run: python tools/images.py push
      - run: python tools/images.py lock

      - name: Commit the lock file
        run: |
          git config user.name  "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          if ! git diff --quiet images.lock.json; then
            git add images.lock.json
            git commit -m "chore(images): refresh digest lock"
            git push
          fi
```

- [ ] **Step 4: Build and lock locally**

This is the first real build. Expect it to take several minutes and to surface
gates G5 (snpEff base paths) and G7 (bioconda solver) from the spec.

Run: `make images-build`
Expected: all three self-built images build. If the snpEff `test -s
snpEffectPredictor.bin` assertion fails, the database did not build — inspect
the layer output rather than removing the assertion.

Run: `docker run --rm artpark/dharini-snippy:4.6.0-1 snippy --version`
Expected: `snippy 4.6.0`

Run: `docker run --rm artpark/dharini-ont-core:1.0.0 minimap2 --version`
Expected: `2.28`

- [ ] **Step 5: Push and lock**

```bash
docker login
make images-push
make images-lock
```

Expected: `images.lock.json` is written with seven entries, each carrying a
`sha256:` digest.

- [ ] **Step 6: Verify pinned resolution now works**

Run:
```bash
REQUIRE_PINNED_IMAGES=true python -c "
from app.pipelines.ont_lsdv import specs
print(specs.ALIGN.image)
"
```
Expected: `artpark/dharini-ont-core@sha256:…` and no exception.

Run: `make test`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add Makefile .github/workflows/images.yml images.lock.json
git commit -m "build: Makefile, image CI workflow, and the first digest lock"
```

---

## Verification before this plan is considered done

- [ ] `make test` passes with Docker available
- [ ] `make test-fast` passes without Docker
- [ ] `python -c "from app.pipelines import registry; print([p.id for p in registry.all()])"` → `['ont-lsdv', 'snippy-fasta']` in some order
- [ ] `grep -rn "image=None" app/` → no matches
- [ ] `grep -rn "_conda_env\|MAC_CONDA\|DOCKER_CONDA" app/` → no matches
- [ ] `grep -rn "ONT_IMAGE_" . --include="*.py" --include="*.example"` → no matches
- [ ] **Gate G1 (spec §16):** run `snippy-fasta` on a known FASTA before and after this plan; `snps.vcf` and `annotated.vcf` must be identical. Snippy 4.6.0 and snpEff 5.4 are unchanged, so any difference means a packaging problem, not a version change.

Gates G2 (ONT end-to-end), G3 (bamclipper dependencies) and G4 (Medaka/Clair3)
belong to the plan for Phases 2–3, once assets and the `fetch` stage exist.

---

## Deferred to later plans

| Item | Plan |
|---|---|
| `ctx.asset()`, `REFS_ROOT/<scope>/`, `tools/assets.py`, `reference` enum | Phase 2 |
| `ObjectStore`, `POST /v1/uploads`, JSON `POST /v1/runs`, `external_id`, `fetch` stage, preflight, `Run` migration | Phase 3 |
| `extra_inputs`, `run_local.py`, tier-1 fixtures, `make new-pipeline` | Phase 4 |
| Delete `ONTpipeline/`, rewrite `readme.md` and `PIPELINE_AUTHORING.md` | Phase 5 |
