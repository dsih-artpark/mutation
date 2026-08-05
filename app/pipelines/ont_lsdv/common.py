"""Shared helpers for the ONT stages.

The original implementation copy-pasted the "which VCF did the selected caller
produce" decision into variant.py, consensus.py and annotation.py. Three copies
means three places to update when a caller is added, and they had already begun
to drift. It lives here once.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from ...config import REFS_ROOT
from ...runtime.context import RunContext
from . import specs

SORTED_BAM = "alignment.sorted.bam"
CLIPPED_BAM = "alignment.primerclipped.bam"


def prepare_reference(ctx: RunContext) -> Path:
    """Copy the reference into the run's work directory and index it.

    Two reasons not to use the shared reference in place: refs/ is mounted
    read-only so `samtools faidx` could not write its index there, and Longshot
    and Clair3 both require a .fai alongside the FASTA.
    """
    name = str(ctx.param("reference", "lsdv_reference_sequence.fasta"))

    source = (REFS_ROOT / name).resolve()
    # Reject traversal: the parameter is client-supplied.
    if not str(source).startswith(str(REFS_ROOT.resolve())):
        raise ValueError(f"reference must live inside the references directory: {name}")
    if not source.exists():
        raise FileNotFoundError(f"reference not found: {source}")

    local = ctx.work_dir / "reference.fasta"
    shutil.copy(source, local)

    ctx.run(specs.SAMTOOLS, ["samtools", "faidx", str(local)])
    return local


def active_bam(ctx: RunContext) -> Path:
    """The BAM downstream stages should consume.

    Prefers the primer-clipped BAM when BamClipper ran. Applied uniformly --
    the original used it for the variant callers but not for Medaka consensus,
    which silently reintroduced primer-derived artefacts into the consensus.
    """
    clipped = ctx.work_dir / CLIPPED_BAM
    return clipped if clipped.exists() else ctx.work_dir / SORTED_BAM


def called_vcf(ctx: RunContext) -> Path:
    """The VCF produced by whichever caller was selected."""
    tool = str(ctx.param("variant.tool", "bcftools")).lower()
    work = ctx.work_dir

    paths = {
        "bcftools": work / "variants.bcftools.vcf.gz",
        "longshot": work / "variants.longshot.vcf",
        "medaka": work / "medaka" / "medaka.vcf",
        "clair3": work / "clair3" / "merge_output.vcf.gz",
    }
    if tool not in paths:
        raise ValueError(f"unsupported variant caller: {tool}")
    return paths[tool]


def ensure_indexed_vcf(ctx: RunContext, vcf: Path) -> Path:
    """Return a bgzip-compressed, indexed copy of `vcf`.

    Fixes a real defect in the original: `bcftools consensus` requires a
    compressed and indexed VCF, but the Longshot and Medaka branches passed it a
    plain .vcf. Consensus generation only ever worked on the bcftools path.
    """
    vcf = Path(vcf)
    if not vcf.exists():
        raise FileNotFoundError(f"variant caller produced no VCF at {vcf}")

    if vcf.suffix == ".gz":
        indexed = vcf
    else:
        indexed = vcf.with_suffix(vcf.suffix + ".gz")
        ctx.run(specs.BCFTOOLS, ["bcftools", "view", "-Oz", "-o", str(indexed), str(vcf)])

    ctx.run(specs.BCFTOOLS, ["bcftools", "index", "-f", "-t", str(indexed)])
    return indexed


def threads(ctx: RunContext, cap: int) -> int:
    """Requested thread count, clamped to what the stage's CPU allocation can
    actually deliver. Oversubscribing a capped container makes tools slower."""
    return max(1, min(int(ctx.param("threads", 4)), cap))
