"""Execution primitives shared by every pipeline."""

from .spec import CompletedStage, StageFailed, StageSpec, StageTimeout
from .context import RunContext, RunPaths, RunResult
from .runner import StageRunner

__all__ = [
    "CompletedStage",
    "RunContext",
    "RunPaths",
    "RunResult",
    "StageFailed",
    "StageRunner",
    "StageSpec",
    "StageTimeout",
]
