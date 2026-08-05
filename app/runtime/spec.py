"""Stage declarations and execution results."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..config import DEFAULT_STAGE_TIMEOUT_S


@dataclass(frozen=True)
class StageSpec:
    """How to execute one tool.

    Every stage names a container image. That is how tools with irreconcilable
    dependencies (Medaka vs Clair3 vs Longshot) coexist, and it is what makes
    the resource caps below real: the local execution path applied neither, so
    a pipeline running on the host could claim the whole box.

    cpus/memory_gb are advisory requests; the runner clamps them to the
    MAX_STAGE_* ceiling so a mis-declared spec cannot claim the whole box.
    """

    image: str
    cpus: float = 2.0
    memory_gb: float = 4.0
    timeout_s: int = DEFAULT_STAGE_TIMEOUT_S

    # Override the image ENTRYPOINT. Needed for images that hard-code one
    # (clair3, medaka) when we want to invoke a different binary.
    entrypoint: str | None = None

    # Extra read-only host paths to expose, mounted at the identical path.
    extra_mounts: tuple[Path, ...] = field(default_factory=tuple)

    def with_(self, **overrides) -> "StageSpec":
        """Return a copy with fields replaced -- lets a pipeline reuse one base
        spec and bump resources for the heavy stages."""
        from dataclasses import replace

        return replace(self, **overrides)


@dataclass
class CompletedStage:
    """Outcome of a successful stage execution."""

    name: str
    exit_code: int
    duration_s: float
    peak_memory_mb: float | None
    log_path: Path
    stdout_tail: str
    image: str = ""


class StageError(Exception):
    """Base for stage execution failures."""


class StageFailed(StageError):
    """A tool exited non-zero.

    Carries the log tail so the failure surfaces in the run record and the
    webhook payload, rather than being buried in a container that no longer
    exists.
    """

    def __init__(self, stage: str, exit_code: int, log_tail: str, log_path: Path | None = None):
        self.stage = stage
        self.exit_code = exit_code
        self.log_tail = log_tail
        self.log_path = log_path
        super().__init__(f"stage '{stage}' failed with exit code {exit_code}\n{log_tail}")


class StageTimeout(StageError):
    """A tool exceeded its declared timeout and was killed."""

    def __init__(self, stage: str, timeout_s: int, log_tail: str = ""):
        self.stage = stage
        self.timeout_s = timeout_s
        self.log_tail = log_tail
        super().__init__(f"stage '{stage}' exceeded its {timeout_s}s timeout\n{log_tail}")


class MissingOutput(StageError):
    """A stage exited zero but did not produce the file it promised.

    Tools in this domain sometimes fail softly -- exit 0 with an empty or absent
    output. Asserting on the artifact is the only reliable check.
    """

    def __init__(self, stage: str, path: Path):
        self.stage = stage
        self.path = path
        super().__init__(f"stage '{stage}' completed but did not produce {path}")
