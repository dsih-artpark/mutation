"""The surface a pipeline author programs against.

A pipeline receives a RunContext and nothing else. It never imports subprocess,
never shells out, never constructs a relative path, and never calls sys.exit.
Those four prohibitions are enforced by this interface having no way to express
them -- not by a style guide.
"""

from __future__ import annotations

import mimetypes
import shutil
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .runner import StageRunner
from .spec import CompletedStage, MissingOutput, StageError, StageSpec


@dataclass(frozen=True)
class RunPaths:
    """Per-run directory layout. Every path is absolute and identical on the
    host, in the worker, and inside tool containers."""

    root: Path      # runs/<run_id>
    input: Path     # uploads, normalised filenames  (read-only to tools)
    work: Path      # scratch -- deleted when the run ends
    output: Path    # published artifacts -- retained
    logs: Path      # one log file per stage execution

    @classmethod
    def for_run(cls, runs_root: Path, run_id: str) -> "RunPaths":
        """Build the layout for one run.

        `run_id` reaches this method straight off a URL path on the legacy
        endpoints, so it is validated here rather than at each of the seven
        call sites -- one check that cannot be forgotten by the eighth caller.
        `GET /download/..%2Fsnps.vcf` otherwise served a file from outside
        RUNS_ROOT.
        """
        runs_root = Path(runs_root).resolve()
        root = (runs_root / run_id).resolve()
        if not root.is_relative_to(runs_root) or root == runs_root:
            raise ValueError(f"invalid run id: {run_id!r}")

        return cls(
            root=root,
            input=root / "input",
            work=root / "work",
            output=root / "output",
            logs=root / "logs",
        )

    def create(self) -> "RunPaths":
        for path in (self.root, self.input, self.work, self.output, self.logs):
            path.mkdir(parents=True, exist_ok=True)
        return self


# Python's mimetypes table is built for desktop file formats and predates every
# one of these. Left to guess, it calls a Variant Call Format file text/x-vcard
# -- a vCard contact -- which is the content type that then gets stored as
# object metadata and decides what a browser does when someone downloads it.
_GENOMICS_TYPES = {
    ".vcf": "text/plain",
    ".gvcf": "text/plain",
    ".bed": "text/plain",
    ".fasta": "text/plain",
    ".fa": "text/plain",
    ".fastq": "text/plain",
    ".fq": "text/plain",
    ".gff3": "text/plain",
    ".sam": "text/plain",
    ".bam": "application/octet-stream",
    ".bai": "application/octet-stream",
    ".cram": "application/octet-stream",
    ".gz": "application/gzip",
    ".bgz": "application/gzip",
    ".tbi": "application/octet-stream",
    ".html": "text/html",
    ".json": "application/json",
    ".tsv": "text/tab-separated-values",
    ".csv": "text/csv",
}


def _content_type(filename: str) -> str:
    """What this file actually is, for a download header and object metadata.

    Suffix-wise from the right, so `snps.vcf.gz` is gzip rather than plain text.
    """
    lowered = filename.lower()
    for suffix, content_type in _GENOMICS_TYPES.items():
        if lowered.endswith(suffix):
            return content_type
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


