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
        "Paired-end Illumina FASTQ analysis for LSDV: read QC, BWA or Bowtie2 "
        "alignment, optional iVar primer trimming, variant calling, consensus, "
        "snpEff annotation and an interactive report."
    )

    accepted_extensions = (".fastq", ".fq", ".fastq.gz", ".fq.gz")
    input_filename = "reads_R1.fastq.gz"
    stages = ["prepare", "qc", "align", "primer", "variant", "consensus", "annotate", "visualise", "report"]

    extra_inputs = {
        "read2": ExtraInput(
            filename="reads_R2.fastq.gz",
            extensions=(".fastq", ".fq", ".fastq.gz", ".fq.gz"),
            required=True,
            title="Reverse reads (R2)",
            description="The paired reverse-read FASTQ file from the same Illumina sample.",
        ),
        "primer_bed": ExtraInput(
            filename="primers.bed",
            extensions=(".bed",),
            required=False,
            title="Primer BED",
            description="Optional iVar primer scheme for amplicon data; contigs must match the LSDV reference.",
        ),
    }

    params_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "threads": {"type": "integer", "minimum": 1, "maximum": 16, "default": 4,
                        "title": "Threads", "description": "Requested CPU threads, capped by each stage allocation."},
            "reference": {"type": "string", "default": "lsdv_reference.fasta",
                          "title": "Reference genome", "description": "Reference FASTA filename in the shared references directory."},
            "qc": {"type": "object", "additionalProperties": False, "title": "Read quality control",
                   "description": "Choose a paired-read QC and trimming workflow.", "properties": {
                       "tool": {"type": "string", "enum": ["fastp", "fastqc", "trimmomatic", "fastqc_trimmomatic"], "default": "fastp",
                                "title": "QC tool", "description": "fastp trims and reports; FastQC reports only; Trimmomatic trims; the combined option reports before and after trimming."},
                   }},
            "fastp": {"type": "object", "additionalProperties": False, "title": "fastp settings",
                      "description": "Read quality and length thresholds used when fastp is selected.", "properties": {
                          "qualified_quality_phred": {"type": "integer", "minimum": 0, "maximum": 40, "default": 20},
                          "length_required": {"type": "integer", "minimum": 1, "default": 50},
                          "detect_adapter_for_pe": {"type": "boolean", "default": True},
                      }},
            "trimmomatic": {"type": "object", "additionalProperties": False, "title": "Trimmomatic settings",
                            "description": "Sliding-window and minimum-read-length settings for Trimmomatic.", "properties": {
                                "sliding_window_quality": {"type": "integer", "minimum": 1, "maximum": 40, "default": 20},
                                "sliding_window_size": {"type": "integer", "minimum": 1, "maximum": 10, "default": 4},
                                "min_length": {"type": "integer", "minimum": 1, "default": 50},
                            }},
            "alignment": {"type": "object", "additionalProperties": False, "title": "Read alignment",
                          "description": "Choose the paired-end aligner.", "properties": {
                              "tool": {"type": "string", "enum": ["bwa", "bowtie2"], "default": "bwa",
                                       "title": "Aligner", "description": "BWA-MEM is the recommended aligner for Illumina reads; Bowtie2 is also available."},
                          }},
            "primer": {"type": "object", "additionalProperties": False, "title": "Primer trimming",
                       "description": "iVar trimming for amplicon libraries; requires the optional primer BED upload.", "properties": {
                           "enabled": {"type": "boolean", "default": False, "title": "Enable primer trimming", "description": "Trim primer-derived read ends before variant calling."},
                           "min_length": {"type": "integer", "minimum": 1, "default": 30},
                           "min_quality": {"type": "integer", "minimum": 0, "maximum": 40, "default": 20},
                           "sliding_window": {"type": "integer", "minimum": 1, "default": 4},
                       }},
            "variant": {"type": "object", "additionalProperties": False, "title": "Variant calling",
                        "description": "Choose the caller and filters for the haploid LSDV genome.", "properties": {
                            "tool": {"type": "string", "enum": ["bcftools", "lofreq", "freebayes"], "default": "bcftools",
                                     "title": "Variant caller", "description": "bcftools is the default; LoFreq and FreeBayes are available alternatives."},
                            "filter": {"type": "string", "default": "QUAL > 20 && INFO/DP > 10"},
                            "ploidy": {"type": "integer", "minimum": 1, "maximum": 2, "default": 1},
                        }},
            "consensus": {"type": "object", "additionalProperties": False, "title": "Consensus sequence",
                          "description": "Consensus is generated from the filtered variants with bcftools.", "properties": {
                              "enabled": {"type": "boolean", "default": True},
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
        bam = primer.run(ctx, bam)
        called = variant.run(ctx, bam, reference)

        if ctx.param("consensus.enabled", True):
            consensus.run(ctx, called, reference)
        else:
            ctx.skip("consensus", "consensus generation disabled")

        annotated = annotate.run(ctx, called)
        rows = parse_annotated_vcf(annotated)
        ctx.set_variants(rows)
        for key, value in summarise(rows).items():
            ctx.metric(key, value)

        ctx.artifact(called, kind="vcf", label="Filtered variants")

        if ctx.param("visualization.enabled", True):
            with ctx.stage("visualise"):
                chart = visualization.run(ctx, annotated, rows)
                ctx.artifact(chart, kind="visualization", label="Variant visualization")
        else:
            ctx.skip("visualise", "visualization disabled")

        with ctx.stage("report"):
            report.run(ctx)

        return ctx.result


registry.register(IlluminaLsdvPipeline())
