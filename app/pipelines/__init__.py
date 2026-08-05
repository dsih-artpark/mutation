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
