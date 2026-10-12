"""Consensus FASTA generation from the caller VCF."""

from __future__ import annotations

from pathlib import Path

from ...runtime.context import RunContext
from . import specs


def run(ctx: RunContext, vcf: Path, reference: Path) -> Path:
    output = ctx.work_dir / "consensus.fasta"
    filtered_vcf = ctx.work_dir / "variants.for_consensus.vcf.gz"
    tool = str(ctx.param("variant.tool", "bcftools")).lower()

    custom_filter = ctx.param("consensus.filter")
    if custom_filter:
        expr = str(custom_filter)
    elif tool == "lofreq":
        min_af = ctx.param("consensus.min_af", 0.5)
        expr = f"INFO/AF>={min_af}"
    elif tool == "freebayes":
        expr = "QUAL>=20"
    else:  # bcftools
        expr = "QUAL>=20 && INFO/DP>=10"

    with ctx.stage("consensus"):
        ctx.run(
            specs.CONSENSUS,
            ["bcftools", "filter", "-i", expr, "-Oz", "-o", str(filtered_vcf), str(vcf)],
        )
        ctx.require(filtered_vcf)
        ctx.run(specs.CONSENSUS, ["bcftools", "index", "-f", str(filtered_vcf)])

        ctx.run(
            specs.CONSENSUS,
            ["bcftools", "consensus", "-f", str(reference), str(filtered_vcf)],
            stdout_path=output,
        )
        ctx.require(output)
        sequence = "".join(line.strip() for line in output.read_text(errors="replace").splitlines()
                           if not line.startswith(">"))
        if sequence:
            ambiguous = sum(sequence.upper().count(base) for base in ("N", "-"))
            ctx.metric("consensus_length", len(sequence))
            ctx.metric("consensus_ambiguous_bases", ambiguous)
            ctx.metric("consensus_completeness_percent", round((1 - ambiguous / len(sequence)) * 100, 2))
            ctx.metric("consensus_filter", expr)
    return ctx.artifact(output, kind="consensus", label="Consensus genome")
