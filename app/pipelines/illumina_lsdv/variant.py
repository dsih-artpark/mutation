"""bcftools, LoFreq, and FreeBayes variant calling."""

from __future__ import annotations

from pathlib import Path

from ...runtime.context import RunContext
from ...runtime.runner import sh
from . import specs
from .common import normalise_vcf, threads


def run(ctx: RunContext, bam: Path, reference: Path) -> Path:
    tool = str(ctx.param("variant.tool", "bcftools")).lower()
    raw = ctx.work_dir / f"variants.{tool}.vcf"

    with ctx.stage("variant"):
        if tool == "bcftools":
            ctx.run(
                specs.VARIANT,
                sh('bcftools mpileup -f "$REF" -a FORMAT/AD,FORMAT/DP "$BAM" '
                   '| bcftools call -m -v --ploidy "$PLOIDY" -Ov -o "$OUT"'),
                env={"REF": str(reference), "BAM": str(bam), "PLOIDY": str(ctx.param("variant.ploidy", 1)), "OUT": str(raw)},
            )
        elif tool == "lofreq":
            ctx.run(specs.VARIANT, ["lofreq", "call", "-f", str(reference), "-o", str(raw), str(bam)])
        elif tool == "freebayes":
            ctx.run(specs.VARIANT, ["freebayes", "-f", str(reference), "-p",
                                    str(ctx.param("variant.ploidy", 1)), str(bam)], stdout_path=raw)
        else:
            raise ValueError(f"unsupported variant caller: {tool}")

        ctx.require(raw, hint=f"{tool} did not produce a VCF")
        compressed = normalise_vcf(ctx, raw)
        ctx.metric("variant_caller", tool)

    return compressed
