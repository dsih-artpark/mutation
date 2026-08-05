"""Variant calling: bcftools, Longshot, Medaka or Clair3."""

from __future__ import annotations

from pathlib import Path

from ...runtime.context import RunContext
from ...runtime.runner import sh
from . import specs
from .common import called_vcf, threads


def run(ctx: RunContext, bam: Path, reference: Path, fastq: Path) -> Path:
    tool = str(ctx.param("variant.tool", "bcftools")).lower()

    with ctx.stage("variant"):
        callers = {
            "bcftools": _bcftools,
            "longshot": _longshot,
            "medaka": _medaka,
            "clair3": _clair3,
        }
        if tool not in callers:
            raise ValueError(f"unsupported variant caller: {tool}")

        callers[tool](ctx, bam, reference, fastq)

        vcf = called_vcf(ctx)
        ctx.require(vcf, hint=f"{tool} produced no VCF at {vcf}")
        ctx.metric("variant_caller", tool)

    return vcf


# ---------------------------------------------------------------- bcftools

def _bcftools(ctx: RunContext, bam: Path, reference: Path, _fastq: Path) -> None:
    output = ctx.work_dir / "variants.bcftools.vcf.gz"

    # --ploidy 1: LSDV is a haploid dsDNA virus. The original omitted this, so
    # bcftools applied its diploid default and emitted heterozygous genotypes
    # that cannot exist in a viral genome.
    ctx.run(
        specs.BCFTOOLS,
        sh('bcftools mpileup -Ou -f "$REF" --max-depth "$MAXDP" "$BAM" '
           '| bcftools call -m -v --ploidy "$PLOIDY" -Oz -o "$OUT"'),
        env={
            "REF": str(reference),
            "BAM": str(bam),
            "OUT": str(output),
            "MAXDP": str(int(ctx.param("bcftools.max_depth", 1000))),
            "PLOIDY": str(int(ctx.param("bcftools.ploidy", 1))),
        },
    )
    ctx.run(specs.BCFTOOLS, ["bcftools", "index", "-f", "-t", str(output)])


# ---------------------------------------------------------------- Longshot

def _longshot(ctx: RunContext, bam: Path, reference: Path, _fastq: Path) -> None:
    ctx.run(
        specs.LONGSHOT,
        [
            "longshot",
            "--bam", str(bam),
            "--ref", str(reference),
            "--out", str(ctx.work_dir / "variants.longshot.vcf"),
            "--force_overwrite",
        ],
    )


# ------------------------------------------------------------------ Medaka

def _medaka(ctx: RunContext, _bam: Path, reference: Path, fastq: Path) -> None:
    """Medaka consumes reads rather than an alignment.

    Note it receives the *filtered* FASTQ. The original passed the raw upload
    here while alignment used the filtered reads, so enabling NanoFilt silently
    gave the two stages different input.
    """
    outdir = ctx.work_dir / "medaka"
    argv = [
        "medaka_variant",
        "-i", str(fastq),
        "-r", str(reference),
        "-o", str(outdir),
        "-t", str(threads(ctx, specs.MEDAKA.cpus)),
    ]
    model = ctx.param("medaka.variant_model")
    if model:
        argv += ["-m", str(model)]

    ctx.run(specs.MEDAKA, argv)

    # Medaka's output filename has moved between releases; normalise it.
    outdir.mkdir(parents=True, exist_ok=True)
    target = outdir / "medaka.vcf"
    if not target.exists():
        for candidate in ("medaka.annotated.vcf", "round_1.vcf", "variants.vcf"):
            found = outdir / candidate
            if found.exists():
                found.rename(target)
                break


# ------------------------------------------------------------------ Clair3

def _clair3(ctx: RunContext, bam: Path, reference: Path, _fastq: Path) -> None:
    outdir = ctx.work_dir / "clair3"
    outdir.mkdir(parents=True, exist_ok=True)

    # --include_all_ctgs is essential here. Clair3 defaults to human contig
    # names (chr1-22, X, Y); without this flag it finds no matching contig in a
    # viral reference and exits successfully having called nothing.
    ctx.run(
        # Reset the image ENTRYPOINT so the script receives these arguments
        # directly rather than having them appended to the image's own command.
        specs.CLAIR3.with_(entrypoint=""),
        [
            "/opt/bin/run_clair3.sh",
            f"--bam_fn={bam}",
            f"--ref_fn={reference}",
            f"--model_path={ctx.param('clair3.model_path', '/opt/models/ont')}",
            f"--threads={threads(ctx, specs.CLAIR3.cpus)}",
            f"--platform={ctx.param('clair3.platform', 'ont')}",
            f"--output={outdir}",
            "--include_all_ctgs",
            "--haploid_precise",
        ],
    )
