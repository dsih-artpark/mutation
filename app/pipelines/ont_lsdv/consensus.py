"""Consensus sequence generation: bcftools or Medaka."""

from __future__ import annotations

from pathlib import Path

from ...runtime.context import RunContext
from ...runtime.runner import sh
from . import specs
from .common import medaka_inference_argv


def run(ctx: RunContext, bam: Path, vcf: Path, reference: Path) -> Path | None:
    tool = str(ctx.param("consensus.tool", "bcftools")).lower()

    with ctx.stage("consensus"):
        if tool == "bcftools":
            consensus = _bcftools(ctx, vcf, reference)
        elif tool == "medaka":
            consensus = _medaka(ctx, bam, reference)
        else:
            raise ValueError(f"unsupported consensus tool: {tool}")

        ctx.require(consensus)
        ctx.metric("consensus_tool", tool)
        _record_consensus_metrics(ctx, consensus)

    return ctx.artifact(consensus, kind="consensus", label="Consensus sequence")


def _bcftools(ctx: RunContext, vcf: Path, reference: Path) -> Path:
    # `vcf` arrives bgzip-compressed and indexed: the pipeline normalises the
    # caller's output once, before any stage reads it. bcftools consensus
    # accepts nothing else, and the original handed it a plain .vcf on the
    # Medaka path, so consensus generation only ever worked under bcftools.
    output = ctx.work_dir / "consensus.fasta"

    ctx.run(
        specs.BCFTOOLS,
        sh('bcftools consensus -f "$REF" "$VCF" > "$OUT"'),
        env={"REF": str(reference), "VCF": str(vcf), "OUT": str(output)},
    )
    return output


def _medaka(ctx: RunContext, bam: Path, reference: Path) -> Path:
    # Same directory as the variant path on purpose: inference is the expensive
    # step, and sharing medaka/ lets the consensus reuse its predictions.
    outdir = ctx.work_dir / "medaka"
    outdir.mkdir(parents=True, exist_ok=True)

    predictions = outdir / "predictions.hdf"
    output = outdir / "consensus.fasta"

    # Upstream deletes a stale predictions.hdf before every inference. We have
    # nothing to delete: work_dir is created per run and destroyed with it, so
    # the only file that can be here is the one this run's variant stage wrote.
    # The caller check states that dependency rather than inferring it from a
    # file's mere presence.
    caller = str(ctx.param("variant.tool", "clair3")).lower()
    reused = caller == "medaka" and predictions.exists()
    if reused:
        ctx.log("[consensus] reusing the Medaka predictions from variant calling")
    else:
        ctx.run(specs.MEDAKA, medaka_inference_argv(ctx, bam, predictions))

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
