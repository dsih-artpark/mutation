"""Optional primer trimming with ARTIC align_trim."""

from __future__ import annotations

from pathlib import Path

from ...runtime.context import RunContext
from ...runtime.runner import sh
from . import specs
from .common import TRIMMED_BAM, threads



def run(ctx: RunContext, bam: Path, reference: Path) -> Path:
    """Trim primer-binding regions when an ARTIC BED or BEDPE was supplied.

    Returns the BAM every downstream stage should read, so nothing has to probe
    the filesystem to work out whether trimming happened.
    """
    if not ctx.param("primer.enabled", False):
        ctx.skip("primer", "primer trimming not enabled")
        _flag_untrimmed(ctx)
        return bam

    tool = str(ctx.param("primer.tool", "align_trim")).lower()

    bed = ctx.extra_input("primer_bedpe") or ctx.extra_input("primer_bed")
    if not bed or not bed.exists():
        ctx.skip("primer", f"primer trimming enabled ({tool}) but no primer BED/BEDPE file was uploaded")
        _flag_untrimmed(ctx)
        return bam

    trimmed = ctx.work_dir / TRIMMED_BAM

    with ctx.stage("primer"):
        if tool == "align_trim":
            _validate_primer_scheme(bed, reference)
            ctx.run(
                specs.ALIGN_TRIM,
                sh('align_trim --samfile "$BAM" --report "$REPORT" "$BED" | samtools sort -o "$OUT"'),
                env={
                    "BAM": str(bam),
                    "REPORT": str(ctx.work_dir / "align_trim.report.tsv"),
                    "BED": str(bed),
                    "OUT": str(trimmed),
                },
            )
            ctx.require(trimmed)
            ctx.run(specs.ALIGN_TRIM, ["samtools", "index", str(trimmed)])

        elif tool == "bamclipper":
            ctx.run(
                specs.BAMCLIPPER,
                sh('bamclipper.sh -b "$BAM" -p "$BED" -n "$THREADS" && samtools sort -o "$OUT" "${BAM%.bam}.primerclipped.bam"'),
                env={
                    "BAM": str(bam),
                    "BED": str(bed),
                    "OUT": str(trimmed),
                    "THREADS": str(threads(ctx, specs.BAMCLIPPER.cpus)),
                },
            )
            ctx.require(trimmed)
            ctx.run(specs.BAMCLIPPER, ["samtools", "index", str(trimmed)])

        else:
            raise ValueError(f"unsupported primer trimming tool: {tool}")

        ctx.metric("primer_trimmed", True)
        ctx.metric("primer_tool", tool)

    return trimmed



_BED_COLUMNS = 7
"""align_trim 1.2.0 parses the primer scheme through primalbedtools, which
requires chrom/start/end/name/pool/strand AND the primer sequence. A classic
six-column ARTIC BED raises IndexError from inside a library three frames
down."""


def _validate_primer_scheme(bed: Path, reference: Path) -> None:
    """Reject a scheme align_trim would misread, before it is handed over.

    Both failures below are ones the operator cannot diagnose from what
    align_trim prints: one is a library stack trace, and the other is no
    message at all.
    """
    rows = [
        line
        for line in bed.read_text(errors="replace").splitlines()
        if line.strip() and not line.startswith(("#", "track", "browser"))
    ]
    if not rows:
        raise ValueError(f"primer BED contains no primer rows: {bed.name}")

    short = next((r for r in rows if len(r.split()) < _BED_COLUMNS), None)
    if short is not None:
        raise ValueError(
            f"primer BED needs {_BED_COLUMNS} columns (chrom, start, end, name, "
            "pool, strand, sequence) but this row has "
            f"{len(short.split())}: {short.strip()!r}"
        )

    # align_trim pairs reads to primers by contig name. Given a scheme for
    # another organism it matches nothing, trims nothing, exits 0, and the run
    # completes with untrimmed reads recorded as trimmed -- the failure reaches
    # the clinician, not the operator. Not hypothetical: the scheme committed
    # alongside the upstream pipeline is ARTIC's SARS-CoV-2 v3 (MN908947.3)
    # while its reference is LSDV (NC_003027.1), and it enables trimming by
    # default.
    fai = Path(f"{reference}.fai")
    if not fai.exists():
        return

    contigs = {
        line.split("\t", 1)[0]
        for line in fai.read_text(errors="replace").splitlines()
        if line.strip()
    }
    scheme = {row.split()[0] for row in rows}

    if not scheme & contigs:
        raise ValueError(
            "primer scheme does not match the reference genome: the BED names "
            f"{sorted(scheme)} but the reference contains {sorted(contigs)}. "
            "Trimming would silently match no primers."
        )


def _flag_untrimmed(ctx: RunContext) -> None:
    ctx.metric("primer_trimmed", False)
    ctx.metric(
        "primer_warning",
        "No primer trimming applied. Variants within primer-binding regions "
        "should be interpreted with caution.",
    )
