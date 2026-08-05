"""FASTA assembly -> variant calling (Snippy) -> annotation (snpEff).

The original pipeline, ported onto the Pipeline contract. Its stages run in
the `shared` Snippy and snpEff images -- the same images ont-lsdv uses for
variant calling and annotation, so a variant gets the same annotation
regardless of which pipeline produced it. See `specs.py`.

Porting it first was deliberate: it validates the contract against a pipeline
whose output is already trusted, before the ONT work depends on it.
"""

from __future__ import annotations

import shutil

from ...config import MAX_STAGE_CPUS, MAX_STAGE_MEMORY_GB, REFERENCE, SNPEFF_DB
from ...runtime.context import RunContext, RunResult
from ...vcf import parse_annotated_vcf, summarise
from ..base import Pipeline
from ..registry import registry
from . import specs
from .visualization import generate_visualization


class SnippyFastaPipeline(Pipeline):
    id = "snippy-fasta"
    version = "2.0.0"
    display_name = "FASTA assembly — Snippy + snpEff"
    description = (
        "Calls variants from an assembled FASTA against the LSDV reference "
        "using Snippy, annotates them with snpEff, and renders an interactive "
        "variant plot."
    )

    accepted_extensions = (".fasta", ".fa", ".fna", ".fasta.gz", ".fa.gz")
    input_filename = "assembly.fasta"

    stages = ["snippy", "annotate", "visualise"]

    params_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "cpus": {
                "type": "integer", "minimum": 1, "maximum": 16, "default": 2,
                "title": "CPU cores", "description": "Cores handed to Snippy.",
            },
            "ram_gb": {
                "type": "integer", "minimum": 1, "maximum": 32, "default": 2,
                "title": "RAM (GB)", "description": "Memory ceiling for Snippy.",
            },
            "mincov": {
                "type": "integer", "minimum": 1, "maximum": 100, "default": 10,
                "title": "Minimum coverage",
                "description": "Minimum read depth to call a variant.",
            },
            "minfrac": {
                "type": "number", "minimum": 0, "maximum": 1, "default": 0.0,
                "title": "Minimum allele fraction",
                "description": "Minimum proportion of reads supporting the variant.",
            },
        },
    }

    def run(self, ctx: RunContext) -> RunResult:
        fasta = ctx.input_dir / self.input_filename

        # Clamp to what the stage will actually be given. The runner caps the
        # container at MAX_STAGE_*, so promising snippy more than that gets it
        # OOM-killed at exit 137 with nothing in the log to explain why. The
        # schema maxima stay high on purpose: the ceilings are env-configurable,
        # so raising MAX_STAGE_MEMORY_GB widens this range with no code change.
        # Mirrors common.threads() on the ONT side.
        requested_cpus = int(ctx.param("cpus", 2))
        requested_ram = int(ctx.param("ram_gb", 2))

        cpus = max(1, min(requested_cpus, int(MAX_STAGE_CPUS)))
        # One below the ceiling: the stage requests ram + 1 so snippy keeps
        # headroom inside its own container.
        ram = max(1, min(requested_ram, int(MAX_STAGE_MEMORY_GB) - 1))

        if cpus != requested_cpus:
            ctx.log(f"[clamp] cpus {requested_cpus} -> {cpus} (MAX_STAGE_CPUS={MAX_STAGE_CPUS:g})")
        if ram != requested_ram:
            ctx.log(
                f"[clamp] ram_gb {requested_ram} -> {ram} "
                f"(MAX_STAGE_MEMORY_GB={MAX_STAGE_MEMORY_GB:g}, minus 1GB headroom)"
            )

        # ---------------- Snippy ----------------
        snippy_out = ctx.work_dir / "snippy"
        with ctx.stage("snippy"):
            ctx.run(
                specs.SNIPPY.with_(cpus=cpus, memory_gb=ram + 1),
                [
                    "snippy",
                    "--force",
                    "--ref", str(REFERENCE),
                    "--ctgs", str(fasta),
                    "--outdir", str(snippy_out),
                    "--ram", str(ram),
                    "--cpus", str(cpus),
                    "--mincov", str(int(ctx.param("mincov", 10))),
                    "--minfrac", str(float(ctx.param("minfrac", 0.0))),
                ],
            )
            raw_vcf = ctx.require(snippy_out / "snps.vcf")

        # ---------------- snpEff ----------------
        annotated = ctx.work_dir / "annotated.vcf"
        with ctx.stage("annotate"):
            # stdout_path replaces the shell redirect the original used; no
            # shell means no quoting concerns around the output path.
            ctx.run(
                specs.SNPEFF,
                ["snpEff", "-v", SNPEFF_DB, str(raw_vcf)],
                stdout_path=annotated,
            )
            ctx.require(annotated)

        # snps.vcf is moved out of work/ before the visualiser needs it, so the
        # published copy is the one the consumer downloads.
        snps = ctx.work_dir / "snps.vcf"
        shutil.copy(raw_vcf, snps)
        ctx.artifact(snps, kind="vcf", label="Snippy variants (raw)")
        annotated_published = ctx.artifact(annotated, kind="vcf", label="Annotated variants")

        # ---------------- Visualisation ----------------
        with ctx.stage("visualise"):
            viz = ctx.work_dir / "visualization.html"
            try:
                generate_visualization(ctx.run_id, vcf_file=annotated_published, output_html=viz)
                ctx.artifact(viz, kind="report", label="Interactive variant plot")
            except Exception as exc:
                # A missing plot must not fail a run whose science succeeded.
                ctx.skip("visualise", f"visualisation unavailable: {exc}")

        # ---------------- Results ----------------
        rows = parse_annotated_vcf(annotated_published)
        ctx.set_variants(rows)
        for key, value in summarise(rows).items():
            ctx.metric(key, value)

        return ctx.result


registry.register(SnippyFastaPipeline())
