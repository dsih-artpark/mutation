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
    ):
        child = sub.add_parser(name)
        child.set_defaults(handler=handler)
        if name in ("build", "push"):
            child.add_argument("--scope", default=None, help="limit to one manifest scope")

    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
