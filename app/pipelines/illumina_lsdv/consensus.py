"""Consensus FASTA generation from the caller VCF."""

from __future__ import annotations

from pathlib import Path

from ...runtime.context import RunContext
from ...runtime.runner import sh
from . import specs


def run(ctx: RunContext, vcf: Path, reference: Path, bam: Path | None = None) -> Path:
    output = ctx.work_dir / "consensus.fasta"
    filtered_vcf = ctx.work_dir / "variants.for_consensus.vcf.gz"
    mask_bed = ctx.work_dir / "low_depth_mask.bed"
    tool = str(ctx.param("variant.tool", "bcftools")).lower()
    min_depth = int(ctx.param("consensus.min_depth", 10))

    custom_filter = ctx.param("consensus.filter", "")
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

        consensus_argv = ["bcftools", "consensus", "-f", str(reference)]
        if bam and bam.is_file() and min_depth > 0:
            ctx.run(
                specs.CONSENSUS,
                sh('samtools depth -aa -J -d 0 "$BAM" '
                   '| awk -v min="$MIN_DEPTH" \'$3 < min { print $1 "\t" ($2 - 1) "\t" $2 }\' '
                   '> "$MASK"'),
                env={"BAM": str(bam), "MIN_DEPTH": str(min_depth), "MASK": str(mask_bed)},
            )
            if mask_bed.is_file() and mask_bed.stat().st_size > 0:
                consensus_argv.extend(["-m", str(mask_bed)])
                ctx.metric("consensus_min_depth_mask", min_depth)

        consensus_argv.append(str(filtered_vcf))
        ctx.run(
            specs.CONSENSUS,
            consensus_argv,
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
