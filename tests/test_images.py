"""Image key resolution: env override, then lock file, then manifest tag.

Second half of the file covers tools/images.py, which answers the same question
for the command line and may not import app.* (see its module docstring), so it
restates those three rules. Those tests exist to keep the two copies honest.
"""

import argparse
import json
from pathlib import Path

import pytest
import yaml

from app.runtime.images import ImageResolutionError, ImageResolver, _env_var, _load_lock
from app.runtime.manifest import ImageEntry, Manifest
from tools import images as images_tool


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
                repository="dsihartpark/dharini-snpeff", version="5.4-lsdv-1",
                dockerfile="docker/snpeff/Dockerfile",
            ),
        ),
        "ont-lsdv": _manifest(
            "ont-lsdv",
            core=ImageEntry(
                key="core", source="build",
                repository="dsihartpark/dharini-ont-core", version="1.0.0",
                dockerfile="docker/core/Dockerfile",
            ),
        ),
    }
    return ImageResolver(manifests=manifests, lock={})


def test_falls_back_to_the_manifest_tag(resolver):
    assert resolver.resolve("ont-lsdv", "core") == "dsihartpark/dharini-ont-core:1.0.0"


def test_lock_file_digest_wins_over_the_manifest_tag():
    manifests = {
        "ont-lsdv": _manifest(
            "ont-lsdv",
            core=ImageEntry(key="core", source="build",
                            repository="dsihartpark/dharini-ont-core", version="1.0.0",
                            dockerfile="d/Dockerfile"),
        )
    }
    lock = {"ont-lsdv:core": {"ref": "dsihartpark/dharini-ont-core@sha256:deadbeef"}}
    resolver = ImageResolver(manifests=manifests, lock=lock)

    assert resolver.resolve("ont-lsdv", "core") == "dsihartpark/dharini-ont-core@sha256:deadbeef"


def test_environment_override_wins_over_the_lock_file(monkeypatch):
    manifests = {
        "ont-lsdv": _manifest(
            "ont-lsdv",
            core=ImageEntry(key="core", source="build",
                            repository="dsihartpark/dharini-ont-core", version="1.0.0",
                            dockerfile="d/Dockerfile"),
        )
    }
    lock = {"ont-lsdv:core": {"ref": "dsihartpark/dharini-ont-core@sha256:deadbeef"}}
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
        "dsihartpark/dharini-ont-core:1.0.0",
        "dsihartpark/dharini-snpeff:5.4-lsdv-1",
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


