"""Paired-end Illumina variant analysis for LSDV."""

from __future__ import annotations

from ...runtime.context import RunContext, RunResult
from ...vcf import parse_annotated_vcf, summarise
from ..base import ExtraInput, Pipeline
from ..registry import registry
from . import align, annotate, consensus, primer, qc, report, variant, visualization
from .common import prepare_reference


class IlluminaLsdvPipeline(Pipeline):
    id = "illumina-lsdv"
    version = "1.0.0"
    display_name = "Illumina — LSDV"
    description = (
        "Paired-end Illumina FASTQ analysis for LSDV: fastp read QC, BWA "
        "alignment, optional iVar primer trimming, variant calling, consensus, "
        "snpEff annotation and an interactive report."
    )

    accepted_extensions = (".fastq.gz", ".fq.gz")
    input_filename = "reads_R1.fastq.gz"
    stages = ["prepare", "qc", "align", "primer", "variant", "consensus", "annotate", "visualise", "report"]

    extra_inputs = {
        "read2": ExtraInput(
            filename="reads_R2.fastq.gz",
            extensions=(".fastq.gz", ".fq.gz"),
            required=True,
            title="Reverse reads (R2)",
            description="The paired reverse-read FASTQ file (.fastq.gz or .fq.gz) from the same Illumina sample.",
        ),
        "primer_bed": ExtraInput(
            filename="primers.bed",
            extensions=(".bed",),
            required=True,
            title="Primer BED",
            description="iVar primer scheme for amplicon data; contigs must match the LSDV reference. Required when primer trimming is enabled.",
            when={"primer.enabled": True},
        ),
    }

    params_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "threads": {"type": "integer", "minimum": 1, "maximum": 16, "default": 4,
                        "title": "Threads", "description": "Requested CPU threads, capped by each stage allocation."},
            "reference": {"type": "string", "default": "lsdv_reference_sequence.fasta",
                          "title": "Reference genome", "description": "Reference FASTA filename in the shared references directory."},
            "fastp": {"type": "object", "additionalProperties": False, "title": "fastp settings",
                      "description": "Read quality and length thresholds used by fastp.", "properties": {
                          "qualified_quality_phred": {"type": "integer", "minimum": 0, "maximum": 40, "default": 20},
                          "length_required": {"type": "integer", "minimum": 1, "default": 50},
                          "detect_adapter_for_pe": {"type": "boolean", "default": True},
                      }},
            "primer": {"type": "object", "additionalProperties": False, "title": "Primer trimming",
                       "description": "iVar trimming for amplicon libraries; requires the optional primer BED upload.", "properties": {
                           "enabled": {"type": "boolean", "default": False, "title": "Enable primer trimming", "description": "Trim primer-derived read ends before variant calling."},
                           "min_length": {"type": "integer", "minimum": 1, "default": 30},
                           "min_quality": {"type": "integer", "minimum": 0, "maximum": 40, "default": 20},
                           "sliding_window": {"type": "integer", "minimum": 1, "default": 4},
                       }},
            "variant": {"type": "object", "additionalProperties": False, "title": "Variant calling",
                        "description": "Choose a caller for the haploid LSDV genome.", "properties": {
                            "tool": {"type": "string", "enum": ["bcftools", "lofreq", "freebayes"], "default": "bcftools",
                                     "title": "Variant caller", "description": "bcftools is the default; LoFreq and FreeBayes are available alternatives."},
                            "ploidy": {"type": "integer", "minimum": 1, "maximum": 2, "default": 1},
                        }},
            "consensus": {"type": "object", "additionalProperties": False, "title": "Consensus sequence",
                          "description": "Consensus is generated from filtered caller variants with bcftools.", "properties": {
                              "enabled": {"type": "boolean", "default": True},
                              "min_af": {"type": "number", "minimum": 0.0, "maximum": 1.0, "default": 0.5,
                                         "title": "Minimum allele frequency",
                                         "description": "Allele frequency cutoff for majority consensus when using LoFreq."},
                              "min_depth": {"type": "integer", "minimum": 0, "default": 10,
                                            "title": "Minimum depth for consensus masking",
                                            "description": "Positions with coverage below this threshold are masked with N."},
                              "filter": {"type": "string", "default": "", "title": "Custom filter expression",
                                         "description": "Optional BCFtools filter expression override for consensus variants."},
                          }},
            "visualization": {"type": "object", "additionalProperties": False, "title": "Variant visualization",
                              "description": "Create an interactive HTML view of annotated variants.", "properties": {
                                  "enabled": {"type": "boolean", "default": True},
                              }},
        },
    }

    def run(self, ctx: RunContext) -> RunResult:
        read2 = ctx.extra_input("read2")
        if not read2 or not read2.is_file():
            raise FileNotFoundError("paired reverse-read input R2 is missing")
        ctx.require(ctx.input_path, hint="uploaded R1 FASTQ is missing or empty")

        with ctx.stage("prepare"):
            reference = prepare_reference(ctx)

        r1, r2 = qc.run(ctx, ctx.input_path, read2)
        bam = align.run(ctx, r1, r2, reference)
        bam = primer.run(ctx, bam, reference)
        called = variant.run(ctx, bam, reference)

        if ctx.param("consensus.enabled", True):
            consensus.run(ctx, called, reference, bam)
        else:
            ctx.skip("consensus", "consensus generation disabled")

        annotated = annotate.run(ctx, called)
        rows = parse_annotated_vcf(annotated)
        ctx.set_variants(rows)
        for key, value in summarise(rows).items():
            ctx.metric(key, value)

        ctx.artifact(called, kind="vcf", label="Called variants")

        if ctx.param("visualization.enabled", True):
            with ctx.stage("visualise"):
                try:
                    chart = visualization.run(ctx, annotated, rows)
                    ctx.artifact(chart, kind="visualization", label="Variant visualization")
                except Exception as exc:
                    ctx.skip("visualise", f"visualisation unavailable: {exc}")
        else:
            ctx.skip("visualise", "visualization disabled")

        with ctx.stage("report"):
            report.run(ctx)

        return ctx.result


registry.register(IlluminaLsdvPipeline())
