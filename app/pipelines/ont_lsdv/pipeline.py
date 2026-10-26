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
from ..base import ExtraInput, Pipeline
from ..registry import registry
from . import align, annotate, common, consensus, primer, qc, report, variant, visualization


class OntLsdvPipeline(Pipeline):
    id = "ont-lsdv"
    version = "1.2.0"
    display_name = "Oxford Nanopore — LSDV"
    description = (
        "Nanopore FASTQ through QC, minimap2 alignment, optional primer "
        "trimming (ARTIC align_trim / BAMClipper), variant calling (bcftools / "
        "Medaka / Clair3), consensus generation and snpEff annotation."
    )

    accepted_extensions = (".fastq", ".fq", ".fastq.gz", ".fq.gz")
    input_filename = "reads.fastq.gz"

    stages = ["prepare", "qc", "align", "primer", "variant", "consensus", "annotate", "visualise", "report"]

    # One input per trimming tool, because the two read different formats and
    # neither checks what it was given: align_trim needs a BED with one row per
    # primer, BAMClipper a BEDPE with one row per amplicon. Handed an ARTIC BED,
    # BAMClipper 1.0.0 exits 0 having clipped the wrong reads. Each input is in
    # use only for its own tool, so the API rejects the other one.
    extra_inputs = {
        "primer_bed": ExtraInput(
            filename="primers.bed",
            extensions=(".bed",),
            required=True,
            title="ARTIC primer scheme (BED)",
            description=(
                "One row per primer, in the 7-column ARTIC format that includes the "
                "primer sequence. Contig names and coordinates must match the "
                "reference exactly."
            ),
            when={"primer.enabled": True, "primer.tool": "align_trim"},
        ),
        "primer_bedpe": ExtraInput(
            filename="primers.bedpe",
            extensions=(".bedpe",),
            required=True,
            title="Primer pairs (BEDPE)",
            description=(
                "One tab-separated row per amplicon: contig, left primer start and "
                "end, contig, right primer start and end. Contig names and "
                "coordinates must match the reference exactly."
            ),
            when={"primer.enabled": True, "primer.tool": "bamclipper"},
        ),
    }

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
                "description": "How the raw reads are summarised before alignment. "
                               "The report is published as a run artifact either way.",
                "properties": {
                    "tool": {
                        "type": "string", "enum": ["nanoplot", "fastp"], "default": "nanoplot",
                        "title": "QC tool",
                    },
                },
            },

            "filter": {
                "type": "object", "additionalProperties": False,
                "title": "Read filtering",
                "description": "Whether filtering thresholds are derived from the QC "
                               "statistics or taken from the NanoFilt settings below.",
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
                "description": "Manual length and quality thresholds. Only used when "
                               "filter mode is 'manual' and NanoFilt is enabled.",
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
                "title": "fastp",
                "description": "Thresholds used when fastp is the selected QC tool. "
                               "Ignored under NanoPlot.",
                "properties": {
                    "qualified_quality_phred": {
                        "type": "integer", "minimum": 0, "maximum": 50, "default": 15},
                    "length_required": {"type": "integer", "minimum": 0, "default": 500},
                },
            },

            "primer": {
                "type": "object", "additionalProperties": False,
                "title": "Primer trimming",
                "description": "For amplicon runs. Removes primer sequences from the "
                               "aligned reads, using a primer file uploaded alongside "
                               "them.",
                "properties": {
                    "enabled": {
                        "type": "boolean", "default": False,
                        "title": "Trim primers",
                        "description": "Needs the primer file for the selected tool. Without "
                                       "trimming, variants in primer-binding regions are "
                                       "unreliable.",
                    },
                    "tool": {
                        "type": "string",
                        "enum": ["align_trim", "bamclipper"],
                        "default": "align_trim",
                        "title": "Primer trimming tool",
                        "description": "align_trim reads an ARTIC primer scheme (BED, one row "
                                       "per primer). BAMClipper reads primer pairs (BEDPE, one "
                                       "row per amplicon).",
                    },
                },
            },


            "variant": {
                "type": "object", "additionalProperties": False,
                "title": "Variant calling",
                "description": "Which caller produces the VCF. This is the main "
                               "accuracy-versus-runtime decision on the run.",
                "properties": {
                    "tool": {
                        "type": "string",
                        "enum": ["bcftools", "medaka", "clair3"],
                        "default": "clair3",
                        "description": "Clair3 and Medaka run neural-network inference on CPU "
                                       "and are substantially slower than bcftools. Clair3 is "
                                       "the default: it trades runtime for accuracy.",
                    },
                },
            },

            "bcftools": {
                "type": "object", "additionalProperties": False,
                "title": "bcftools",
                "description": "Settings for the bcftools caller. Ignored when another "
                               "variant caller is selected.",
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
                "title": "Medaka",
                "description": "Basecaller-specific Medaka model. The wrong model for "
                               "the flow cell and chemistry degrades calls silently.",
                "properties": {
                    "model": {
                        "type": "string", "default": "r1041_e82_400bps_sup_v5.2.0",
                        "description": "One model is shared by Medaka variant calling and "
                                       "Medaka consensus, which reuses the variant stage's "
                                       "predictions. It must match the basecaller that "
                                       "produced the reads.",
                    },
                    "ignore_read_groups": {
                        "type": "boolean", "default": True,
                        "description": "Process every read group together. ARTIC align_trim "
                                       "assigns one per primer pool, and Medaka refuses to "
                                       "combine read groups unless told to.",
                    },
                },
            },

            "clair3": {
                "type": "object", "additionalProperties": False,
                "title": "Clair3",
                "description": "Clair3 model location and sequencing platform, as they "
                               "exist inside the Clair3 image.",
                "properties": {
                    "model_path": {"type": "string", "default": "/opt/models/ont"},
                    "platform": {"type": "string", "enum": ["ont"], "default": "ont"},
                },
            },

            "consensus": {
                "type": "object", "additionalProperties": False,
                "title": "Consensus",
                "description": "How the consensus genome is built from the alignment "
                               "and the called variants.",
                "properties": {
                    "tool": {
                        "type": "string", "enum": ["bcftools", "medaka"], "default": "bcftools"},
                },
            },

            "snpeff": {
                "type": "object", "additionalProperties": False,
                "title": "snpEff annotation",
                "description": "Which snpEff database annotates the variants. It must "
                               "derive from the same accession as the reference genome.",
                "properties": {
                    "database": {"type": "string", "default": "LSDV"},
                    "config": {"type": "string", "default": "/snpEff/snpEff.config"},
                },
            },

            "visualization": {
                "type": "object", "additionalProperties": False,
                "title": "Variant visualization",
                "description": "Create an interactive HTML view of annotated variants.",
                "properties": {
                    "enabled": {"type": "boolean", "default": True},
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
        bam = primer.run(ctx, bam, reference)

        # Whichever caller ran, this is bgzip-compressed and indexed -- both
        # bcftools consensus and snpEff require that, and normalising it inside
        # the variant stage is what lets these two lines stay caller-agnostic.
        called = variant.run(ctx, bam, reference)

        consensus.run(ctx, bam, called, reference)
        annotated = annotate.run(ctx, called)

        # Publish the caller's raw VCF alongside the annotated one; the
        # annotated file is derived, and reviewers often want the original.
        ctx.artifact(called, kind="vcf", label="Called variants (raw)")

        if ctx.param("keep_alignment", False):
            ctx.artifact(bam, kind="alignment", label="Sorted BAM")

        rows = parse_annotated_vcf(annotated)
        ctx.set_variants(rows)
        for key, value in summarise(rows).items():
            ctx.metric(key, value)

        # Downstream of the annotated rows only; a plotting failure is logged
        # rather than allowed to fail a run whose science already succeeded.
        if ctx.param("visualization.enabled", True):
            chart, error = None, None
            with ctx.stage("visualise"):
                try:
                    chart = visualization.run(ctx, annotated, rows)
                except Exception as exc:
                    error = exc
            if chart:
                ctx.artifact(chart, kind="visualization", label="Variant visualization")
            elif error:
                ctx.log(f"[visualise] visualisation unavailable: {error}")
        else:
            ctx.skip("visualise", "visualization disabled")

        with ctx.stage("report"):
            report.run(ctx)

        return ctx.result


registry.register(OntLsdvPipeline())