# ===========================================================================
# tools/images.py -- the catalogue behind `make images-list`
# ===========================================================================
#
# tools/images.py is stdlib + PyYAML with no app.* imports, so that CI and the
# dev container can run it without FastAPI, Celery or SQLAlchemy installed. The
# cost of that rule is a second copy of the resolution order, and the tests
# below are what stop the two copies drifting apart in silence.


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def synthetic_repo(tmp_path, monkeypatch):
    """A repository shaped like this one, with scopes nothing has been told about.

    Two pipelines and a shared scope, none of them named in tools/images.py or
    in this file's assertions by anything other than what is written to disk
    here -- so discovery has to be a walk for these tests to pass.
    """
    _write(tmp_path / "shared" / "manifest.yaml", """
schema_version: 1
scope: shared
images:
  toolbox:
    source: build
    dockerfile: docker/toolbox/Dockerfile
    repository: dsihartpark/toolbox
    version: "2.0.0"
""")
    _write(tmp_path / "app" / "pipelines" / "alpha" / "manifest.yaml", """
schema_version: 1
scope: alpha
images:
  aligner:
    source: registry
    repository: example/aligner
    version: "1.2.3"
  orphan:
    source: registry
    repository: example/orphan
    version: "9.9.9"
""")
    _write(
        tmp_path / "app" / "pipelines" / "alpha" / "specs.py",
        'img = images_for(__package__)\n'
        'ALIGN = StageSpec(image=img("aligner"))\n'
        'BOX = StageSpec(image=img("shared:toolbox"))\n',
    )
    _write(tmp_path / "app" / "pipelines" / "beta" / "manifest.yaml", """
schema_version: 1
scope: beta
images: {}
""")
    _write(
        tmp_path / "app" / "pipelines" / "beta" / "specs.py",
        'img = images_for(__package__)\nBOX = StageSpec(image=img("shared:toolbox"))\n',
    )

    monkeypatch.setattr(images_tool, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(images_tool, "LOCK_PATH", tmp_path / "images.lock.json")
    return tmp_path


# -- discovery ---------------------------------------------------------------

def test_every_pipeline_package_shipping_a_manifest_reaches_the_catalogue():
    """Fails the moment the discovery walk stops finding a scope.

    The expected set is walked off disk rather than written here, for the same
    reason tests/contract.py parametrises over the registry: a sixth pipeline
    has to be covered without anybody editing a test. Cross-checked with
    iterdir() rather than the glob manifest_paths() itself uses, so a broken
    glob cannot make both sides agree on nothing.
    """
    root = Path(images_tool.REPO_ROOT)
    packages = sorted(
        directory for directory in (root / "app" / "pipelines").iterdir()
        if (directory / "manifest.yaml").exists()
    )
    assert len(packages) >= 2, (
        "this test cross-checks discovery against a walk of app/pipelines, and "
        "that walk found fewer than two pipelines -- it is proving nothing"
    )

    discovered = images_tool.scopes()
    assert "shared" in discovered, "shared/manifest.yaml is not being discovered"
    for directory in packages:
        scope = yaml.safe_load((directory / "manifest.yaml").read_text())["scope"]
        assert scope in discovered, (
            f"{directory.name} declares scope {scope!r}, which `images-list` never shows"
        )


def test_a_scope_nobody_registered_anywhere_is_still_listed(synthetic_repo):
    """The point of the command: an author adding a pipeline directory gets it
    in the catalogue without touching tools/images.py."""
    assert set(images_tool.scopes()) == {"shared", "alpha", "beta"}
    assert {row["scope"] for row in images_tool.catalogue()} == {"shared", "alpha"}


def test_a_scope_declaring_no_images_of_its_own_is_not_an_error(synthetic_repo):
    """beta reuses shared:toolbox and declares nothing. Listing it must succeed
    -- that combination is the recommended one, not a broken manifest."""
    assert images_tool.catalogue(scope="beta") == []


def test_an_unknown_scope_says_which_scopes_exist(synthetic_repo):
    with pytest.raises(SystemExit, match="alpha, beta, shared"):
        images_tool.catalogue(scope="typo")


# -- resolution: the same three rules app/runtime/images.py applies -----------

def test_the_manifest_tag_is_what_shows_when_nothing_overrides_it(synthetic_repo):
    row = next(r for r in images_tool.catalogue() if r["key"] == "aligner")
    assert (row["ref"], row["via"]) == ("example/aligner:1.2.3", "manifest")


def test_a_locked_digest_shows_instead_of_the_tag(synthetic_repo):
    (synthetic_repo / "images.lock.json").write_text(json.dumps({
        "schema_version": 1,
        "images": {"alpha:aligner": {"ref": "example/aligner@sha256:deadbeef"}},
    }))

    row = next(r for r in images_tool.catalogue() if r["key"] == "aligner")
    assert (row["ref"], row["via"]) == ("example/aligner@sha256:deadbeef", "lock")


def test_an_environment_override_shows_instead_of_the_locked_digest(synthetic_repo, monkeypatch):
    (synthetic_repo / "images.lock.json").write_text(json.dumps({
        "images": {"alpha:aligner": {"ref": "example/aligner@sha256:deadbeef"}},
    }))
    monkeypatch.setenv("DHARINI_IMAGE__ALPHA__ALIGNER", "localdev/aligner:wip")

    row = next(r for r in images_tool.catalogue() if r["key"] == "aligner")
    assert (row["ref"], row["via"]) == ("localdev/aligner:wip", "env")


@pytest.mark.parametrize(
    "scope, key",
    [("ont-lsdv", "core"), ("shared", "snpeff-lsdv"), ("snippy-fasta", "x")],
)
def test_the_override_variable_name_agrees_with_the_resolver(scope, key):
    """Two implementations of one naming rule. A developer who reads the name
    out of `images-list` and exports it must not find that nothing happens."""
    assert images_tool.env_var(scope, key) == _env_var(scope, key)


def test_every_declared_image_resolves_identically_in_both_implementations():
    """The end-to-end version of the rule above, over the real manifests.

    A fresh ImageResolver rather than the module singleton: resolve() records
    what it resolved, and tests/contract.py asserts against those records.
    """
    rows = images_tool.catalogue()
    assert rows, "no images discovered at all, so this proves nothing"

    resolver = ImageResolver()
    for row in rows:
        assert row["ref"] == resolver.resolve(row["scope"], row["key"]), (
            f"{row['scope']}:{row['key']} resolves differently in tools/images.py "
            f"than in app/runtime/images.py"
        )


# -- what makes a shared image visibly shared --------------------------------

def test_a_shared_image_is_shown_under_the_key_an_author_would_type(synthetic_repo):
    rows = {row["key"]: row for row in images_tool.catalogue()}
    assert rows["toolbox"]["write_as"] == "shared:toolbox"
    assert rows["aligner"]["write_as"] == "aligner", (
        "a pipeline's own image is reached by the bare key; qualifying it here "
        "would teach authors to write a prefix that does not resolve"
    )


def test_a_shared_image_names_every_pipeline_that_uses_it(synthetic_repo):
    """The reuse signal. Two pipelines call img('shared:toolbox') and neither
    declares it, so only a scan of the calls can report both."""
    rows = {row["key"]: row for row in images_tool.catalogue()}
    assert rows["toolbox"]["used_by"] == ["alpha", "beta"]
    assert rows["aligner"]["used_by"] == ["alpha"]


def test_an_image_nothing_calls_is_reported_as_used_by_nothing(synthetic_repo):
    """`orphan` is declared and never referenced -- the state that produces the
    seventh copy of an image somebody forgot they already had."""
    rows = {row["key"]: row for row in images_tool.catalogue()}
    assert rows["orphan"]["used_by"] == []


def test_shared_images_are_only_ever_attributed_to_real_pipeline_scopes():
    pipelines = set(images_tool.scopes()) - {"shared"}
    shared = [row for row in images_tool.catalogue() if row["scope"] == "shared"]
    assert shared, "the shared scope declares no images"
    assert any(row["used_by"] for row in shared), (
        "no shared image is used by any pipeline, which means the img() scan "
        "has stopped matching the calls in specs.py"
    )
    for row in shared:
        assert set(row["used_by"]) <= pipelines


# -- local presence ----------------------------------------------------------

def test_presence_is_three_valued_not_two(synthetic_repo):
    """"Docker is not reachable from here" must not render as "you have pulled
    nothing" -- one is a broken environment, the other is a normal first run."""
    unknown = images_tool.catalogue(present=None)
    assert all(row["local"] is None for row in unknown)

    known = images_tool.catalogue(present={"example/aligner:1.2.3"})
    by_key = {row["key"]: row for row in known}
    assert by_key["aligner"]["local"] is True
    assert by_key["orphan"]["local"] is False


def test_untagged_daemon_rows_are_not_mistaken_for_images(monkeypatch):
    """`docker images` prints <none>:<none> for dangling layers. Treating those
    as references would make an unrelated leftover mark an image as present."""
    monkeypatch.setattr(
        images_tool, "capture",
        lambda cmd: "example/a:1\texample/a@sha256:aa\n<none>:<none>\t<none>@<none>",
    )
    assert images_tool.local_refs() == {"example/a:1", "example/a@sha256:aa"}


def test_an_unreachable_daemon_reports_unknown_rather_than_raising(monkeypatch):
    def unreachable(cmd):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(images_tool, "capture", unreachable)
    assert images_tool.local_refs() is None


# -- contract: one registry namespace for everything we build ----------------

# The namespace was moved repo-wide from artpark/ to dsihartpark/. An image
# published under a personal account or a typo of this is not caught by any
# build or push -- `docker push` succeeds against whatever it is told -- so it
# is caught here instead. Changing this constant is a deliberate act.
BUILD_NAMESPACE = "dsihartpark"


def test_every_built_image_shares_one_registry_namespace():
    built = {
        name: entry["repository"]
        for name, (entry, _) in images_tool.load_images().items()
        if entry.get("source") == "build"
    }
    assert built, "no image declares source: build, so this contract is vacuous"

    wrong = {
        name: repository for name, repository in built.items()
        if repository.split("/", 1)[0] != BUILD_NAMESPACE
    }
    assert not wrong, (
        f"images we build must all publish under {BUILD_NAMESPACE}/, but "
        + ", ".join(f"{name} declares {repo}" for name, repo in sorted(wrong.items()))
        + ". A push to a personal or misspelled namespace succeeds silently."
    )


def test_a_stray_namespace_is_actually_caught(synthetic_repo):
    """The assertion above passes today; this proves it would fail if it should.

    Without this, deleting the body of that test would leave both green.
    """
    manifest = synthetic_repo / "app" / "pipelines" / "alpha" / "manifest.yaml"
    manifest.write_text(
        "schema_version: 1\nscope: alpha\nimages:\n  aligner:\n"
        "    source: build\n    dockerfile: docker/aligner/Dockerfile\n"
        "    repository: someones-laptop/aligner\n    version: \"1.2.3\"\n"
    )

    with pytest.raises(AssertionError, match="someones-laptop/aligner"):
        test_every_built_image_shares_one_registry_namespace()


# -- pull honours the declared platform --------------------------------------

def test_the_lock_file_records_a_platform_for_every_image():
    """Without it `docker pull` resolves against the host architecture.

    Every image we publish is a single-platform (linux/amd64) manifest list, so
    a bare `docker pull <digest>` on an arm64 developer machine fails with
    "no matching manifest for linux/arm64/v8" -- which reads as a broken or
    missing image rather than a pull asking for the wrong architecture. This is
    the first command the readme tells a new developer to run.
    """
    lock = json.loads(images_tool.LOCK_PATH.read_text())["images"]
    assert lock, "images.lock.json has no entries"

    missing = sorted(name for name, entry in lock.items() if not entry.get("platform"))
    assert not missing, f"no platform recorded for: {', '.join(missing)}"


def test_pull_passes_the_locked_platform_to_docker(tmp_path, monkeypatch):
    """The recorded platform has to reach the command line, not just the file."""
    lock_path = tmp_path / "images.lock.json"
    lock_path.write_text(json.dumps({
        "schema_version": 1,
        "images": {
            "demo:tool": {
                "repository": "dsihartpark/demo",
                "version": "1.0.0",
                "platform": "linux/amd64",
                "digest": "sha256:abc",
                "ref": "dsihartpark/demo@sha256:abc",
            }
        },
    }))
    monkeypatch.setattr(images_tool, "LOCK_PATH", lock_path)

    calls: list[list[str]] = []
    monkeypatch.setattr(images_tool, "run", lambda cmd: calls.append(list(cmd)))

    images_tool.cmd_pull(argparse.Namespace())

    assert calls == [[
        "docker", "pull", "--platform", "linux/amd64", "dsihartpark/demo@sha256:abc",
    ]]


def test_pull_defaults_the_platform_for_a_lock_written_before_it_existed(tmp_path, monkeypatch):
    """An older lock file must still pull, not crash on a missing key."""
    lock_path = tmp_path / "images.lock.json"
    lock_path.write_text(json.dumps({
        "schema_version": 1,
        "images": {"demo:tool": {"ref": "dsihartpark/demo@sha256:abc"}},
    }))
    monkeypatch.setattr(images_tool, "LOCK_PATH", lock_path)

    calls: list[list[str]] = []
    monkeypatch.setattr(images_tool, "run", lambda cmd: calls.append(list(cmd)))

    images_tool.cmd_pull(argparse.Namespace())

    assert calls[0][:4] == ["docker", "pull", "--platform", "linux/amd64"]
