"""Generate small, deterministic inputs for a local end-to-end run.

Real ONT data is gigabytes and cannot live in the repository, but "does this
pipeline work" should not require a download. These inputs are synthesised from
the LSDV reference with substitutions planted at known positions, so the
expected result is known exactly rather than merely plausible: a run that does
not report those variants is wrong, and you can see that without a reference
result to diff against.

    python tools/make_fixtures.py                 # writes fixtures/
    python tools/run_local.py ont-lsdv fixtures/reads.fastq.gz \\
        --param primer.enabled=true --extra primer_bed=fixtures/scheme.bed

Deliberately not a read simulator. The reads are perfect apart from a uniform
substitution rate, which is enough to exercise every stage and every tool
container. Use real data before trusting a variant call.
"""

from __future__ import annotations

import argparse
import gzip
import random
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Fixed so two developers, and two runs on the same machine, get byte-identical
# inputs. A fixture that changes per run cannot be used to compare two commits.
SEED = 20260805

REGION_START = 20_000
REGION_END = 50_000
AMPLICON = 1_000
OVERLAP = 200
PRIMER = 25
READS_PER_AMPLICON = 60
SUBSTITUTION_RATE = 0.02

# Offsets from REGION_START. Interior of the tiled region so every planted
# variant sits under full amplicon coverage.
PLANTED_OFFSETS = (5_000, 12_345, 21_000)

_COMPLEMENT = str.maketrans("ACGT", "TGCA")


def _revcomp(sequence: str) -> str:
    return sequence[::-1].translate(_COMPLEMENT)


def _read_reference(path: Path) -> tuple[str, str]:
    lines = path.read_text().splitlines()
    if not lines or not lines[0].startswith(">"):
        raise SystemExit(f"not a FASTA file: {path}")
    name = lines[0][1:].split()[0]
    sequence = "".join(l.strip() for l in lines[1:] if not l.startswith(">")).upper()
    return name, sequence


def _wrap(sequence: str, width: int = 70) -> str:
    return "\n".join(sequence[i:i + width] for i in range(0, len(sequence), width))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--reference", type=Path,
        default=REPO_ROOT / "data" / "lsdv_reference_sequence.fasta",
    )
    parser.add_argument("--outdir", type=Path, default=REPO_ROOT / "fixtures")
    args = parser.parse_args(argv)

    if not args.reference.exists():
        raise SystemExit(f"reference not found: {args.reference}")

    random.seed(SEED)
    contig, reference = _read_reference(args.reference)
    args.outdir.mkdir(parents=True, exist_ok=True)

    planted: dict[int, tuple[str, str]] = {}
    transition = {"A": "G", "G": "A", "C": "T", "T": "C"}
    for offset in PLANTED_OFFSETS:
        position = REGION_START + offset
        ref_base = reference[position]
        planted[position] = (ref_base, transition[ref_base])

    mutated = list(reference)
    for position, (_, alt) in planted.items():
        mutated[position] = alt
    mutated = "".join(mutated)

    # ---------------------------------------------------------------- reads
    starts = list(range(REGION_START, REGION_END - AMPLICON, AMPLICON - OVERLAP))
    records: list[str] = []
    for index, start in enumerate(starts):
        for number in range(READS_PER_AMPLICON):
            begin = start + random.randint(-15, 15)
            end = begin + AMPLICON + random.randint(-30, 30)
            bases = list(mutated[begin:end])
            for position in range(len(bases)):
                if random.random() < SUBSTITUTION_RATE:
                    bases[position] = random.choice("ACGT")
            read = "".join(bases)
            if random.random() < 0.5:
                read = _revcomp(read)
            quality = "".join(chr(33 + random.randint(18, 40)) for _ in read)
            records.append(f"@amplicon{index}_read{number}\n{read}\n+\n{quality}\n")

    random.shuffle(records)
    fastq = args.outdir / "reads.fastq.gz"
    with gzip.open(fastq, "wt") as handle:
        handle.write("".join(records))

    # --------------------------------------------------------------- scheme
    # Seven columns. align_trim 1.2.0 parses the scheme through primalbedtools,
    # which requires the primer sequence as well -- a classic six-column ARTIC
    # BED raises IndexError from inside the library.
    rows: list[str] = []
    for number, start in enumerate(starts, 1):
        pool = (number % 2) + 1
        left = reference[start:start + PRIMER]
        rows.append(f"{contig}\t{start}\t{start + PRIMER}\tLSDV_{number}_LEFT\t{pool}\t+\t{left}")
        right_end = start + AMPLICON
        right = _revcomp(reference[right_end - PRIMER:right_end])
        rows.append(
            f"{contig}\t{right_end - PRIMER}\t{right_end}\t"
            f"LSDV_{number}_RIGHT\t{pool}\t-\t{right}"
        )
    scheme = args.outdir / "scheme.bed"
    scheme.write_text("\n".join(rows) + "\n")

    # ------------------------------------------------------- query assembly
    # snippy-fasta takes an assembled genome, not reads. Same planted variants,
    # so both pipelines are checked against the same known answer.
    query = args.outdir / "query.fasta"
    query.write_text(f">query_LSDV\n{_wrap(mutated)}\n")

    expected = ", ".join(
        f"{position + 1}{ref}>{alt}" for position, (ref, alt) in sorted(planted.items())
    )
    print(f"reference : {contig} ({len(reference):,} bp)")
    print(f"planted   : {expected}")
    print(f"  (1-based positions; every run of either pipeline must report these {len(planted)})")
    print(f"reads     : {fastq}  {len(records):,} reads, {fastq.stat().st_size:,} B")
    print(f"scheme    : {scheme}  {len(rows)} primers over {len(starts)} amplicons")
    print(f"assembly  : {query}  {query.stat().st_size:,} B")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