@dataclass
class Artifact:
    kind: str
    label: str
    path: Path
    content_type: str = "application/octet-stream"

    @property
    def size_bytes(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0


@dataclass
class RunResult:
    """What a pipeline returns. Artifacts and metrics both flow into the run
    record and the webhook payload."""

    metrics: dict = field(default_factory=dict)
    artifacts: list[Artifact] = field(default_factory=list)
    variants: list[dict] = field(default_factory=list)


class StageRecorder(Protocol):
    """Sink for stage lifecycle events. The Celery task supplies a Postgres
    implementation; tests and CLI runs supply the no-op below."""

    def stage_started(self, name: str, ordinal: int) -> None: ...
    def stage_finished(self, name: str, duration_s: float, peak_memory_mb: float | None,
                       image: str | None) -> None: ...
    def stage_failed(self, name: str, message: str, exit_code: int | None) -> None: ...
    def stage_skipped(self, name: str, reason: str) -> None: ...
    def heartbeat(self, name: str) -> None: ...
    def log(self, message: str) -> None: ...


class NullRecorder:
    def stage_started(self, name, ordinal): pass
    def stage_finished(self, name, duration_s, peak_memory_mb, image): pass
    def stage_failed(self, name, message, exit_code): pass
    def stage_skipped(self, name, reason): pass
    def heartbeat(self, name): pass
    def log(self, message): print(message, flush=True)


class RunContext:
    """Handed to `Pipeline.run()`."""

    def __init__(
        self,
        run_id: str,
        params: dict,
        paths: RunPaths,
        refs_dir: Path,
        recorder: StageRecorder | None = None,
        stages: list[str] | None = None,
        input_name: str | None = None,
        extra_inputs: dict[str, str] | None = None,
    ):
        self.run_id = run_id
        self.params = params
        self.paths = paths
        self.refs_dir = Path(refs_dir).resolve()
        self.recorder = recorder or NullRecorder()
        self.declared_stages = stages or []
        self.input_name = input_name
        self.extra_inputs = extra_inputs or {}

        self._result = RunResult()
        self._current_stage: str | None = None
        self._stage_peak: float | None = None
        self._stage_image: str | None = None
        self.stage_log: list[dict] = []

        self.runner = StageRunner(
            run_id=run_id,
            workdir=paths.work,
            mounts=[(paths.root, False), (self.refs_dir, True)],
            log_dir=paths.logs,
            # getattr, not a hard attribute access: a recorder that predates
            # this feature (a test double duck-typing StageRecorder without
            # subclassing it) should still work, just without a heartbeat.
            on_heartbeat=getattr(self.recorder, "heartbeat", None),
        )

    # -- convenience accessors --------------------------------------------

    @property
    def input_dir(self) -> Path:
        return self.paths.input

    @property
    def input_path(self) -> Path:
        """The primary uploaded file, stored under a normalised name.

        The client's original filename is kept only as run metadata -- it is
        attacker-influenced and never reaches a tool command line.
        """
        if not self.input_name:
            raise FileNotFoundError("no primary input recorded for this run")
        return self.paths.input / self.input_name

    def extra_input(self, key: str) -> Path | None:
        """An optional secondary upload, e.g. an ARTIC primer scheme BED."""
        name = self.extra_inputs.get(key)
        return self.paths.input / name if name else None

    @property
    def work_dir(self) -> Path:
        return self.paths.work

    @property
    def output_dir(self) -> Path:
        return self.paths.output

    def param(self, dotted: str, default=None):
        """Read a nested parameter: `ctx.param("variant.tool", "bcftools")`."""
        node = self.params
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return default if node is None else node

    # -- stage lifecycle ---------------------------------------------------

    @contextmanager
    def stage(self, name: str):
        """Mark a stage. Records timing, peak memory and outcome."""
        ordinal = self._ordinal_of(name)
        self._current_stage = name
        self._stage_peak = None
        self._stage_image = None
        self.recorder.stage_started(name, ordinal)

        started = time.monotonic()
        try:
            yield
        except StageError as exc:
            exit_code = getattr(exc, "exit_code", None)
            self.recorder.stage_failed(name, str(exc), exit_code)
            self.stage_log.append({"name": name, "status": "FAILED", "detail": str(exc)[:200]})
            raise
        except Exception as exc:
            self.recorder.stage_failed(name, f"{type(exc).__name__}: {exc}", None)
            self.stage_log.append({"name": name, "status": "FAILED", "detail": str(exc)[:200]})
            raise
        else:
            duration = time.monotonic() - started
            self.recorder.stage_finished(name, duration, self._stage_peak, self._stage_image)
            self.stage_log.append({
                "name": name,
                "status": "SUCCEEDED",
                "duration_s": round(duration, 2),
                "peak_memory_mb": round(self._stage_peak, 1) if self._stage_peak else None,
            })
        finally:
            self._current_stage = None

    def _ordinal_of(self, name: str) -> int:
        """Position of `name` in the declared stage list.

        Undeclared is an error rather than a -1, because a -1 is invisible:
        `stages` drives the progress denominator a caller polls, so a stage
        added to run() and forgotten in the list produces "4/7" for a pipeline
        that runs eight stages, and reports SUCCEEDED having silently skipped a
        position. The two are written in different places and will drift; this
        is what stops them.
        """
        if not self.declared_stages:
            return -1
        try:
            return self.declared_stages.index(name)
        except ValueError:
            raise ValueError(
                f"stage {name!r} is not in the pipeline's declared stages "
                f"{self.declared_stages}. Add it to `stages`, in the order it runs."
            ) from None

    def skip(self, name: str, reason: str) -> None:
        """Record a stage as deliberately not run. Keeps the progress denominator
        honest instead of leaving a permanent gap."""
        self._ordinal_of(name)  # same drift check as stage(); a typo here is silent otherwise
        self.recorder.stage_skipped(name, reason)
        self.stage_log.append({"name": name, "status": "SKIPPED", "detail": reason})
        self.log(f"[skip] {name}: {reason}")

    # -- execution ---------------------------------------------------------

    def run(
        self,
        spec: StageSpec,
        argv: list[str],
        env: dict[str, str] | None = None,
        stdout_path: Path | None = None,
    ) -> CompletedStage:
        """Execute a tool. Raises StageFailed on non-zero exit."""
        stage = self._current_stage or "unnamed"
        completed = self.runner.run(stage, spec, argv, env=env, stdout_path=stdout_path)

        if completed.peak_memory_mb is not None:
            if self._stage_peak is None or completed.peak_memory_mb > self._stage_peak:
                self._stage_peak = completed.peak_memory_mb
        if completed.image:
            self._stage_image = completed.image
        return completed

    def require(self, path: Path, hint: str = "") -> Path:
        """Assert a tool actually produced its output.

        Tools in this domain fail softly -- exit 0 having written nothing. The
        exit code alone is not a sufficient success check.
        """
        path = Path(path)
        if not path.exists() or path.stat().st_size == 0:
            raise MissingOutput(self._current_stage or "unnamed", path if not hint else Path(hint))
        return path

    # -- outputs -----------------------------------------------------------

    def artifact(self, path: Path, kind: str, label: str) -> Path:
        """Publish a file. Moves it into output/ and registers it for download
        and webhook delivery. Anything not published dies with work/."""
        path = Path(path)
        if not path.exists():
            raise MissingOutput(self._current_stage or "unnamed", path)

        destination = self.paths.output / path.name
        if path.resolve() != destination.resolve():
            self.paths.output.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(destination))

        content_type = _content_type(destination.name)
        self._result.artifacts.append(
            Artifact(kind=kind, label=label, path=destination, content_type=content_type)
        )
        return destination

    def metric(self, key: str, value) -> None:
        self._result.metrics[key] = value

    def set_variants(self, rows: list[dict]) -> None:
        """Parsed variant rows, embedded in the webhook payload so the consumer
        gets queryable data without re-parsing a VCF."""
        self._result.variants = rows

    def log(self, message: str) -> None:
        self.recorder.log(message)

    @property
    def result(self) -> RunResult:
        return self._result
