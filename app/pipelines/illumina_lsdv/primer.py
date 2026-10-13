"""Optional iVar primer trimming."""

from __future__ import annotations

from pathlib import Path

from ...runtime.context import RunContext
from . import specs
from .common import threads


def run(ctx: RunContext, bam: Path, reference: Path) -> Path:
    bed = ctx.extra_input("primer_bed")
    if not ctx.param("primer.enabled", False):
        ctx.skip("primer", "primer trimming is disabled")
        _record_untrimmed(ctx)
        return bam
    if not bed or not bed.is_file():
        ctx.skip("primer", "primer trimming enabled but no primer BED was uploaded")
        _record_untrimmed(ctx)
        return bam

    prefix = ctx.work_dir / "primer_trim"
    raw = ctx.work_dir / "primer_trim.bam"
    sorted_bam = ctx.work_dir / "trimmed.sorted.bam"
    with ctx.stage("primer"):
        _validate_bed(bed, reference)
        ctx.run(specs.PRIMER, [
            "ivar", "trim", "-e", "-i", str(bam), "-b", str(bed), "-p", str(prefix),
            "-m", str(ctx.param("primer.min_length", 30)),
            "-q", str(ctx.param("primer.min_quality", 20)),
            "-s", str(ctx.param("primer.sliding_window", 4)),
        ])
        ctx.require(raw, hint="iVar did not create the trimmed BAM")
        ctx.run(specs.SAMTOOLS, ["samtools", "sort", "-@", str(threads(ctx, specs.SAMTOOLS.cpus)),
                                 "-o", str(sorted_bam), str(raw)])
        ctx.require(sorted_bam)
        ctx.run(specs.SAMTOOLS, ["samtools", "index", str(sorted_bam)])
        ctx.metric("primer_trimmed", True)
    return sorted_bam


def _validate_bed(path: Path, reference: Path) -> None:
    rows = [line for line in path.read_text(errors="replace").splitlines()
            if line.strip() and not line.startswith(("#", "track", "browser"))]
    if not rows:
        raise ValueError("primer BED contains no primer rows")
    for row in rows:
        fields = row.split()
        if len(fields) < 3:
            raise ValueError(f"invalid primer BED row: {row!r}")
        try:
            start, end = int(fields[1]), int(fields[2])
        except ValueError as exc:
            raise ValueError(f"invalid primer BED coordinates: {row!r}") from exc
        if start < 0 or end <= start:
            raise ValueError(f"invalid primer BED interval: {row!r}")

    _check_contigs({row.split()[0] for row in rows}, reference, "BED")


def _check_contigs(scheme: set[str], reference: Path, kind: str) -> None:
    fai = Path(f"{reference}.fai")
    if not fai.exists():
        return

    contigs = {
        line.split("\t", 1)[0]
        for line in fai.read_text(errors="replace").splitlines()
        if line.strip()
    }

    if not scheme & contigs:
        raise ValueError(
            f"primer scheme does not match the reference genome: the {kind} names "
            f"{sorted(scheme)} but the reference contains {sorted(contigs)}. "
            "Trimming would silently match no primers."
        )


def _record_untrimmed(ctx: RunContext) -> None:
    ctx.metric("primer_trimmed", False)
    ctx.metric("primer_warning", "No primer trimming was applied; variants in primer-binding regions may be unreliable.")
