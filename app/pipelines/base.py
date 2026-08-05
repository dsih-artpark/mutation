"""The Pipeline contract.

Three declarations and one method. Everything the platform needs to accept a
run, validate it, show progress, store results and notify Dharini is derived
from these -- which is why adding a pipeline requires no API, no frontend and
no database changes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..runtime.context import RunContext, RunResult


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

    # -- execution ---------------------------------------------------------

    @abstractmethod
    def run(self, ctx: RunContext) -> RunResult:
        """Execute the pipeline.

        Raise on failure -- StageFailed and friends carry the log tail through
        to the run record. Never call sys.exit(): this runs inside a Celery
        worker, and exiting kills the worker rather than the task.
        """

    # -- introspection -----------------------------------------------------

    def describe(self) -> dict:
        return {
            "id": self.id,
            "version": self.version,
            "display_name": self.display_name,
            "description": self.description,
            "accepted_extensions": list(self.accepted_extensions),
            "stages": list(self.stages),
            "params_schema": self.params_schema,
        }
