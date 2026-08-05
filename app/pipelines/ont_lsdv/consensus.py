"""Consensus sequence generation: bcftools or Medaka."""

from __future__ import annotations

from pathlib import Path

from ...runtime.context import RunContext
from ...runtime.runner import sh
from . import specs
from .common import active_bam, ensure_indexed_vcf, threads


def run(ctx: RunContext, vcf: Path, reference: Path) -> Path | None:
    tool = str(ctx.param("consensus.tool", "bcftools")).lower()

    with ctx.stage("consensus"):
        if tool == "bcftools":
            consensus = _bcftools(ctx, vcf, reference)
        elif tool == "medaka":
            consensus = _medaka(ctx, reference)
        else:
            raise ValueError(f"unsupported consensus tool: {tool}")

        ctx.require(consensus)
        ctx.metric("consensus_tool", tool)
        _record_consensus_metrics(ctx, consensus)

    return ctx.artifact(consensus, kind="consensus", label="Consensus sequence")


def _bcftools(ctx: RunContext, vcf: Path, reference: Path) -> Path:
    # bcftools consensus requires a bgzip-compressed, indexed VCF. The original
    # handed it a plain .vcf on the Longshot and Medaka paths, so consensus
    # generation failed for three of the four callers.
    indexed = ensure_indexed_vcf(ctx, vcf)
    output = ctx.work_dir / "consensus.fasta"

    ctx.run(
        specs.BCFTOOLS,
        sh('bcftools consensus -f "$REF" "$VCF" > "$OUT"'),
        env={"REF": str(reference), "VCF": str(indexed), "OUT": str(output)},
    )
    return output


def _medaka(ctx: RunContext, reference: Path) -> Path:
    outdir = ctx.work_dir / "medaka_consensus"
    outdir.mkdir(parents=True, exist_ok=True)

    predictions = outdir / "predictions.hdf"
    output = outdir / "consensus.fasta"

    # active_bam() rather than the sorted BAM: the original hard-coded
    # alignment.sorted.bam here, so a primer-clipped run still built its
    # consensus from untrimmed reads.
    bam = active_bam(ctx)

    argv = ["medaka", "inference", str(bam), str(predictions),
            "--threads", str(threads(ctx, specs.MEDAKA.cpus))]
    model = ctx.param("medaka.consensus_model")
    if model:
        argv += ["--model", str(model)]
    ctx.run(specs.MEDAKA, argv)

    ctx.run(specs.MEDAKA, ["medaka", "sequence", str(predictions), str(reference), str(output)])
    return output


def _record_consensus_metrics(ctx: RunContext, consensus: Path) -> None:
    """Ambiguous-base fraction is the headline quality number for a consensus
    genome -- a sequence that is 40% N should not be read as a result."""
    try:
        sequence = "".join(
            line.strip()
            for line in consensus.read_text(errors="replace").splitlines()
            if not line.startswith(">")
        )
    except OSError:
        return

    if not sequence:
        return

    ambiguous = sum(sequence.upper().count(base) for base in ("N", "-"))
    ctx.metric("consensus_length", len(sequence))
    ctx.metric("consensus_ambiguous_bases", ambiguous)
    ctx.metric("consensus_completeness_percent", round((1 - ambiguous / len(sequence)) * 100, 2))
