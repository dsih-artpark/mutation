"""Optional primer trimming with ARTIC align_trim or BAMClipper."""

from __future__ import annotations

from pathlib import Path

from ...runtime.context import RunContext
from ...runtime.runner import sh
from . import specs
from .common import TRIMMED_BAM, threads

_INPUT_FOR_TOOL = {"align_trim": "primer_bed", "bamclipper": "primer_bedpe"}
"""The extra input each tool reads, matching the `when` on its declaration in
pipeline.py. Never a fallback from one to the other: the formats are not
interchangeable, and neither tool notices when it is handed the wrong one."""


def run(ctx: RunContext, bam: Path, reference: Path) -> Path:
    """Trim primer-binding regions with the selected tool and its primer file.

    Returns the BAM every downstream stage should read, so nothing has to probe
    the filesystem to work out whether trimming happened.
    """
    if not ctx.param("primer.enabled", False):
        ctx.skip("primer", "primer trimming not enabled")
        _flag_untrimmed(ctx)
        return bam

    tool = str(ctx.param("primer.tool", "align_trim")).lower()
    if tool not in _INPUT_FOR_TOOL:
        raise ValueError(f"unsupported primer trimming tool: {tool}")

    key = _INPUT_FOR_TOOL[tool]
    primers = ctx.extra_input(key)
    if not primers or not primers.exists():
        # The API refuses this submission, so only tools/run_local.py, which
        # does not go through it, can get here.
        ctx.skip("primer", f"primer trimming enabled for {tool} but no {key} file was supplied")
        _flag_untrimmed(ctx)
        return bam

    trimmed = ctx.work_dir / TRIMMED_BAM

    with ctx.stage("primer"):
        if tool == "align_trim":
            _validate_primer_scheme(primers, reference)
            ctx.run(
                specs.ALIGN_TRIM,
                sh('align_trim --samfile "$BAM" --report "$REPORT" "$BED" | samtools sort -o "$OUT"'),
                env={
                    "BAM": str(bam),
                    "REPORT": str(ctx.work_dir / "align_trim.report.tsv"),
                    "BED": str(primers),
                    "OUT": str(trimmed),
                },
            )
            ctx.require(trimmed)
            ctx.run(specs.ALIGN_TRIM, ["samtools", "index", str(trimmed)])

        else:
            _validate_primer_pairs(primers, reference)
            ctx.run(
                specs.BAMCLIPPER,
                sh('bamclipper.sh -b "$BAM" -p "$BEDPE" -n "$THREADS" && samtools sort -o "$OUT" "${BAM%.bam}.primerclipped.bam"'),
                env={
                    "BAM": str(bam),
                    "BEDPE": str(primers),
                    "OUT": str(trimmed),
                    "THREADS": str(threads(ctx, specs.BAMCLIPPER.cpus)),
                },
            )
            ctx.require(trimmed)
            ctx.run(specs.BAMCLIPPER, ["samtools", "index", str(trimmed)])

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
    _check_contigs({row.split()[0] for row in rows}, reference, "BED")


_BEDPE_COLUMNS = 6
"""chrom1, start1, end1, chrom2, start2, end2. BAMClipper reads a seventh as
the amplicon name and ignores anything after it."""


def _validate_primer_pairs(bedpe: Path, reference: Path) -> None:
    """Reject primer pairs BAMClipper would misread, before it is handed over.

    BAMClipper 1.0.0 checks only that a row has six tab-separated fields. A
    classic ARTIC BED has six too, so it passes: the primer name is read as the
    right primer's contig and the pool number as its start. No reverse read
    then matches a right primer, and a forward read that matches a primer is
    clipped almost to nothing, because its right primer "starts" at position 2.
    The run exits 0 and records the reads as trimmed.
    """
    rows = [
        line
        for line in bedpe.read_text(errors="replace").splitlines()
        # Exactly what BAMClipper skips. It has no notion of track lines.
        if line.strip() and not line.startswith("#")
    ]
    if not rows:
        raise ValueError(f"primer BEDPE contains no primer pairs: {bedpe.name}")

    for row in rows:
        fields = row.split("\t")

        if len(fields) < _BEDPE_COLUMNS:
            if len(row.split()) >= _BEDPE_COLUMNS:
                raise ValueError(
                    "primer BEDPE columns must be separated by tabs, which BAMClipper "
                    f"splits on: {row.strip()!r}"
                )
            raise ValueError(
                f"primer BEDPE needs {_BEDPE_COLUMNS} columns (chrom, start, end, "
                "chrom, start, end) but this row has "
                f"{len(fields)}: {row.strip()!r}"
            )

        coordinates = [fields[i].strip() for i in (1, 2, 4, 5)]
        if not all(value.isdigit() for value in coordinates):
            if fields[5].strip() in ("+", "-"):
                raise ValueError(
                    "this looks like an ARTIC primer BED (one row per primer, with a "
                    "strand in column 6), not primer pairs (one row per amplicon). "
                    "Select align_trim for a BED, or convert the scheme to BEDPE: "
                    f"{row.strip()!r}"
                )
            raise ValueError(
                "primer BEDPE columns 2, 3, 5 and 6 must be whole-number "
                f"coordinates: {row.strip()!r}"
            )

        left_start, left_end, right_start, right_end = (int(value) for value in coordinates)
        if fields[0] != fields[3]:
            raise ValueError(
                f"the left and right primers name different contigs: {row.strip()!r}"
            )
        if not (left_start < left_end <= right_start < right_end):
            raise ValueError(
                "each primer's start must precede its end, and the left primer must "
                f"end before the right primer starts: {row.strip()!r}"
            )

    # Same silent failure as align_trim: pairs on another genome's contig match
    # no reads, clip nothing, and the run records the reads as trimmed.
    _check_contigs({row.split("\t", 1)[0] for row in rows}, reference, "BEDPE")


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


def _flag_untrimmed(ctx: RunContext) -> None:
    ctx.metric("primer_trimmed", False)
    ctx.metric(
        "primer_warning",
        "No primer trimming applied. Variants within primer-binding regions "
        "should be interpreted with caution.",
    )
