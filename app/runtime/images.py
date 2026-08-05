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
