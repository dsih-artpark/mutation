"""LSDV variant annotation with the shared, pinned snpEff image."""

from __future__ import annotations

from pathlib import Path

from ...runtime.context import RunContext
from . import specs


def run(ctx: RunContext, vcf: Path) -> Path:
    output = ctx.work_dir / "annotated.vcf"
    with ctx.stage("annotate"):
        ctx.run(specs.SNPEFF,
                ["snpEff", "-c", "/snpEff/snpEff.config", "-noStats", "LSDV", str(vcf)],
                stdout_path=output)
        ctx.require(output)
    return ctx.artifact(output, kind="vcf", label="Annotated variants")
