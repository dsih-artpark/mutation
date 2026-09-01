"""Optional primer trimming with ARTIC align_trim."""

from __future__ import annotations

from pathlib import Path

from ...runtime.context import RunContext
from ...runtime.runner import sh
from . import specs
from .common import TRIMMED_BAM


def run(ctx: RunContext, bam: Path, reference: Path) -> Path:
    """Trim primer-binding regions when an ARTIC BED was supplied.

    Returns the BAM every downstream stage should read, so nothing has to probe
    the filesystem to work out whether trimming happened.

    Skipping is a legitimate outcome, not a failure -- but it changes how the
    variants should be read, so it is recorded on the run rather than only
    printed as a warning the way the original did.
    """
    if not ctx.param("primer.enabled", False):
        ctx.skip("primer", "primer trimming not enabled")
        _flag_untrimmed(ctx)
        return bam

    bed = ctx.extra_input("primer_bed")
    if not bed or not bed.exists():
        ctx.skip("primer", "primer trimming enabled but no primer BED file was uploaded")
        _flag_untrimmed(ctx)
        return bam

    trimmed = ctx.work_dir / TRIMMED_BAM

    with ctx.stage("primer"):
        bed = _ensure_normalized_primer_bed(bed, reference, ctx.work_dir)
        _validate_primer_scheme(bed, reference)

        # align_trim writes SAM to stdout, so it and samtools have to share an
        # image: materialising the intermediate SAM would cost several times
        # the size of the BAM. Constant script, data via env.
        #
        # --normalise is deliberately not passed. align_trim 1.2.0 defaults it
        # to 0, but older ARTIC releases defaulted to 100 -- which silently
        # downsampled every amplicon to 100x coverage before variant calling.
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

        ctx.metric("primer_trimmed", True)

    return trimmed


_BED_COLUMNS = 7
"""align_trim 1.2.0 parses the primer scheme through primalbedtools, which
requires chrom/start/end/name/pool/strand AND the primer sequence. A classic
six-column ARTIC BED raises IndexError from inside a library three frames
down."""


def _reverse_complement(seq: str) -> str:
    trans = str.maketrans("ACGTacgtNn", "TGCAtgcaNn")
    return seq.translate(trans)[::-1]


def _format_primer_name(raw_name: str) -> str:
    clean = raw_name.replace(" ", "_")
    while "__" in clean:
        clean = clean.replace("__", "_")
    clean = clean.strip("_")

    parts = clean.split("_")
    if len(parts) >= 3:
        prefix = parts[0] if parts[0] else "LSDV"
        num = parts[1]
        dir_part = parts[2].upper()
        if dir_part in ("L", "LEFT"):
            return f"{prefix}_{num}_LEFT"
        elif dir_part in ("R", "RIGHT"):
            return f"{prefix}_{num}_RIGHT"

    if clean.endswith("_L"):
        return clean[:-2] + "_LEFT"
    elif clean.endswith("_R"):
        return clean[:-2] + "_RIGHT"
    elif not (clean.endswith("_LEFT") or clean.endswith("_RIGHT")):
        return clean + "_LEFT"
    return clean


def _load_fasta(fasta_path: Path) -> dict[str, str]:
    sequences: dict[str, str] = {}
    curr_header = None
    curr_seq: list[str] = []
    for line in fasta_path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith(">"):
            if curr_header:
                sequences[curr_header] = "".join(curr_seq)
            curr_header = line[1:].split()[0]
            curr_seq = []
        else:
            curr_seq.append(line)
    if curr_header:
        sequences[curr_header] = "".join(curr_seq)
    return sequences


def _ensure_normalized_primer_bed(bed: Path, reference: Path, work_dir: Path) -> Path:
    """Normalize any primer BED file into a valid 7-column ARTIC scheme.

    Auto-fixes spacing, standardizes names (_LEFT / _RIGHT), formats pool & strand,
    and extracts primer nucleotide sequences from the reference genome if missing.
    """
    lines = [
        line.strip()
        for line in bed.read_text(errors="replace").splitlines()
        if line.strip() and not line.startswith(("#", "track", "browser"))
    ]
    if not lines:
        raise ValueError(f"primer BED contains no primer rows: {bed.name}")

    ref_seqs: dict[str, str] | None = None
    new_rows: list[str] = []

    for idx, line in enumerate(lines):
        parts = line.split("\t")
        if len(parts) < 4:
            parts = line.split()

        chrom = parts[0]
        start = int(parts[1])
        end = int(parts[2])
        raw_name = parts[3] if len(parts) > 3 else f"primer_{idx+1}_LEFT"
        pool = parts[4] if len(parts) > 4 else "1"
        try:
            pool_val = str(int(pool))
        except ValueError:
            pool_val = "1"

        strand = parts[5] if len(parts) > 5 else "+"
        if strand not in ("+", "-"):
            strand = "+"

        name = _format_primer_name(raw_name)

        if len(parts) >= 7 and parts[6].strip():
            seq = parts[6].strip()
        else:
            if ref_seqs is None:
                ref_seqs = _load_fasta(reference)
            if chrom not in ref_seqs:
                raise ValueError(
                    f"Contig '{chrom}' in primer BED was not found in reference FASTA. "
                    f"Available contigs: {sorted(ref_seqs.keys())}"
                )
            seq = ref_seqs[chrom][start:end].upper()
            if strand == "-":
                seq = _reverse_complement(seq)

        new_rows.append(f"{chrom}\t{start}\t{end}\t{name}\t{pool_val}\t{strand}\t{seq}")

    out_bed = work_dir / "normalized_primer_scheme.bed"
    out_bed.write_text("\n".join(new_rows) + "\n")
    return out_bed


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

