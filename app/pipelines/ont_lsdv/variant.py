"""Variant calling: bcftools, Medaka or Clair3."""

from __future__ import annotations

from pathlib import Path

from ...runtime.context import RunContext
from ...runtime.runner import sh
from . import specs
from .common import called_vcf, ensure_indexed_vcf, medaka_inference_argv, threads


def run(ctx: RunContext, bam: Path, reference: Path) -> Path:
    tool = str(ctx.param("variant.tool", "clair3")).lower()

    with ctx.stage("variant"):
        callers = {
            "bcftools": _bcftools,
            "medaka": _medaka,
            "clair3": _clair3,
        }
        if tool not in callers:
            raise ValueError(f"unsupported variant caller: {tool}")

        callers[tool](ctx, bam, reference)

        vcf = called_vcf(ctx)
        ctx.require(vcf, hint=f"{tool} produced no VCF at {vcf}")
        ctx.metric("variant_caller", tool)

        # Normalise inside the stage rather than after it. bcftools consensus
        # and snpEff both require a bgzip-compressed, indexed VCF and what a
        # caller leaves behind varies -- Clair3 bgzips but does not index. Doing
        # it here means a failure is attributed to `variant` instead of to no
        # stage at all, and no downstream stage has to know which caller ran.
        vcf = ensure_indexed_vcf(ctx, vcf)

    return vcf


# ---------------------------------------------------------------- bcftools

def _bcftools(ctx: RunContext, bam: Path, reference: Path) -> None:
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


# ------------------------------------------------------------------ Medaka

def _medaka(ctx: RunContext, bam: Path, reference: Path) -> None:
    """Medaka's three-step API, run over the alignment.

    It replaces the deprecated `medaka_variant` wrapper, which consumed reads
    rather than an alignment -- so primer trimming could not reach it, and the
    caller had to remember to hand it the *filtered* FASTQ or the two stages
    silently saw different input.
    """
    outdir = ctx.work_dir / "medaka"
    outdir.mkdir(parents=True, exist_ok=True)

    predictions = outdir / "predictions.hdf"
    vcf = outdir / "medaka.vcf"

    ctx.run(specs.MEDAKA, medaka_inference_argv(ctx, bam, predictions))
    ctx.run(specs.MEDAKA, ["medaka", "vcf", str(predictions), str(reference), str(vcf)])

    # `medaka vcf` writes plain text, and called_vcf() has to name a file that
    # exists by the time run() asserts on it.
    ensure_indexed_vcf(ctx, vcf)


# ------------------------------------------------------------------ Clair3

def _clair3(ctx: RunContext, bam: Path, reference: Path) -> None:
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
