"""The only place in the codebase that starts a process.

Every tool invocation funnels through here, which is what makes the safety
properties hold globally instead of per-pipeline:

  * argv is always a list, never a shell string, so no interpolation of
    user-controlled values into a command line is possible;
  * non-zero exit raises a typed exception instead of calling sys.exit(), so a
    failing tool kills the run, not the worker process;
  * every stage is capped on CPU, memory and wall-clock;
  * bind mounts use identical host and container paths (see config.py).

When a stage genuinely needs a shell -- a pipe between two tools is idiomatic
and unavoidable in this domain -- the *script text must be a literal constant*
and all data must arrive through `env`. See `sh()` below.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import threading
import time
from pathlib import Path

from ..config import (
    DEFAULT_STAGE_TIMEOUT_S,
    DOCKER_BIN,
    HEARTBEAT_INTERVAL_S,
    MAX_STAGE_CPUS,
    MAX_STAGE_MEMORY_GB,
    STATS_SAMPLE_INTERVAL_S,
)
from .spec import CompletedStage, PreflightFailed, StageFailed, StageSpec, StageTimeout

_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_UNSAFE_NAME = re.compile(r"[^a-zA-Z0-9_.-]")

_MEM_UNITS = {
    "b": 1,
    "kb": 10**3, "mb": 10**6, "gb": 10**9, "tb": 10**12,
    "kib": 2**10, "mib": 2**20, "gib": 2**30, "tib": 2**40,
}


def sh(script: str) -> list[str]:
    """Wrap a *constant* shell script for stages that need a pipe.

    The script must not be an f-string and must not embed any runtime value.
    Reference data through quoted environment variables instead::

        ctx.run(spec, sh('minimap2 -ax map-ont -t "$T" "$REF" "$FQ" | samtools sort -o "$BAM"'),
                env={"T": "4", "REF": ref, "FQ": fq, "BAM": bam})

    Quoted expansion of an environment variable is not re-parsed by the shell,
    so a filename containing `;` or `$(...)` is inert.
    """
    return ["bash", "-euo", "pipefail", "-c", script]


def _parse_mem(text: str) -> float | None:
    """Parse a `docker stats` memory figure such as '1.23GiB' into megabytes."""
    m = re.match(r"^\s*([0-9.]+)\s*([A-Za-z]+)\s*$", text)
    if not m:
        return None
    try:
        value = float(m.group(1))
    except ValueError:
        return None
    factor = _MEM_UNITS.get(m.group(2).lower())
    if factor is None:
        return None
    return value * factor / 10**6


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


class _PeakMemorySampler(threading.Thread):
    """Polls `docker stats` and keeps the high-water mark.

    Best-effort telemetry, not accounting: a stage shorter than the sampling
    interval may report nothing. Its purpose is to answer "how much RAM does
    this box actually need" from real runs instead of estimates.
    """

    def __init__(self, container: str, interval: float, on_sample=None):
        super().__init__(daemon=True)
        self.container = container
        self.interval = interval
        self.peak_mb: float | None = None
        self._stop = threading.Event()
        # Liveness piggybacks on this sampler rather than a separate thread
        # (spec S20): it already polls every STATS_SAMPLE_INTERVAL_S, so a
        # heartbeat costs one throttled callback, not a second timer.
        self._on_sample = on_sample
        self._last_beat = 0.0

    def run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                out = subprocess.run(
                    [DOCKER_BIN, "stats", "--no-stream", "--format", "{{.MemUsage}}", self.container],
                    capture_output=True, text=True, timeout=10,
                )
            except (subprocess.SubprocessError, OSError):
                return
            if out.returncode != 0 or not out.stdout.strip():
                return  # container gone
            used = out.stdout.split("/")[0]
            mb = _parse_mem(used)
            if mb is not None and (self.peak_mb is None or mb > self.peak_mb):
                self.peak_mb = mb

            # Throttled well below the memory-sampling interval: this writes
            # to Postgres, and liveness at ten-second resolution is as useful
            # as at two.
            now = time.monotonic()
            if self._on_sample and now - self._last_beat >= HEARTBEAT_INTERVAL_S:
                self._last_beat = now
                try:
                    self._on_sample()
                except Exception:
                    # A heartbeat is telemetry. Losing one must never take
                    # the stage down with it.
                    pass

    def stop(self) -> None:
        self._stop.set()


class StageRunner:
    """Executes StageSpecs. One instance per run."""

    def __init__(
        self, run_id: str, workdir: Path, mounts: list[tuple[Path, bool]], log_dir: Path,
        on_heartbeat=None,
    ):
        """
        Args:
            mounts: (path, read_only) pairs exposed at the identical path
                    inside the container.
            on_heartbeat: called as `on_heartbeat(stage_name)` roughly every
                    HEARTBEAT_INTERVAL_S while a container runs. Optional so
                    tests and tools/run_local.py need not supply one.
        """
        self.run_id = run_id
        self.workdir = workdir
        self.mounts = mounts
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.on_heartbeat = on_heartbeat
        self._counter = 0

    # -- public ------------------------------------------------------------

    def run(
        self,
        stage: str,
        spec: StageSpec,
        argv: list[str],
        env: dict[str, str] | None = None,
        stdout_path: Path | None = None,
    ) -> CompletedStage:
        """Execute one command. Raises StageFailed / StageTimeout."""
        if not isinstance(argv, (list, tuple)) or not all(isinstance(a, str) for a in argv):
            raise TypeError("argv must be a list of strings -- never a shell string")

        env = {k: str(v) for k, v in (env or {}).items()}
        for key in env:
            if not _ENV_KEY.match(key):
                raise ValueError(f"invalid environment variable name: {key!r}")

        self._counter += 1
        log_path = self.log_dir / f"{self._counter:02d}_{_UNSAFE_NAME.sub('-', stage)}.log"

        started = time.monotonic()
        exit_code, peak_mb = self._run_container(stage, spec, argv, env, log_path, stdout_path)
        duration = time.monotonic() - started

        tail = _tail(log_path)
        if exit_code != 0:
            raise StageFailed(stage, exit_code, tail, log_path)

        return CompletedStage(
            name=stage,
            exit_code=exit_code,
            duration_s=duration,
            peak_memory_mb=peak_mb,
            log_path=log_path,
            stdout_tail=tail,
            image=spec.image,
        )

    # -- container execution ----------------------------------------------

    def _run_container(self, stage, spec, argv, env, log_path, stdout_path):
        container = self._container_name(stage)
        cmd = docker_argv(container, self.workdir, self.mounts, spec, argv, env)

        sampler = None
        if STATS_SAMPLE_INTERVAL_S > 0:
            on_sample = (lambda: self.on_heartbeat(stage)) if self.on_heartbeat else None
            sampler = _PeakMemorySampler(container, STATS_SAMPLE_INTERVAL_S, on_sample=on_sample)
            sampler.start()

        try:
            code = self._spawn(cmd, log_path, stdout_path, spec.timeout_s, header=cmd)
        except StageTimeout:
            # The docker CLI exiting does not stop the container; kill it by name.
            subprocess.run([DOCKER_BIN, "kill", container], capture_output=True, check=False)
            raise
        finally:
            if sampler:
                sampler.stop()

        return code, (sampler.peak_mb if sampler else None)

    # -- shared ------------------------------------------------------------

    def _spawn(self, cmd, log_path, stdout_path, timeout_s, header=None):
        """Spawn `cmd` and wait for it.

        Deliberately takes no env or cwd: `cmd` is a `docker run` invocation, so
        those would configure the *docker CLI* on the worker, not the tool. A
        stage's environment reaches the tool through the -e flags docker_argv
        builds; passing it here would instead replace the worker's own
        environment, dropping PATH and DOCKER_HOST, while the container saw
        nothing new.
        """
        timeout_s = timeout_s or DEFAULT_STAGE_TIMEOUT_S

        with open(log_path, "ab") as log:
            if header:
                log.write(f"$ {shlex.join(str(c) for c in header)}\n\n".encode())
                log.flush()

            out_target = open(stdout_path, "wb") if stdout_path else log
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=out_target,
                    stderr=log,
                    close_fds=True,
                )
                try:
                    return proc.wait(timeout=timeout_s)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=30)
                    raise StageTimeout(str(log_path.stem), timeout_s, _tail(log_path))
            except FileNotFoundError as exc:
                raise StageFailed(
                    str(log_path.stem), 127,
                    f"executable not found: {exc}. "
                    f"Is Docker available to the worker (socket mounted)?",
                    log_path,
                ) from exc
            finally:
                if stdout_path:
                    out_target.close()

    def _container_name(self, stage: str) -> str:
        safe = _UNSAFE_NAME.sub("-", stage)[:24]
        return f"dharini-{self.run_id[:8]}-{safe}-{self._counter}"


def _tail(path: Path, limit: int = 4000) -> str:
    """Last `limit` characters of a log, for error reporting."""
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace")
    return text[-limit:] if len(text) > limit else text


# ==========================================================================
# Preflight
# ==========================================================================

def preflight(images: list[str], stage: str = "fetch") -> None:
    """Confirm every image this run needs is already on the host.

    Lives here because this module is the only one permitted to start a
    process -- the same rule that keeps every container invocation auditable in
    one file.

    Without this, a missing image surfaces at stage five, twenty minutes into a
    run, as a Docker error about a container that no longer exists. With it the
    run fails in about a second, naming the image and the command that fixes
    it. The check is cheap because `image inspect` is local metadata: it never
    contacts a registry, so a preflight pass does not mean the image can be
    pulled, only that it is already here -- which is the property that matters
    on a box that pre-pulls.
    """
    missing = []
    for image in images:
        try:
            result = subprocess.run(
                [DOCKER_BIN, "image", "inspect", image],
                capture_output=True, timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise PreflightFailed(
                stage, f"could not query the Docker daemon: {type(exc).__name__}: {exc}"
            ) from exc
        if result.returncode != 0:
            missing.append(image)

    if missing:
        raise PreflightFailed(
            stage,
            f"image(s) not present on this host: {', '.join(sorted(missing))}. "
            f"Run 'make images-pull'.",
        )
