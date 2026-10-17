"""Shared helpers for the Illumina LSDV pipeline stages."""

from __future__ import annotations

import shutil
from pathlib import Path

from ...runtime.context import RunContext
from . import specs


def threads(ctx: RunContext, cap: int) -> int:
    return max(1, min(int(ctx.param("threads", 4)), cap))


def prepare_reference(ctx: RunContext) -> Path:
    name = str(ctx.param("reference", "lsdv_reference_sequence.fasta"))
    refs = Path(ctx.refs_dir).resolve()
    source = (refs / name).resolve()
    if not source.is_relative_to(refs):
        raise ValueError("reference must be a filename inside the shared references directory")
    if not source.is_file():
        raise FileNotFoundError(f"reference not found: {name}")

    local = ctx.work_dir / "reference.fasta"
    shutil.copyfile(source, local)
    ctx.run(specs.SAMTOOLS, ["samtools", "faidx", str(local)])
    return local


def select_bam(ctx: RunContext) -> Path:
    return ctx.work_dir / ("trimmed.sorted.bam" if ctx.param("primer.enabled", False)
                           and ctx.extra_input("primer_bed") else "alignment.sorted.bam")


def normalise_vcf(ctx: RunContext, vcf: Path) -> Path:
    """Return a bgzip-compressed, indexed VCF for downstream consumers."""
    compressed = ctx.work_dir / f"{vcf.name}.gz"
    ctx.run(specs.SAMTOOLS, ["bcftools", "view", "-Oz", "-o", str(compressed), str(vcf)])
    ctx.require(compressed)
    ctx.run(specs.SAMTOOLS, ["bcftools", "index", "-f", "-t", str(compressed)])
    return compressed
