"""The snippy-fasta reference must resolve inside REFS_ROOT.

REFS_ROOT is the only references directory the runner mounts into a tool
container. A reference outside it is readable by the application and invisible
to the tool, which is how `--ref /app/data/lsdv_reference_sequence.fasta`
reached snippy as "Invalid --reference filename": the path existed in the
application image and nowhere else. It only ever worked while the since-deleted
local execution path ran tools in-process.
"""

from __future__ import annotations

import importlib

import pytest


def _reload_config(monkeypatch, refs_root, **env):
    monkeypatch.setenv("REFS_ROOT", str(refs_root))
    for key in ("REFERENCE_PATH", "REFERENCE_FILENAME"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    import app.config

    return importlib.reload(app.config)


def test_reference_resolves_inside_refs_root(monkeypatch, tmp_path):
    config = _reload_config(monkeypatch, tmp_path)

    assert config.REFERENCE.parent == config.REFS_ROOT
    assert config.REFERENCE.name == "lsdv_reference_sequence.fasta"


def test_reference_filename_is_honoured(monkeypatch, tmp_path):
    config = _reload_config(monkeypatch, tmp_path, REFERENCE_FILENAME="other.fasta")

    assert config.REFERENCE == (tmp_path / "other.fasta").resolve()


def test_legacy_reference_path_outside_refs_root_is_rejected(monkeypatch, tmp_path):
    """Failing at import is deliberate. The alternative is a service that starts
    cleanly and fails every snippy run minutes later with an error naming a file
    rather than the setting that produced it."""
    with pytest.raises(RuntimeError, match="outside REFS_ROOT"):
        _reload_config(
            monkeypatch, tmp_path, REFERENCE_PATH="/app/data/lsdv_reference_sequence.fasta"
        )


def test_legacy_reference_path_inside_refs_root_still_works(monkeypatch, tmp_path):
    inside = tmp_path / "custom.fasta"
    config = _reload_config(monkeypatch, tmp_path, REFERENCE_PATH=str(inside))

    assert config.REFERENCE == inside.resolve()


@pytest.fixture(autouse=True)
def _restore_config():
    """config.py reads the environment at import, so a reload inside a test
    leaves the module bound to that test's paths. Restore it or every later
    test in the session sees a tmp_path that no longer exists."""
    yield
    import app.config

    importlib.reload(app.config)
