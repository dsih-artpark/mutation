"""The Pipeline contract.

Three declarations and one method. Everything the platform needs to accept a
run, validate it, show progress, store results and notify Dharini is derived
from these -- which is why adding a pipeline requires no API, no frontend and
no database changes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..runtime.context import RunContext, RunResult

# Prepended and appended by the platform to every pipeline's declared stages.
# Reserved: tests/contract.py rejects a pipeline that declares either.
FETCH_STAGE = "fetch"
PUBLISH_STAGE = "publish"


@dataclass(frozen=True)
class ExtraInput:
    """A secondary file a pipeline accepts alongside its primary input.

    Declared rather than hardcoded in the API, which is what removes
    `primer_bed` from the submit endpoint's signature: a pipeline needing a
    different secondary file becomes a backend-only change, and Dharini renders
    the extra file picker from `describe()` exactly as it renders the parameter
    form from `params_schema`.
    """

    filename: str
    """Normalised name on disk. Client filenames never reach a tool."""

    extensions: tuple[str, ...] = ()
    required: bool = False
    title: str = ""
    description: str = ""


class Pipeline(ABC):
    # -- identity ----------------------------------------------------------

    id: str = ""
    """Stable slug used in the API: `POST /v1/runs {"pipeline_id": "ont-lsdv"}`."""

    version: str = "0.1.0"
    """Recorded on every run. Bump it whenever behaviour changes, so a result
    from six months ago can be traced to the code that produced it."""

    display_name: str = ""
    description: str = ""

    # -- input -------------------------------------------------------------

    accepted_extensions: tuple[str, ...] = ()
    """Validated at upload. e.g. ('.fastq', '.fastq.gz', '.fq', '.fq.gz')"""

    input_filename: str = "input.dat"
    """The uploaded file is stored under this fixed name. Client filenames are
    hostile input -- spaces, unicode and shell metacharacters all cause trouble
    downstream -- so the original is kept only as metadata."""

    # -- declarations ------------------------------------------------------

    params_schema: dict = {"type": "object", "properties": {}, "additionalProperties": False}
    """JSON Schema. Serves three purposes at once: the API rejects bad params in
    milliseconds rather than eight minutes into a run; Dharini renders its form
    from it, so a new pipeline needs no frontend work; and defaults live in one
    place instead of being scattered through the stage code."""

    stages: list[str] = []
    """Declared up front so progress reads `4/7 · consensus` from the moment a
    run is queued, rather than a binary PENDING/SUCCESS."""

    extra_inputs: dict[str, ExtraInput] = {}
    """Secondary inputs, keyed by the name used in `extra_input_keys` and in
    `ctx.extra_input(key)`. A plain class-level dict, not a dataclass field
    with a factory: nothing mutates it at runtime, and every subclass either
    leaves the base's shared empty dict alone or overrides the attribute
    entirely, so there is no aliasing hazard to guard against."""

    # -- execution ---------------------------------------------------------

    @abstractmethod
    def run(self, ctx: RunContext) -> RunResult:
        """Execute the pipeline.

        Raise on failure -- StageFailed and friends carry the log tail through
        to the run record. Never call sys.exit(): this runs inside a Celery
        worker, and exiting kills the worker rather than the task.
        """

    # -- introspection -----------------------------------------------------

    @property
    def effective_stages(self) -> tuple[str, ...]:
        """What actually runs, including the platform's own bookends.

        `stages` is what the author declared; this is what a client should see.
        A 90-second fetch and a four-minute publish are otherwise invisible, and
        a run reporting 0/8 for ninety seconds looks hung rather than busy.
        """
        return (FETCH_STAGE, *self.stages, PUBLISH_STAGE)

    def describe(self) -> dict:
        return {
            "id": self.id,
            "version": self.version,
            "display_name": self.display_name,
            "description": self.description,
            "accepted_extensions": list(self.accepted_extensions),
            "stages": list(self.effective_stages),
            "params_schema": self.params_schema,
            "extra_inputs": {
                key: {
                    "filename": extra.filename,
                    "extensions": list(extra.extensions),
                    "required": extra.required,
                    "title": extra.title,
                    "description": extra.description,
                }
                for key, extra in self.extra_inputs.items()
            },
        }
