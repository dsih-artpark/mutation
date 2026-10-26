# Illumina LSDV Variant Calling Pipeline (`illumina-lsdv`)

> **A comprehensive, modular, and reproducible paired-end Illumina genomic analysis pipeline for Lumpy Skin Disease Virus (LSDV).**

---

## 📖 Biological & Genomic Context

**Lumpy Skin Disease Virus (LSDV)** is a high-consequence viral pathogen affecting cattle and water buffalo, causing substantial economic loss and trade restrictions.
* **Family & Genus**: *Poxviridae*, genus *Capripoxvirus*.
* **Genome Characteristics**: Large, double-stranded DNA (dsDNA) genome of approximately **151 kbp** encoding ~156 putative genes.
* **Genomic Reference**: RefSeq [`NC_003027.1`](https://www.ncbi.nlm.nih.gov/nuccore/NC_003027.1) (LSDV Neethling warmbaths 1969 strain).
* **Viral Ploidy**: Haploid viral genome (`--ploidy 1`). Because viral isolates exist as homogeneous strains or heterogeneous quasi-species populations, accurate variant analysis requires strict haploid assumptions combined with sensitive frequency estimation.

The `illumina-lsdv` package provides end-to-end processing for paired-end Illumina sequencing data: from raw FASTQ quality control, reference alignment, and amplicon primer clipping to haploid variant calling, low-depth masked consensus generation, `snpEff` functional annotation, and interactive genomic visualization.

---

## 🔄 End-to-End Pipeline Architecture

```text
Raw Paired FASTQ (R1 + R2)
        │
        ▼
   [Stage: qc] ──────────────────► fastp.html & fastp.json
   (fastp Q20/Q30 + Adapter Clip)
        │
        ▼ Clean Reads
   [Stage: align] ───────────────► alignment.flagstat.txt & coverage.txt
   (BWA-MEM + Coordinate Sort)
        │
        ▼ alignment.sorted.bam
   [Stage: primer] (Optional) ───► trimmed.sorted.bam
   (iVar Primer Clipping)          (Validates BED contigs against reference)
        │
        ▼
   [Stage: variant] ─────────────► variants.<caller>.vcf.gz (every call, marked PASS / LowQual)
   (bcftools -d 1000 / lofreq / freebayes
    + caller-specific PASS rule)
        │
        ├─────────────────────────────────────────────────┐
        ▼                                                 ▼
   [Stage: consensus] ────────► consensus.fasta      [Stage: annotate] ──► annotated.vcf
   (PASS calls only +                                (snpEff LSDV database,
    Low-depth mask via -m)                            every call, labels kept)
        │                                                 │
        │                                                 ▼
        │                                            [Stage: visualise] ─► visualization.html
        │                                            (Plotly Interactive Genome Dashboard)
        │                                                 │
        └────────────────────────┬────────────────────────┘
                                 ▼
                           [Stage: report] ──────────────► pipeline_report.txt
```

---

## 🔬 Stage-by-Stage Functional Breakdown

### 1. Preparation & Reference Indexing (`prepare` / `common.py`)
- Resolves the reference FASTA (default `lsdv_reference_sequence.fasta`) inside the shared references directory, rejecting any path that points outside it.
- Copies it into the run's work directory as `reference.fasta` and indexes it with `samtools faidx` (`.fai`).

### 2. Read Quality Control & Preprocessing (`qc` / `qc.py`)
- **Tool**: `fastp` (v1.3.3).
- **Functionality**:
  - Auto-detects and clips standard Illumina TruSeq / Nextera adapters for paired-end reads.
  - Applies a sliding Phred quality cutoff (default: **Q20** / 99% accuracy; user-configurable up to Q30).
  - Discards reads shorter than length threshold (default: **50 bp**).
- **Published Artifacts**: `fastp.html` (interactive quality graphs) and `fastp.json` (machine-readable metrics).
- **Tracked Metrics**: `reads_before_qc`, `reads_after_qc`, `qc_q30_percent`.

### 3. Reference Alignment & Coverage Statistics (`align` / `align.py`)
- **Tools**: `BWA-MEM` + `samtools`.
- **Functionality**:
  - Builds the BWA index (`.bwt`, `.pac`, `.ann`, `.amb`, `.sa`) in the work directory on every run (seconds for the 151 kbp genome).
  - Aligns clean paired reads against the indexed LSDV genome.
  - Adds Illumina read group headers (`@RG\tID:illumina\tSM:sample\tPL:ILLUMINA`).
  - Streams SAM directly into `samtools sort` to produce coordinate-sorted `alignment.sorted.bam`.
  - Indexes BAM (`.bai`) and calculates alignment rates via `samtools flagstat` and per-contig coverage via `samtools coverage`.
- **Tracked Metrics**: `reads_total`, `reads_mapped`, `reads_mapped_percent`, `aligner: "bwa"`.

### 4. Amplicon Primer Scheme Trimming (`primer` / `primer.py`)
- **Tool**: `iVar trim` (v1.4.4) + `samtools`.
- **Why It Matters**: In amplicon-based sequencing (e.g. 2 kbp tiled amplicon schemes), primer binding sites contain synthetic oligonucleotides that artificially introduce mismatches, inflating false-positive variant calls by over 100 variants.
- **Functionality**:
  - Soft-clips primer sequences from 5' and 3' read ends using an uploaded primer BED scheme (e.g. `primers-2kb.bed`). The `-e` flag is always passed so reads that do not overlap any primer are retained rather than silently discarded.
  - **Strict Contig Validation**: Compares BED chromosome names against `reference.fasta.fai`. If contig names mismatch (e.g. SARS-CoV-2 or invalid contig labels), it safely fails inside the `primer` stage with an actionable error rather than silently succeeding.
  - **Submission-Time Validation**: `primer_bed` is a **required** upload whenever `primer.enabled = true`. The API rejects the submission before any run starts if the BED is missing — preventing the silent 155-variant-instead-of-21 outcome.
- **Tracked Metrics**: `primer_trimmed: True` (or `False` with warning if skipped).

### 5. Haploid Viral Variant Calling (`variant` / `variant.py`)
The pipeline provides three caller options tailored to different analytical goals:
* **`bcftools` (Default)**:
  - Generates genotype likelihoods with `bcftools mpileup -d 1000 -a FORMAT/AD,FORMAT/DP`.
  - Calls variants with `bcftools call -m -v --ploidy 1`.
  - The `-d 1000` depth ceiling ensures deep amplicon coverage is not truncated at 250x.
* **`lofreq` (Sub-clonal / Minority Variant Caller)**:
  - Computes indel qualities using `lofreq indelqual --dindel` + `samtools index`.
  - Calls both SNVs and Indels using `lofreq call --call-indels`.
  - Ideal for detecting low-frequency (sub-50%) intra-host viral variants.
* **`freebayes` (Haplotype-Aware Caller)**:
  - Bayesian caller evaluating multi-nucleotide variants and complex indels (`freebayes -p 1`).
- **PASS / LowQual Marking (soft filter)**:
  Every call is kept and labelled with the selected caller's rule using `bcftools filter -s LowQual -i <rule>`:

  | Caller | PASS rule | Note recorded |
  | :--- | :--- | :--- |
  | `bcftools` | `QUAL>=20 && INFO/DP>=10` | PASS = QUAL >= 20 and depth >= 10; only PASS used in consensus |
  | `lofreq` | `INFO/AF>=<consensus.min_af>` (default 0.5) | PASS = allele frequency >= 0.5; only PASS used in consensus |
  | `freebayes` | `QUAL>=20` | PASS = QUAL >= 20; only PASS used in consensus |

  - Calls meeting the rule get `FILTER=PASS`; the rest get `FILTER=LowQual`. Nothing is removed.
  - A call the caller already flagged itself (e.g. LoFreq `sb_fdr`) keeps that flag, so it is reported but not used in the consensus.
  - bcftools writes the rule into the VCF header: `##FILTER=<ID=LowQual,Description="Set if not true: QUAL>=20 && INFO/DP>=10">`.
  - The rules are fixed per caller; there is no free-text filter parameter.
- **Output**: Caller VCF is bgzip-compressed, marked, and tabix-indexed as `variants.<caller>.vcf.gz`.
- **Tracked Metrics**: `variant_caller`, `variant_filter` (the rule), `variant_filter_note` (short note above).

### 6. Consensus Sequence Generation with Low-Depth Masking (`consensus` / `consensus.py`)
- **PASS Calls Only**:
  `bcftools view -f PASS` takes only the calls marked PASS in the variant stage, so non-dominant sub-clones or noise do not corrupt the consensus. The same marked VCF goes to annotation, so the consensus and the reported variants always agree on which calls passed.
- **Low-Depth Masking (`-m`)**:
  - Scans read depth using `samtools depth -aa -J -d 0`.
    - `-aa`: emits a depth row for **every** reference position, including positions with zero depth and positions not covered by any read. Without this, a sample with no mapped reads produces an empty depth file, no mask is applied, and the reference sequence is returned as a fake 100%-complete consensus.
    - `-J`: counts reads that span a deletion gap toward depth at that position. Without this, bases covered only by deletion-spanning reads score depth 0, enter the low-depth mask, and `bcftools consensus` skips the deletion variant — replacing a real indel with `N`. LSDV field strains carry genuine deletions against the reference, so this matters.
  - Positions with depth below `consensus.min_depth` (default: **10x**) are collected into `low_depth_mask.bed`.
  - Invokes `bcftools consensus -m low_depth_mask.bed` to mask unsequenced/low-depth regions with **`N`**. The mask and the variants are applied together in reference coordinates, so indels cannot shift the mask.
  - Set `consensus.min_depth = 0` to turn masking off.
- **Honest Completeness Metric**:
  Accurately counts ambiguous `N` bases to report true genome completeness (`consensus_completeness_percent`), preventing uncovered samples from falsely claiming 100% completeness.
- **Tracked Metrics**: `consensus_min_depth_mask`, `consensus_masked_positions`, `consensus_mask_note` (e.g. `N = depth < 10 reads`), `consensus_length`, `consensus_ambiguous_bases`, `consensus_completeness_percent`.

### 7. Functional Variant Annotation (`annotate` / `annotate.py`)
- **Tool**: `snpEff` (v5.4).
- **Database**: Custom pre-built LSDV genome database (`NC_003027.1`).
- **Functionality**:
  - Annotates the marked caller VCF, so every call is annotated and keeps its `PASS` / `LowQual` label.
  - Annotates variants with amino acid alterations, codon changes, and HGVS nomenclature (`HGVS.c`, `HGVS.p`).
  - Classifies biological impact:
    - 🔴 **HIGH**: Frameshift mutations, stop-codon gained.
    - 🟡 **MODERATE**: Missense mutations, in-frame insertions/deletions.
    - 🟢 **LOW**: Synonymous mutations, start codon retained.
    - ⚪ **MODIFIER**: Upstream / downstream gene variants.
- **Tracked Metrics**: `variant_count` (all calls), `variant_count_pass` (calls used in the consensus), `snp_count`, `indel_count`, `by_impact`, `by_effect`.

### 8. Interactive Plotly Visualization (`visualise` / `visualization.py`)
- **Tool**: Plotly (Python).
- **Features**:
  - Standalone, interactive HTML report (`visualization.html`).
  - Reads `lsdv_reference_sequence.gff3` to display gene and CDS genomic tracks.
  - Dropdown Zoom Controls: Zoom into individual LSDV genes or inspect the full 151 kbp genome.
  - Functional Impact Filters: Filter mutations by impact level or consequence category.
  - Interactive Tooltips: Hovering over mutations reveals exact genomic position, reference/alternate allele, depth, allele frequency, gene name, protein alteration, and filter status (`PASS` / `LowQual`).
  - **Resilience Guard**: Wrapped in error-isolation handlers; a non-critical plotting error will skip the visualization stage without failing the scientific run.

### 9. Execution Summary Report (`report` / `report.py`)
- Generates a human-readable text report (`pipeline_report.txt`) summarizing:
  - Workflow settings and actual execution state.
  - Variant filter note, calls vs. PASS count, and the low-depth mask note with the number of masked positions, e.g.:
    ```text
    Variant filter : PASS = QUAL >= 20 and depth >= 10; only PASS used in consensus
    Variants       : 27 called, 23 PASS
    Low-depth mask : N = depth < 10 reads (1950 positions masked)
    ```
  - Stage execution durations.
  - Total reads, mapped read percentage, variant counts, and functional impact distributions.
  - Complete index of all generated output artifact paths.

---

## 📊 Benchmark Comparison Across Tool Combinations

The following benchmarks demonstrate real results obtained across different tool configurations on verified LSDV sample datasets. **Variants** counts every caller call (`variant_count`), which is unchanged by the PASS / LowQual marking; the PASS subset used in the consensus is reported separately as `variant_count_pass`.

| Sample | QC Tool | Aligner | Primer Trimming (`iVar`) | Variant Caller | Mapped Reads (%) | Variants | SNPs | Indels | Biological Observation |
| :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :--- |
| **631** | `fastp` | `BWA` | ❌ Disabled | `bcftools` | 99.17% | 155 | 139 | 16 | Untrimmed amplicon primers inflate variant counts |
| **631** | `fastp` | `BWA` | ✅ **Enabled** (`primers-2kb.bed`) | `bcftools` | 99.17% | **21** | 20 | 1 | ⭐ **Gold Standard**: Removes 134 false-positive primer mutations |
| **631** | `fastp` | `BWA` | ✅ **Enabled** (`primers-2kb.bed`) | `lofreq` | 99.17% | **71** | 71 | 0 | Sensitive to low-frequency minority sub-clones |
| **631** | `fastp` | `BWA` | ✅ **Enabled** (`primers-2kb.bed`) | `freebayes` | 99.17% | **25** | 20 | 5 | Haplotype-aware calling; detects complex indels |
| **717** | `fastp` | `BWA` | ❌ Disabled | `bcftools` | 93.92% | 137 | 122 | 15 | Untrimmed primer regions create false positives |
| **717** | `fastp` | `BWA` | ✅ **Enabled** (`primers-2kb.bed`) | `bcftools` | 93.92% | **27** | 24 | 3 | ⭐ **Gold Standard**: True biological variants identified |
| **717** | `fastp` | `BWA` | ✅ **Enabled** (`primers-2kb.bed`) | `lofreq` | 93.92% | **52** | 52 | 0 | Sub-clonal viral detection with indels enabled |
| **717** | `fastp` | `BWA` | ✅ **Enabled** (`primers-2kb.bed`) | `freebayes` | 93.92% | **80** | 63 | 17 | Comprehensive indel and multi-bp haplotype calling |

---

## 📥 Inputs & Extra Inputs Specification

| Input | Key | Required? | Accepted Extensions | Description |
| :--- | :--- | :---: | :--- | :--- |
| **Primary Input** | `input_path` | **Yes** | `.fastq.gz`, `.fq.gz` | Forward paired-end read FASTQ file (`R1`) |
| **Extra Input** | `read2` | **Yes** | `.fastq.gz`, `.fq.gz` | Reverse paired-end read FASTQ file (`R2`) |
| **Extra Input** | `primer_bed` | **Required when `primer.enabled = true`** | `.bed` | Amplicon primer scheme BED file (e.g. `primers-2kb.bed`). The API rejects the submission if this is missing when primer trimming is enabled. |

---

## ⚙️ Configurable Parameters (JSON Schema)

| Parameter | Type | Default | Valid Range / Options | Description |
| :--- | :---: | :---: | :---: | :--- |
| `threads` | integer | `4` | 1 – 16 | Number of CPU threads allocated to pipeline stages |
| `reference` | string | `"lsdv_reference_sequence.fasta"` | Filename | Reference FASTA filename in the shared references directory |
| `fastp.qualified_quality_phred` | integer | `20` | 0 – 40 | Minimum Phred quality score for fastp read filtering (Q20 = 99%) |
| `fastp.length_required` | integer | `50` | $\ge 1$ | Minimum read length in bp after quality trimming |
| `fastp.detect_adapter_for_pe` | boolean | `true` | true / false | Automatically detect and clip paired-end Illumina adapters |
| `primer.enabled` | boolean | `false` | true / false | Enable `iVar` primer trimming (requires `primer_bed` input) |
| `primer.min_length` | integer | `30` | $\ge 1$ | Minimum read length in bp after primer clipping |
| `primer.min_quality` | integer | `20` | 0 – 40 | Minimum Phred quality score for iVar sliding window |
| `primer.sliding_window` | integer | `4` | $\ge 1$ | Sliding window width in bp for iVar primer clipping |
| `variant.tool` | string | `"bcftools"` | `"bcftools"`, `"lofreq"`, `"freebayes"` | Choose viral variant caller algorithm; also selects the PASS rule (see stage 5) |
| `variant.ploidy` | integer | `1` | 1 – 2 | Ploidy level (1 for haploid viral genomes) |
| `consensus.enabled` | boolean | `true` | true / false | Enable consensus genome generation |
| `consensus.min_af` | float | `0.5` | 0.0 – 1.0 | **LoFreq only**: calls at or above this allele frequency are marked PASS |
| `consensus.min_depth` | integer | `10` | $\ge 0$ | Positions with depth below this are masked with `N`; `0` = off |
| `visualization.enabled` | boolean | `true` | true / false | Generate interactive Plotly HTML report |

---

## 🐳 Container Images

All stages execute inside containerized environments:

* **Core Pipeline Image**: `dsihartpark/dharini-illumina-core:1.0.0`
  * Contains: `fastp=1.3.3`, `bwa=0.7.18`, `samtools=1.22.1`, `bcftools=1.22`, `ivar=1.4.4`, `lofreq=2.1.5`, `freebayes=1.3.10` (plus `fastqc`, `trimmomatic`, `bowtie2`, which the pipeline does not currently use).
* **Visualization**: runs in the worker's own Python environment (`plotly`), not in a tool container.
* **Annotation Image**: `dsihartpark/dharini-snpeff:5.4-lsdv-1`
  * Contains: `snpEff=5.4` configured with custom LSDV `NC_003027.1` reference database.

---

## 📊 Published Output Artifacts

Upon completion, the pipeline publishes the following artifacts:

| Artifact Filename | Category | Description |
| :--- | :---: | :--- |
| `fastp.html` / `fastp.json` | `qc` | Interactive HTML quality graphs and machine-readable QC metrics |
| `alignment.flagstat.txt` | `alignment` | Mapping statistics from `samtools flagstat` |
| `alignment.coverage.txt` | `alignment` | Contig-level coverage and mean depth from `samtools coverage` |
| `variants.<caller>.vcf.gz` | `vcf` | Compressed caller VCF with every call marked `PASS` / `LowQual` (its `.tbi` index is not published) |
| `annotated.vcf` | `vcf` | `snpEff`-annotated version of the marked VCF, `PASS` / `LowQual` labels kept |
| `consensus.fasta` | `consensus` | Consensus FASTA genome built from `PASS` calls, with low-depth regions masked as `N` |
| `visualization.html` | `visualization` | Standalone interactive Plotly HTML report with zoom & gene tracks |
| `pipeline_report.txt` | `report` | Complete human-readable execution summary and metrics log |

---

## 🚀 Execution Example (Python API)

```python
from pathlib import Path
from app.config import RUNS_ROOT, REFS_ROOT
from app.runtime.context import RunContext, RunPaths, NullRecorder
from app.pipelines.illumina_lsdv import IlluminaLsdvPipeline

# 1. Initialize run layout
run_id = "sample_717_analysis"
paths = RunPaths.for_run(RUNS_ROOT, run_id).create()

# 2. Place input files inside runs/<run_id>/input/
# - input/reads_R1.fastq.gz
# - input/reads_R2.fastq.gz
# - input/primer.bed (optional)

# 3. Configure pipeline run parameters
params = {
    "threads": 8,
    "reference": "lsdv_reference_sequence.fasta",
    "fastp": {
        "qualified_quality_phred": 20,
        "length_required": 50,
        "detect_adapter_for_pe": True,
    },
    "primer": {
        "enabled": True,
        "min_length": 30,
        "min_quality": 20,
        "sliding_window": 4,
    },
    "variant": {
        "tool": "bcftools",
        "ploidy": 1,
    },
    "consensus": {
        "enabled": True,
        "min_depth": 10,  # Low-depth masking cutoff
    },
    "visualization": {
        "enabled": True,
    },
}

ctx = RunContext(
    run_id=run_id,
    params=params,
    paths=paths,
    refs_dir=REFS_ROOT,
    recorder=NullRecorder(),
    stages=IlluminaLsdvPipeline().stages,
    input_name="reads_R1.fastq.gz",
    extra_inputs={
        "read2": "reads_R2.fastq.gz",
        "primer_bed": "primer.bed",
    },
)

# 4. Execute pipeline
pipeline = IlluminaLsdvPipeline()
result = pipeline.run(ctx)

print(f"Status: {result.status}")
print(f"Variants Found: {result.metrics.get('variant_count')} ({result.metrics.get('variant_count_pass')} PASS)")
print(f"Genome Completeness: {result.metrics.get('consensus_completeness_percent')}%")
```
