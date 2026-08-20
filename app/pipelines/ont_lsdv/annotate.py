"""Variant annotation with snpEff against the custom LSDV database."""

from __future__ import annotations

from pathlib import Path

from ...config import SNPEFF_DB
from ...runtime.context import RunContext
from . import specs


def run(ctx: RunContext, vcf: Path) -> Path:
    annotated = ctx.work_dir / "annotated.vcf"

    with ctx.stage("annotate"):
        # stdout_path replaces the original's shell redirect. snpEff reads
        # bgzipped VCFs directly, so no decompression step is needed.
        ctx.run(
            specs.SNPEFF,
            [
                "snpEff",
                "-c", str(ctx.param("snpeff.config", "/snpEff/snpEff.config")),
                "-noStats",
                str(ctx.param("snpeff.database", SNPEFF_DB)),
                str(vcf),
            ],
            stdout_path=annotated,
        )
        ctx.require(annotated)

    return ctx.artifact(annotated, kind="vcf", label="Annotated variants")
