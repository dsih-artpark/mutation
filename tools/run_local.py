"""Run one pipeline end-to-end with no API, no database and no queue.

This is how a pipeline author verifies their work before pushing. It builds the
same RunContext the Celery worker builds, applies the same JSON Schema
validation and defaults the API applies, and executes the same stages in the
same tool containers -- so a run that succeeds here fails here for real reasons,
not because the harness differs from production.

What it deliberately does NOT do: talk to Postgres, enqueue anything, deliver a
webhook, or write a Run row. Those are the parts an author is not debugging.

    docker compose run --rm dev \\
        python tools/run_local.py ont-lsdv tests/fixtures/reads.fastq.gz \\
            --param variant.tool=bcftools \\
            --extra primer_bed=scheme.bed

Run it inside the container, not on the host: the tool containers it spawns
mount paths by their absolute host path, and only the compose service is set up
to make those paths agree (see docker-compose.yml).
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import pipelines  # noqa: F401,E402  -- import registers every pipeline
from app.config import REFS_ROOT, RUNS_ROOT  # noqa: E402
from app.pipelines.registry import ParamsInvalid, PipelineNotFound, registry  # noqa: E402
from app.runtime.context import RunContext, RunPaths  # noqa: E402
from app.runtime.spec import StageError  # noqa: E402


class _PrintingRecorder:
    """Stage lifecycle straight to the terminal.

    The worker's recorder writes to Postgres; an author wants to watch stages
    go by. Same Protocol, so the pipeline cannot tell the difference.
    """

    def __init__(self, total: int):
        self._total = total
        self._started: dict[str, float] = {}

    def stage_started(self, name, ordinal):
        self._started[name] = time.monotonic()
        print(f"\n  [{ordinal}/{self._total}] {name} ...", flush=True)

    def stage_finished(self, name, duration_s, peak_memory_mb, image):
        peak = f"{peak_memory_mb:.0f} MB" if peak_memory_mb else "peak unknown"
        print(f"  [ok] {name}  {duration_s:.1f}s  {peak}  {image}", flush=True)

    def stage_failed(self, name, message, exit_code):
        print(f"  [FAIL] {name}  exit={exit_code}  {message}", flush=True)

    def stage_skipped(self, name, reason):
        print(f"  [skip] {name}  {reason}", flush=True)

    def log(self, message):
        print(f"       {message}", flush=True)


def _parse_params(pairs: list[str]) -> dict:
    """Turn `--param variant.tool=bcftools` into `{"variant": {"tool": ...}}`.

    Values go through json.loads first so `--param threads=8` is an integer and
    `--param primer.enabled=true` is a boolean; a schema expecting those types
    would otherwise reject every value the shell can produce.
    """
    params: dict = {}
    for pair in pairs:
        key, _, raw = pair.partition("=")
        if not _:
            raise SystemExit(f"--param expects key=value, got: {pair!r}")
        try:
            value = json.loads(raw)
        except ValueError:
            value = raw

        target = params
        *parents, leaf = key.split(".")
        for part in parents:
            target = target.setdefault(part, {})
            # `--param a=1 --param a.b=2` asks for `a` to be both a scalar and
            # an object. Left alone this is a bare TypeError from dict
            # subscription, which says nothing about which two flags disagree.
            if not isinstance(target, dict):
                raise SystemExit(
                    f"--param {key}={raw} conflicts with an earlier value for "
                    f"{part!r}, which was set to a scalar. Drop one of them."
                )
        if isinstance(target.get(leaf), dict):
            raise SystemExit(
                f"--param {key}={raw} would replace the object already built "
                f"for {key!r} by an earlier --param. Drop one of them."
            )
        target[leaf] = value
    return params


def _stage_run_directory(pipeline, run_id: str, source: Path, extras: list[str]) -> tuple:
    paths = RunPaths.for_run(RUNS_ROOT, run_id).create()

    # Same normalisation the API performs: the file is stored under the
    # pipeline's declared name so a client filename never reaches a command
    # line. Doing it here too is what makes a local run representative.
    shutil.copy(source, paths.input / pipeline.input_filename)

    extra_inputs: dict[str, str] = {}
    for pair in extras:
        key, _, path = pair.partition("=")
        if not _:
            raise SystemExit(f"--extra expects key=path, got: {pair!r}")
        origin = Path(path).expanduser().resolve()
        if not origin.exists():
            raise SystemExit(f"--extra file not found: {origin}")
        shutil.copy(origin, paths.input / origin.name)
        extra_inputs[key] = origin.name

    return paths, extra_inputs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("pipeline", help="pipeline id, e.g. ont-lsdv")
    parser.add_argument("input", type=Path, help="primary input file")
    parser.add_argument("--param", action="append", default=[], metavar="KEY=VALUE",
                        help="dotted parameter override; repeatable")
    parser.add_argument("--extra", action="append", default=[], metavar="KEY=PATH",
                        help="secondary input, e.g. primer_bed=scheme.bed; repeatable")
    parser.add_argument("--run-id", default=None, help="defaults to local-<timestamp>")
    parser.add_argument("--keep", action="store_true",
                        help="keep the work directory; the worker purges it")
    args = parser.parse_args(argv)

    try:
        pipeline = registry.get(args.pipeline)
    except PipelineNotFound:
        known = ", ".join(sorted(p.id for p in registry.all())) or "(none discovered)"
        print(f"unknown pipeline: {args.pipeline}\navailable: {known}", file=sys.stderr)
        return 2

    source = args.input.expanduser().resolve()
    if not source.exists():
        print(f"input not found: {source}", file=sys.stderr)
        return 2

    # The API rejects a mismatched extension before writing anything; so should
    # this, or an author debugs a tool error that is really a wrong-file error.
    if pipeline.accepted_extensions and not any(
        source.name.endswith(ext) for ext in pipeline.accepted_extensions
    ):
        print(
            f"{source.name} does not match {pipeline.id}'s accepted extensions: "
            f"{', '.join(pipeline.accepted_extensions)}",
            file=sys.stderr,
        )
        return 2

    try:
        params = registry.validate_params(args.pipeline, _parse_params(args.param))
    except ParamsInvalid as exc:
        print("invalid parameters:", file=sys.stderr)
        for error in exc.errors:
            print(f"  {error}", file=sys.stderr)
        return 2

    run_id = args.run_id or f"local-{int(time.time())}"
    paths, extra_inputs = _stage_run_directory(pipeline, run_id, source, args.extra)

    print(f"pipeline : {pipeline.id} {pipeline.version}")
    print(f"run id   : {run_id}")
    print(f"run dir  : {paths.root}")
    print(f"refs     : {REFS_ROOT}")
    print(f"params   : {json.dumps(params, sort_keys=True)}")

    ctx = RunContext(
        run_id=run_id,
        params=params,
        paths=paths,
        refs_dir=REFS_ROOT,
        recorder=_PrintingRecorder(len(pipeline.stages)),
        stages=list(pipeline.stages),
        input_name=pipeline.input_filename,
        extra_inputs=extra_inputs,
    )

    started = time.monotonic()
    try:
        result = pipeline.run(ctx)
    except StageError as exc:
        print(f"\nFAILED after {time.monotonic() - started:.1f}s: {exc}", file=sys.stderr)
        print(f"stage logs: {paths.logs}", file=sys.stderr)
        return 1
    except Exception as exc:
        # A pipeline is allowed to raise anything -- rejecting a mismatched
        # primer scheme is a plain ValueError. Print it the way the worker
        # records it rather than dumping a traceback the author has to read
        # past to find the one line that matters.
        print(f"\nFAILED after {time.monotonic() - started:.1f}s", file=sys.stderr)
        print(f"  {type(exc).__name__}: {exc}", file=sys.stderr)
        print(f"stage logs: {paths.logs}", file=sys.stderr)
        return 1

    print(f"\nSUCCEEDED in {time.monotonic() - started:.1f}s")

    if result.metrics:
        print("\nmetrics")
        for key, value in sorted(result.metrics.items()):
            print(f"  {key:34} {value}")

    print(f"\nartifacts ({len(result.artifacts)})")
    for artifact in result.artifacts:
        print(f"  {artifact.kind:12} {artifact.size_bytes:>12,} B  {artifact.path.name}")

    if result.variants:
        print(f"\nvariants: {len(result.variants)}")

    if not args.keep:
        shutil.rmtree(paths.work, ignore_errors=True)
        print(f"\nwork directory purged (--keep to retain it)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
