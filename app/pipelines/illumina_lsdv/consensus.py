"""Consensus FASTA generation from the filtered VCF."""

from __future__ import annotations

from pathlib import Path

from ...runtime.context import RunContext
from . import specs


def run(ctx: RunContext, vcf: Path, reference: Path) -> Path:
    output = ctx.work_dir / "consensus.fasta"
    with ctx.stage("consensus"):
        ctx.run(specs.CONSENSUS,
                ["bcftools", "consensus", "-f", str(reference), str(vcf)],
                stdout_path=output)
        ctx.require(output)
        sequence = "".join(line.strip() for line in output.read_text(errors="replace").splitlines()
                           if not line.startswith(">"))
        if sequence:
            ambiguous = sum(sequence.upper().count(base) for base in ("N", "-"))
            ctx.metric("consensus_length", len(sequence))
            ctx.metric("consensus_ambiguous_bases", ambiguous)
            ctx.metric("consensus_completeness_percent", round((1 - ambiguous / len(sequence)) * 100, 2))
    return ctx.artifact(output, kind="consensus", label="Consensus genome")
