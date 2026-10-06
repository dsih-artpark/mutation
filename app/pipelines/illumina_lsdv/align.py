"""Paired-end alignment with BWA-MEM."""

from __future__ import annotations

from pathlib import Path

from ...runtime.context import RunContext
from ...runtime.runner import sh
from . import specs
from .common import threads


def run(ctx: RunContext, r1: Path, r2: Path, reference: Path) -> Path:
    bam = ctx.work_dir / "alignment.sorted.bam"
    n_threads = threads(ctx, specs.ALIGN.cpus)

    with ctx.stage("align"):
        prefix = ctx.work_dir / "reference.bwa"
        ctx.run(specs.ALIGN, ["bwa", "index", "-p", str(prefix), str(reference)])
        ctx.run(
            specs.ALIGN,
            sh('bwa mem -t "$T" -R "$RG" "$IDX" "$R1" "$R2" '
               '| samtools sort -@ "$T" -m "$SORTMEM" -o "$BAM"'),
            env={
                "T": str(n_threads), "RG": "@RG\\tID:illumina\\tSM:sample\\tPL:ILLUMINA",
                "IDX": str(prefix), "R1": str(r1), "R2": str(r2), "BAM": str(bam),
                "SORTMEM": f"{max(128, int(specs.ALIGN.memory_gb * 512 / n_threads))}M",
            },
        )

        ctx.require(bam)
        ctx.run(specs.SAMTOOLS, ["samtools", "index", str(bam)])
        ctx.run(specs.SAMTOOLS, ["samtools", "flagstat", str(bam)], stdout_path=ctx.work_dir / "alignment.flagstat.txt")
        ctx.run(specs.SAMTOOLS, ["samtools", "coverage", str(bam)], stdout_path=ctx.work_dir / "alignment.coverage.txt")
        _record_mapped_metrics(ctx, ctx.work_dir / "alignment.flagstat.txt")
        ctx.artifact(ctx.work_dir / "alignment.flagstat.txt", kind="alignment", label="Alignment statistics")
        ctx.artifact(ctx.work_dir / "alignment.coverage.txt", kind="alignment", label="Coverage statistics")

    ctx.metric("aligner", "bwa")
    return bam


def _record_mapped_metrics(ctx: RunContext, stats: Path) -> None:
    for line in stats.read_text(errors="replace").splitlines():
        if " in total (QC-passed reads + QC-failed reads)" in line:
            ctx.metric("reads_total", int(line.split()[0]))
        if " mapped (" in line and "primary" not in line:
            fields = line.split()
            ctx.metric("reads_mapped", int(fields[0]))
            try:
                ctx.metric("reads_mapped_percent", float(line.split("(", 1)[1].split("%", 1)[0]))
            except (IndexError, ValueError):
                pass
            break
