"""Alignment: minimap2 -> samtools sort -> samtools index."""

from __future__ import annotations

from pathlib import Path

from ...runtime.context import RunContext
from ...runtime.runner import sh
from . import specs
from .common import SORTED_BAM, threads


def run(ctx: RunContext, fastq: Path, reference: Path) -> Path:
    bam = ctx.work_dir / SORTED_BAM
    n_threads = threads(ctx, specs.ALIGN.cpus)

    with ctx.stage("align"):
        # A pipe avoids materialising an intermediate SAM, which for ONT data is
        # several times the size of the BAM. Constant script, data via env.
        #
        # -m caps samtools sort memory *per thread*. Without it samtools
        # defaults to ~768 MB/thread, so -@ 8 quietly reserves ~6 GB and gets
        # OOM-killed inside a memory-capped container.
        ctx.run(
            specs.ALIGN,
            sh('minimap2 -ax map-ont -t "$T" "$REF" "$FQ" '
               '| samtools sort -@ "$T" -m "$SORTMEM" -o "$BAM"'),
            env={
                "T": str(n_threads),
                "REF": str(reference),
                "FQ": str(fastq),
                "BAM": str(bam),
                "SORTMEM": f"{_sort_memory_mb(n_threads)}M",
            },
        )
        ctx.require(bam)

        ctx.run(specs.SAMTOOLS, ["samtools", "index", str(bam)])

        _record_alignment_metrics(ctx, bam)

    return bam


def _sort_memory_mb(n_threads: int) -> int:
    """Per-thread sort buffer sized to fit inside the stage's memory cap,
    leaving headroom for minimap2 running concurrently in the same container."""
    budget_mb = int(specs.ALIGN.memory_gb * 1024 * 0.5)
    return max(128, budget_mb // max(1, n_threads))


def _record_alignment_metrics(ctx: RunContext, bam) -> None:
    """Mapped-read rate is the single most useful signal for whether a run is
    worth trusting, so it belongs in the run record rather than only in a log."""
    stats = ctx.work_dir / "flagstat.txt"
    try:
        ctx.run(specs.SAMTOOLS, ["samtools", "flagstat", str(bam)], stdout_path=stats)
    except Exception as exc:
        ctx.log(f"[align] flagstat unavailable: {exc}")
        return

    for line in stats.read_text(errors="replace").splitlines():
        if " mapped (" in line and "primary" not in line:
            parts = line.split()
            ctx.metric("reads_mapped", int(parts[0]))
            percent = line.split("(")[1].split("%")[0] if "(" in line else None
            if percent:
                try:
                    ctx.metric("reads_mapped_percent", float(percent))
                except ValueError:
                    pass
            break
        if line.strip().endswith("in total (QC-passed reads + QC-failed reads)"):
            ctx.metric("reads_total", int(line.split()[0]))
