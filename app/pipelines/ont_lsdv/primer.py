"""Optional primer trimming with BamClipper."""

from __future__ import annotations

from pathlib import Path

from ...runtime.context import RunContext
from . import specs
from .common import CLIPPED_BAM, SORTED_BAM


def run(ctx: RunContext, bam: Path) -> Path:
    """Trim primer-binding regions when a BEDPE was supplied.

    Skipping is a legitimate outcome, not a failure -- but it changes how the
    variants should be read, so it is recorded on the run rather than only
    printed as a warning the way the original did.
    """
    if not ctx.param("primer.enabled", False):
        ctx.skip("primer", "primer trimming not enabled")
        _flag_untrimmed(ctx)
        return bam

    bedpe = ctx.extra_input("primer_bed")
    if not bedpe or not bedpe.exists():
        ctx.skip("primer", "primer trimming enabled but no BEDPE file was uploaded")
        _flag_untrimmed(ctx)
        return bam

    with ctx.stage("primer"):
        ctx.run(
            specs.BAMCLIPPER,
            ["bamclipper.sh", "-b", str(bam), "-p", str(bedpe), "-o", str(ctx.work_dir)],
        )

        clipped = ctx.work_dir / CLIPPED_BAM
        if not clipped.exists():
            # BamClipper names its output after the input BAM.
            produced = ctx.work_dir / f"{Path(SORTED_BAM).stem}.primerclipped.bam"
            if produced.exists():
                produced.rename(clipped)

        ctx.require(clipped)
        ctx.run(specs.BAMCLIPPER.with_(image=specs.CORE_IMAGE),
                ["samtools", "index", str(clipped)])

        ctx.metric("primer_trimmed", True)

    return clipped


def _flag_untrimmed(ctx: RunContext) -> None:
    ctx.metric("primer_trimmed", False)
    ctx.metric(
        "primer_warning",
        "No primer trimming applied. Variants within primer-binding regions "
        "should be interpreted with caution.",
    )
