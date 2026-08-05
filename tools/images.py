#!/usr/bin/env python3
"""Build, push, pin, pull and list the container images every pipeline declares.

Deliberately imports only the standard library and PyYAML: CI builds a container
without installing FastAPI, Celery, SQLAlchemy or boto3.

    python tools/images.py build [--scope ont-lsdv]
    python tools/images.py push  [--scope ont-lsdv]
    python tools/images.py lock
    python tools/images.py pull
    python tools/images.py list  [--scope ont-lsdv] [--json]

`list` is the answer to "which images already exist?". Nobody should have to
open five manifests to find out that the image they are about to declare is
already in the shared scope.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = REPO_ROOT / "images.lock.json"
SHARED_SCOPE = "shared"


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


# -- the catalogue ----------------------------------------------------------
#
# Everything from here to cmd_list() restates resolution rules that
# app/runtime/images.py already implements: env override, then the lock file,
# then the manifest tag. That duplication is deliberate and is the price of the
# no-app-imports rule in the module docstring -- importing app.runtime.images
# would drag in app.config, and through it FastAPI, Celery and SQLAlchemy, into
# a container whose whole purpose is to need none of them. The rules are three
# lines long and both copies are covered by tests/test_images.py.


def load_lock() -> dict[str, dict]:
    """images.lock.json, or {} when it is absent or unusable.

    Silent about a corrupt file on purpose, unlike app/runtime/images.py: this
    is a reporting command, and it prints the VIA column, so a lock file that
    contributed nothing is already visible as `manifest` on every row.
    """
    try:
        data = json.loads(LOCK_PATH.read_text())
    except (OSError, ValueError):
        return {}
    images = data.get("images") if isinstance(data, dict) else None
    return images if isinstance(images, dict) else {}


def env_var(scope: str, key: str) -> str:
    """The DHARINI_IMAGE__<SCOPE>__<KEY> override name for one image."""
    normalise = lambda text: text.upper().replace("-", "_")  # noqa: E731
    return f"DHARINI_IMAGE__{normalise(scope)}__{normalise(key)}"


def resolve(scope: str, key: str, entry: dict, lock: dict[str, dict]) -> tuple[str, str]:
    """The reference actually in force, and which of the three sources set it."""
    override = os.getenv(env_var(scope, key))
    if override:
        return override, "env"
    locked = lock.get(f"{scope}:{key}")
    if locked and locked.get("ref"):
        return str(locked["ref"]), "lock"
    return tagged_ref(entry), "manifest"


# Matches the `img("core")` / `img('shared:snpeff-lsdv')` calls in a pipeline's
# specs.py. Static on purpose: importing the pipeline packages to ask them would
# need FastAPI and Celery, which is exactly what this module may not do.
_IMG_CALL = re.compile(r"""\bimg\(\s*["']([^"']+)["']""")


def references() -> dict[str, set[str]]:
    """Which pipeline scopes name which image, keyed 'scope:key'.

    Read from the source that does the naming rather than from a list, so an
    image stops looking used the moment the last `img()` call for it is deleted.
    """
    found: dict[str, set[str]] = {}
    for path in sorted((REPO_ROOT / "app" / "pipelines").glob("*/manifest.yaml")):
        scope = (yaml.safe_load(path.read_text()) or {}).get("scope")
        if not scope:
            continue
        for source in sorted(path.parent.rglob("*.py")):
            for match in _IMG_CALL.finditer(source.read_text()):
                key = match.group(1)
                name = key if ":" in key else f"{scope}:{key}"
                found.setdefault(name, set()).add(scope)
    return found


def local_refs() -> set[str] | None:
    """Every reference the local daemon already holds, or None if it cannot be asked.

    None and empty are different answers -- "Docker is not running here" must not
    render identically to "you have pulled nothing" -- so the LOCAL column shows
    `?` for the first and `no` for the second.
    """
    try:
        out = capture([
            "docker", "images", "--digests",
            "--format", "{{.Repository}}:{{.Tag}}\t{{.Repository}}@{{.Digest}}",
        ])
    except (OSError, subprocess.CalledProcessError):
        return None

    held: set[str] = set()
    for line in out.splitlines():
        for ref in line.split("\t"):
            ref = ref.strip()
            if ref and "<none>" not in ref:
                held.add(ref)
    return held


def scopes() -> dict[str, Path]:
    """Every declared scope, keyed to its manifest, discovered by walking."""
    found: dict[str, Path] = {}
    for path in manifest_paths():
        scope = (yaml.safe_load(path.read_text()) or {}).get("scope")
        if scope:
            found[scope] = path
    return found


def catalogue(scope: str | None = None, present: set[str] | None = None) -> list[dict]:
    """Every declared image, resolved, with the pipeline scopes that use it."""
    known = scopes()
    if scope is not None and scope not in known:
        listed = ", ".join(sorted(known)) or "(none)"
        sys.exit(f"unknown scope {scope!r}. Known scopes: {listed}")

    lock = load_lock()
    used_by = references()

    rows: list[dict] = []
    for name, (entry, _) in sorted(load_images().items()):
        image_scope, key = name.split(":", 1)
        if scope is not None and image_scope != scope:
            continue
        ref, via = resolve(image_scope, key, entry, lock)
        rows.append({
            "scope": image_scope,
            "key": key,
            # What an author types. A shared image is only ever reached through
            # its qualified name, which is the whole point of showing it.
            "write_as": name if image_scope == SHARED_SCOPE else key,
            "ref": ref,
            "via": via,
            "source": entry.get("source", "?"),
            "local": None if present is None else ref in present,
            "used_by": sorted(used_by.get(name, ())),
        })
    return rows


def _print_catalogue(rows: list[dict], only: str | None) -> None:
    known = scopes()
    order = sorted(known, key=lambda s: (s != SHARED_SCOPE, s))
    if only is not None:
        order = [only]

    headers = ("WRITE IN specs.py", "RESOLVES TO", "KIND", "VIA", "LOCAL", "USED BY")

    def cells(row: dict) -> tuple[str, ...]:
        local = "?" if row["local"] is None else ("yes" if row["local"] else "no")
        return (
            row["write_as"], row["ref"], row["source"], row["via"], local,
            ", ".join(row["used_by"]) or "-- nothing references it",
        )

    body = [cells(row) for row in rows]
    widths = [
        max(len(header), *(len(cell[i]) for cell in body)) if body else len(header)
        for i, header in enumerate(headers)
    ]

    def line(values: tuple[str, ...]) -> str:
        return "  " + "  ".join(v.ljust(w) for v, w in zip(values, widths)).rstrip()

    print("VIA: env = DHARINI_IMAGE__<SCOPE>__<KEY> override, "
          "lock = images.lock.json digest, manifest = the tag in manifest.yaml.")

    for name in order:
        in_scope = [(row, cell) for row, cell in zip(rows, body) if row["scope"] == name]
        print()
        print(f"{name}  ({known[name].relative_to(REPO_ROOT)})")
        if name == SHARED_SCOPE:
            print("  Reusable across pipelines. Reach one from any pipeline by writing its")
            print("  qualified key -- there is never a reason to declare a second copy.")
        print()
        if not in_scope:
            print("  declares no images of its own")
            continue
        print(line(headers))
        for _, cell in in_scope:
            print(line(cell))

    shared = sum(1 for row in rows if row["scope"] == SHARED_SCOPE)
    summary = f"{len(rows)} images in {len(order)} scope{'' if len(order) == 1 else 's'}"
    if only is None:
        summary += f"; {shared} of them shared, and reusable by any pipeline"
    print()
    print(summary + ".")


def cmd_list(args) -> None:
    rows = catalogue(args.scope, present=local_refs())
    if args.json:
        print(json.dumps(rows, indent=2))
        return
    _print_catalogue(rows, args.scope)


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

        # `docker inspect --format '{{index .RepoDigests 0}}'` does not degrade
        # gracefully: on an image with an empty RepoDigests list -- a build that
        # was never pushed, or a tag that was never built at all -- the Go
        # template engine fails to execute the index expression and `docker
        # inspect` exits non-zero. Because `capture()` runs with check=True,
        # that would otherwise surface as an uncaught CalledProcessError instead
        # of the actionable message below, so it is caught here and folded into
        # the same "no digest" branch as a merely-empty result.
        try:
            raw = capture(["docker", "inspect", "--format", "{{index .RepoDigests 0}}", ref])
        except subprocess.CalledProcessError:
            raw = ""
        digest = raw.split("@", 1)[1] if "@" in raw else ""
        if not digest:
            sys.exit(
                f"{name}: {ref} has no repo digest. A built image must be built "
                f"and pushed before it can be locked."
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
        ("list", cmd_list),
    ):
        child = sub.add_parser(name)
        child.set_defaults(handler=handler)
        if name in ("build", "push", "list"):
            child.add_argument("--scope", default=None, help="limit to one manifest scope")
        if name == "list":
            child.add_argument("--json", action="store_true", help="machine-readable output")

    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
