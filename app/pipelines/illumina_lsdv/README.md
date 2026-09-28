# Illumina LSDV Variant Calling Pipeline (`illumina-lsdv`)

> A Dharani-native, paired-end Illumina variant analysis pipeline for Lumpy Skin Disease Virus (LSDV).

This package implements a modular, reproducible Illumina variant calling workflow integrated with Dharani's `RunContext` execution engine, image management, progress tracking, and artifact publishing.

---

## 📌 Features

- **Paired-End Read Processing**: Supports forward (`R1`) and reverse (`R2`) Illumina FASTQ reads (`.fastq`, `.fq`, `.fastq.gz`, `.fq.gz`).
- **Flexible Read Quality Control**: Choice of `fastp` (auto adapter trimming + Phred/length filtering), `FastQC`, `Trimmomatic`, or combined `fastqc_trimmomatic`.
- **Reference Alignment**: Alignment with `BWA-MEM` or `Bowtie2`, producing coordinate-sorted and indexed BAM files, `samtools flagstat`, and per-contig coverage reports.
- **Optional Amplicon Primer Trimming**: `iVar trim` support using an optional primer scheme BED file.
- **Viral Haploid Variant Calling**: Call haploid variants using `bcftools` (mpileup + call), `LoFreq`, or `FreeBayes`, filtered by QUAL and read depth (`DP`).
- **Consensus Sequence Generation**: Automated consensus FASTA creation using `bcftools consensus`.
- **Functional Variant Annotation**: `snpEff` annotation against a custom pre-built LSDV genome database (`NC_003027.1`).
- **Rich Interactive Visualization**: Standalone, interactive Plotly web view (`visualization.html`) with gene/CDS tracks, effect category filters, and gene zoom dropdowns.
- **Execution Summary Report**: Automatically generates a structured summary text report (`pipeline_report.txt`).

---

## 📂 Package Architecture

```text
app/pipelines/illumina_lsdv/
├── __init__.py                # Package exports
├── pipeline.py                # IlluminaLsdvPipeline class & JSON Schema parameters
├── specs.py                   # StageSpec CPU, memory & container image definitions
├── manifest.yaml              # Container image specifications
├── common.py                  # Reference preparation & shared utilities
├── qc.py                      # Stage: Read quality control (fastp / FastQC / Trimmomatic)
├── align.py                   # Stage: BWA-MEM / Bowtie2 alignment
├── primer.py                  # Stage: Optional iVar primer trimming
├── variant.py                 # Stage: bcftools / LoFreq / FreeBayes variant calling
├── consensus.py               # Stage: Consensus FASTA generation
├── annotate.py                # Stage: snpEff LSDV annotation
├── visualization.py           # Stage: Plotly HTML variant overview builder
├── report.py                  # Stage: Final run summary report
└── docker/
    └── core/
        └── Dockerfile         # Illumina core container image build file
```

---

## 📥 Inputs & Extra Inputs

| Input | Key | Required? | Accepted Formats | Description |
| --- | --- | --- | --- | --- |
| **Primary Input** | `input_path` | **Yes** | `.fastq.gz`, `.fq.gz`, `.fastq`, `.fq` | Forward read file (`R1`) |
| **Extra Input** | `read2` | **Yes** | `.fastq.gz`, `.fq.gz`, `.fastq`, `.fq` | Reverse paired read file (`R2`) |
| **Extra Input** | `primer_bed` | Optional | `.bed` | Amplicon primer scheme BED file |

---

