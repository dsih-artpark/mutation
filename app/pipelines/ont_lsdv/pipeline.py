"""Oxford Nanopore variant analysis for LSDV.

Adapted from the standalone ONTpipeline written by the bioinformatics team. The
tool selection, parameters and stage order are theirs and are preserved; what
changed is the execution contract -- per-run isolation instead of a shared
output/ directory, raised exceptions instead of sys.exit(), argv lists instead
of interpolated shell strings, and declared resources per stage.
"""

from __future__ import annotations

from ...runtime.context import RunContext, RunResult
from ...vcf import parse_annotated_vcf, summarise
from ..base import Pipeline
from ..registry import registry
from . import align, annotate, common, consensus, primer, qc, report, variant


class OntLsdvPipeline(Pipeline):
    id = "ont-lsdv"
    version = "1.0.0"
    display_name = "Oxford Nanopore — LSDV"
    description = (
        "Nanopore FASTQ through QC, minimap2 alignment, optional primer "
        "trimming, variant calling (bcftools / Longshot / Medaka / Clair3), "
        "consensus generation and snpEff annotation."
    )

    accepted_extensions = (".fastq", ".fq", ".fastq.gz", ".fq.gz")
    input_filename = "reads.fastq.gz"

    stages = ["prepare", "qc", "align", "primer", "variant", "consensus", "annotate", "report"]

    params_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "threads": {
                "type": "integer", "minimum": 1, "maximum": 16, "default": 4,
                "title": "Threads",
                "description": "Clamped per stage to that stage's CPU allocation.",
            },
            "reference": {
                "type": "string", "default": "lsdv_reference_sequence.fasta",
                "title": "Reference genome",
                "description": "Filename within the shared references directory.",
            },
            "keep_alignment": {
                "type": "boolean", "default": False,
                "title": "Publish BAM",
                "description": "Retain the sorted BAM for download. Large — off by default.",
            },

            "qc": {
                "type": "object", "additionalProperties": False,
                "title": "Quality control",
                "properties": {
                    "tool": {
                        "type": "string", "enum": ["nanoplot", "fastp"], "default": "nanoplot",
                        "title": "QC tool",
                    },
                },
            },

            "filter": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "mode": {
                        "type": "string", "enum": ["recommended", "manual"],
                        "default": "recommended",
                        "title": "Filter mode",
                        "description": "'recommended' derives thresholds from observed read statistics.",
                    },
                },
            },

            "nanofilt": {
                "type": "object", "additionalProperties": False,
                "title": "NanoFilt",
                "properties": {
                    "enabled": {"type": "boolean", "default": False},
                    "min_quality": {"type": "integer", "minimum": 0, "maximum": 50, "default": 10},
                    "min_length": {"type": "integer", "minimum": 0, "default": 500},
                    "headcrop": {"type": "integer", "minimum": 0, "default": 0},
                    "tailcrop": {"type": "integer", "minimum": 0, "default": 0},
                },
            },

            "fastp": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "qualified_quality_phred": {
                        "type": "integer", "minimum": 0, "maximum": 50, "default": 15},
                    "length_required": {"type": "integer", "minimum": 0, "default": 500},
                },
            },

            "primer": {
                "type": "object", "additionalProperties": False,
                "title": "Primer trimming",
                "properties": {
                    "enabled": {
                        "type": "boolean", "default": False,
                        "description": "Requires a BEDPE upload. Without trimming, variants in "
                                       "primer-binding regions are unreliable.",
                    },
                },
            },

            "variant": {
                "type": "object", "additionalProperties": False,
                "title": "Variant calling",
                "properties": {
                    "tool": {
                        "type": "string",
                        "enum": ["bcftools", "longshot", "medaka", "clair3"],
                        "default": "bcftools",
                        "description": "Medaka and Clair3 run neural-network inference on CPU "
                                       "and are substantially slower.",
                    },
                },
            },

            "bcftools": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "max_depth": {"type": "integer", "minimum": 1, "default": 1000},
                    "ploidy": {
                        "type": "integer", "minimum": 1, "maximum": 2, "default": 1,
                        "description": "1 for viral genomes.",
                    },
                },
            },

            "medaka": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "variant_model": {"type": ["string", "null"], "default": None},
                    "consensus_model": {
                        "type": ["string", "null"], "default": "r1041_e82_400bps_sup_v5.2.0"},
                },
            },

            "clair3": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "model_path": {"type": "string", "default": "/opt/models/ont"},
                    "platform": {"type": "string", "enum": ["ont"], "default": "ont"},
                },
            },

            "consensus": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "tool": {
                        "type": "string", "enum": ["bcftools", "medaka"], "default": "bcftools"},
                },
            },

            "snpeff": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "database": {"type": "string", "default": "LSDV"},
                    "config": {"type": "string", "default": "/snpEff/snpEff.config"},
                },
            },
        },
    }

    def run(self, ctx: RunContext) -> RunResult:
        with ctx.stage("prepare"):
            reference = common.prepare_reference(ctx)
            ctx.require(ctx.input_path, hint="uploaded FASTQ is missing or empty")

        fastq = qc.run(ctx)
        bam = align.run(ctx, fastq, reference)
        bam = primer.run(ctx, bam)

        called = variant.run(ctx, bam, reference, fastq)
        consensus.run(ctx, called, reference)
        annotated = annotate.run(ctx, called)

        # Publish the caller's raw VCF alongside the annotated one; the
        # annotated file is derived, and reviewers often want the original.
        indexed = common.ensure_indexed_vcf(ctx, called)
        ctx.artifact(indexed, kind="vcf", label="Called variants (raw)")

        if ctx.param("keep_alignment", False):
            ctx.artifact(bam, kind="alignment", label="Sorted BAM")

        rows = parse_annotated_vcf(annotated)
        ctx.set_variants(rows)
        for key, value in summarise(rows).items():
            ctx.metric(key, value)

        with ctx.stage("report"):
            report.run(ctx)

        return ctx.result


registry.register(OntLsdvPipeline())
