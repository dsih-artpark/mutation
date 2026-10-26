"""bcftools, LoFreq, and FreeBayes variant calling."""

from __future__ import annotations

from pathlib import Path

from ...runtime.context import RunContext
from ...runtime.runner import sh
from . import specs
from .common import normalise_vcf, threads, variant_filter


def run(ctx: RunContext, bam: Path, reference: Path) -> Path:
    tool = str(ctx.param("variant.tool", "bcftools")).lower()
    raw = ctx.work_dir / f"variants.{tool}.raw.vcf"

    with ctx.stage("variant"):
        if tool == "bcftools":
            ctx.run(
                specs.VARIANT,
                sh('bcftools mpileup -d 1000 -f "$REF" -a FORMAT/AD,FORMAT/DP "$BAM" '
                   '| bcftools call -m -v --ploidy "$PLOIDY" -Ov -o "$OUT"'),
                env={"REF": str(reference), "BAM": str(bam), "PLOIDY": str(ctx.param("variant.ploidy", 1)), "OUT": str(raw)},
            )
        elif tool == "lofreq":
            indel_bam = ctx.work_dir / "lofreq.indelqual.bam"
            ctx.run(specs.VARIANT, ["lofreq", "indelqual", "--dindel", "-f", str(reference),
                                    "-o", str(indel_bam), str(bam)])
            ctx.require(indel_bam)
            ctx.run(specs.SAMTOOLS, ["samtools", "index", str(indel_bam)])
            ctx.run(specs.VARIANT, ["lofreq", "call", "--call-indels", "-f", str(reference),
                                    "-o", str(raw), str(indel_bam)])
        elif tool == "freebayes":
            ctx.run(specs.VARIANT, ["freebayes", "-f", str(reference), "-p",
                                    str(ctx.param("variant.ploidy", 1)), str(bam)], stdout_path=raw)
        else:
            raise ValueError(f"unsupported variant caller: {tool}")

        ctx.require(raw, hint=f"{tool} did not produce a VCF")
        compressed = normalise_vcf(ctx, raw)
        ctx.metric("variant_caller", tool)
        marked = _mark(ctx, compressed, ctx.work_dir / f"variants.{tool}.vcf.gz")

    return marked


def _mark(ctx: RunContext, vcf: Path, output: Path) -> Path:
    """Soft-filter: calls failing the caller's rule get FILTER=LowQual, the rest PASS.

    Nothing is removed, so annotation and the report see every call while the
    consensus takes only PASS. bcftools also writes the rule into the VCF header.
    """
    expr, note = variant_filter(ctx)
    ctx.run(specs.VARIANT, ["bcftools", "filter", "-s", "LowQual", "-i", expr,
                            "-Oz", "-o", str(output), str(vcf)])
    ctx.require(output)
    ctx.run(specs.VARIANT, ["bcftools", "index", "-f", "-t", str(output)])
    ctx.metric("variant_filter", expr)
    ctx.metric("variant_filter_note", note)
    return output
