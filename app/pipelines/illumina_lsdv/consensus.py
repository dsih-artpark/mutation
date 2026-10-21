"""Consensus FASTA generation from the PASS calls of the marked caller VCF."""

from __future__ import annotations

from pathlib import Path

from ...runtime.context import RunContext
from ...runtime.runner import sh
from . import specs


def run(ctx: RunContext, vcf: Path, reference: Path, bam: Path | None = None) -> Path:
    output = ctx.work_dir / "consensus.fasta"
    pass_vcf = ctx.work_dir / "variants.pass.vcf.gz"
    mask_bed = ctx.work_dir / "low_depth_mask.bed"
    min_depth = int(ctx.param("consensus.min_depth", 10))

    with ctx.stage("consensus"):
        # The variant stage already marked each call PASS or LowQual with the
        # caller's rule; the consensus takes only PASS.
        ctx.run(
            specs.CONSENSUS,
            ["bcftools", "view", "-f", "PASS", "-Oz", "-o", str(pass_vcf), str(vcf)],
        )
        ctx.require(pass_vcf)
        ctx.run(specs.CONSENSUS, ["bcftools", "index", "-f", str(pass_vcf)])

        consensus_argv = ["bcftools", "consensus", "-f", str(reference)]
        ctx.metric("consensus_min_depth_mask", min_depth)
        if min_depth <= 0:
            ctx.metric("consensus_mask_note", "masking off (min depth = 0)")
        elif not (bam and bam.is_file()):
            ctx.metric("consensus_mask_note", "masking skipped: no alignment available")
        else:
            ctx.run(
                specs.CONSENSUS,
                sh('samtools depth -aa -J -d 0 "$BAM" '
                   '| awk -v min="$MIN_DEPTH" \'$3 < min { print $1 "\t" ($2 - 1) "\t" $2 }\' '
                   '> "$MASK"'),
                env={"BAM": str(bam), "MIN_DEPTH": str(min_depth), "MASK": str(mask_bed)},
            )
            masked = 0
            if mask_bed.is_file():
                masked = sum(1 for line in mask_bed.read_text(errors="replace").splitlines() if line.strip())
            if masked:
                consensus_argv.extend(["-m", str(mask_bed)])
            ctx.metric("consensus_masked_positions", masked)
            ctx.metric("consensus_mask_note", f"N = depth < {min_depth} reads")

        consensus_argv.append(str(pass_vcf))
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
    return ctx.artifact(output, kind="consensus", label="Consensus genome")