## ⚙️ Configurable Parameters (JSON Schema)

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `threads` | integer | `4` | Number of CPU threads requested |
| `reference` | string | `"lsdv_reference_sequence.fasta"` | Reference genome FASTA in shared data directory |
| `qc.tool` | enum | `"fastp"` | Options: `"fastp"`, `"fastqc"`, `"trimmomatic"`, `"fastqc_trimmomatic"` |
| `fastp.qualified_quality_phred` | integer | `20` | Minimum Phred quality score (Q20) |
| `fastp.length_required` | integer | `50` | Minimum read length (bp) |
| `fastp.detect_adapter_for_pe` | boolean | `true` | Auto-detect paired-end adapters |
| `alignment.tool` | enum | `"bwa"` | Options: `"bwa"`, `"bowtie2"` |
| `primer.enabled` | boolean | `false` | Enable `iVar` primer trimming (requires `primer_bed`) |
| `primer.min_length` | integer | `30` | Minimum read length after primer trimming |
| `primer.min_quality` | integer | `20` | Minimum Phred quality for sliding window |
| `primer.sliding_window` | integer | `4` | Sliding window width (bp) |
| `variant.tool` | enum | `"bcftools"` | Options: `"bcftools"`, `"lofreq"`, `"freebayes"` |
| `variant.filter` | string | `"QUAL > 20 && INFO/DP > 10"` | `bcftools filter` expression |
| `variant.ploidy` | integer | `1` | Genome ploidy (1 for haploid viral genome) |
| `consensus.enabled` | boolean | `true` | Generate consensus FASTA sequence |
| `visualization.enabled` | boolean | `true` | Generate interactive Plotly HTML report |

---

## 🐳 Container Images

* **Core Stage Container**: `dsihartpark/dharini-illumina-core:1.0.0` (contains `fastp`, `fastqc`, `trimmomatic`, `bwa`, `bowtie2`, `samtools`, `bcftools`, `ivar`, `lofreq`, `freebayes`).
* **Annotation Container**: `dsihartpark/dharini-snpeff:5.4-lsdv-1` (contains `snpEff` with pre-built LSDV NC_003027.1 database).

---

## 📊 Published Output Artifacts

* `fastp.html` / `fastp.json`: QC metrics and interactive report.
* `alignment.flagstat.txt`: `samtools flagstat` mapping statistics.
* `alignment.coverage.txt`: Per-contig coverage breakdown.
* `consensus.fasta`: Filtered consensus genome sequence.
* `annotated.vcf`: Functional variant annotations (`snpEff`).
* `variants.bcftools.vcf.gz`: Compressed and indexed filtered VCF.
* `visualization.html`: Interactive web page with gene tracks, zoom, effect filters, and tooltips.
* `pipeline_report.txt`: Complete execution summary and metrics log.

---

## 🚀 Execution Example (Python API)

```python
from pathlib import Path
from app.config import RUNS_ROOT, REFS_ROOT
from app.runtime.context import RunContext, RunPaths, NullRecorder
from app.pipelines.illumina_lsdv import IlluminaLsdvPipeline

# 1. Create run directory layout
run_id = "sample_631_run"
paths = RunPaths.for_run(RUNS_ROOT, run_id).create()

# 2. Place R1 & R2 FASTQ files inside runs/<run_id>/input/
# input/reads_R1.fastq.gz
# input/reads_R2.fastq.gz

# 3. Instantiate pipeline context
ctx = RunContext(
    run_id=run_id,
    params={
        "threads": 8,
        "reference": "lsdv_reference_sequence.fasta",
        "qc": {"tool": "fastp"},
        "alignment": {"tool": "bwa"},
        "primer": {"enabled": False},
        "variant": {"tool": "bcftools", "filter": "QUAL > 20 && INFO/DP > 10"},
        "consensus": {"enabled": True},
        "visualization": {"enabled": True},
    },
    paths=paths,
    refs_dir=REFS_ROOT,
    recorder=NullRecorder(),
    stages=IlluminaLsdvPipeline().stages,
    input_name="reads_R1.fastq.gz",
    extra_inputs={"read2": "reads_R2.fastq.gz"},
)

# 4. Execute pipeline
result = IlluminaLsdvPipeline().run(ctx)
print("Pipeline Status:", result.status)
```
