# FASTA Assembly Variant Calling Pipeline (`snippy-fasta`)

> **Calls variants from an assembled LSDV genome (FASTA) against the LSDV reference with Snippy, annotates them with snpEff, and renders an interactive variant plot.**

---

## 📖 What it is for

Use this pipeline when you already have an **assembled genome** (a FASTA file, for example a consensus downloaded from NCBI such as `PP488411.1`) rather than raw reads.

* **Reference**: LSDV RefSeq [`NC_003027.1`](https://www.ncbi.nlm.nih.gov/nuccore/NC_003027.1), the same reference and snpEff database used by `illumina-lsdv` and `ont-lsdv`, so a variant gets the same annotation whichever pipeline produced it.
* **Input**: one assembled FASTA (`.fasta`, `.fa`, `.fna`, `.fasta.gz`, `.fa.gz`).
* **Version**: `2.1.0`

---

## 🔄 Pipeline Architecture

```text
Assembled FASTA (assembly.fasta)
        │
        ▼
   [Stage: snippy] ──────────────► snps.vcf
   (Snippy --ctgs: shred → bwa → freebayes → filter)
        │
        ▼
   [Stage: annotate] ────────────► annotated.vcf
   (snpEff, LSDV database)
        │
        ▼
   [Stage: visualise] ───────────► visualization.html
   (interactive Plotly variant plot)
```

---

## 🔬 Stage-by-Stage Breakdown

### 1. Variant calling (`snippy`)

Snippy is a **wrapper**: it runs several tools in a row. Because the input is an assembly and not reads, `--ctgs` makes Snippy first cut the FASTA into overlapping **pseudo-reads** (about 20x depth), then:

1. **bwa mem** aligns the pseudo-reads to the reference.
2. **samtools** sorts and indexes the alignment.
3. **freebayes** calls variants (haploid).
4. **bcftools / vt** filter and normalise the calls using `--mincov`, `--minfrac` and Snippy's default minimum quality (`--minqual 100`).

Command run by the pipeline:

```bash
snippy --force \
  --ref <LSDV reference> \
  --ctgs assembly.fasta \
  --outdir work/snippy \
  --rgid <sample_name or run id> \
  --ram <ram_gb> --cpus <cpus> \
  --mincov <mincov> --minfrac <minfrac>
```

CPU and RAM are clamped to what the stage container is given (`MAX_STAGE_CPUS`, and `MAX_STAGE_MEMORY_GB` minus 1 GB headroom). Any clamping is written to the run log.

### 2. Annotation (`annotate`)

```bash
snpEff -v LSDV snps.vcf > annotated.vcf
```

Adds the `ANN` field: gene, effect (missense, synonymous, ...), impact and protein change.

### 3. Visualisation (`visualise`)

Builds `visualization.html`, an interactive plot of the annotated variants along the genome with gene tracks from the reference GFF. A failure here is logged and the stage marked skipped. It never fails a run whose variant calling succeeded.

---

## 🏷️ Sample Name in the VCF

The last column of the VCF `#CHROM` line is the **sample name**.

* Without `--rgid`, Snippy names the sample after the last folder of `--outdir`. The pipeline always uses `work/snippy`, so **before 2.1.0 every VCF said `snippy`**.
* From **2.1.0**, the pipeline passes `--rgid`:
  * `sample_name` given → that name is used (e.g. the Sample ID from Dharini).
  * `sample_name` empty → the **run id** is used, so the VCF is still unique and traceable to its run.
* Allowed characters: letters, digits, `.`, `_`, `-` (max 64). Spaces and `/` are rejected.

```text
Before (2.0.0):  #CHROM  POS  ID  REF  ALT  QUAL  FILTER  INFO  FORMAT  snippy
After  (2.1.0):  #CHROM  POS  ID  REF  ALT  QUAL  FILTER  INFO  FORMAT  PP488411.1
```

The name is carried into `annotated.vcf` unchanged.

> **Dharini integration:** the platform must send the submission's Sample ID as `params.sample_name`. Until it does, VCFs carry the run id.

---

## ⚙️ Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `cpus` | integer (1–16) | `2` | Cores given to Snippy (clamped to the stage limit). |
| `ram_gb` | integer (1–32) | `2` | Memory for Snippy in GB (clamped to the stage limit minus 1 GB). |
| `mincov` | integer (1–100) | `10` | Minimum depth of pseudo-reads at a site to call a variant. |
| `minfrac` | number (0–1) | `0.0` | Minimum fraction of reads supporting the variant. `0` = no fraction filter. |
| `sample_name` | string | `""` | Sample name written to the VCF. Empty = run id. |

Example request parameters:

```json
{ "cpus": 2, "ram_gb": 2, "mincov": 10, "minfrac": 0.0, "sample_name": "PP488411.1" }
```

---

## 🐳 Container Images

| Stage | Image | Contents |
|---|---|---|
| `snippy` | `dsihartpark/dharini-snippy` | Snippy 4.6.0 and its toolchain (bwa, samtools, freebayes, bcftools, vt) |
| `annotate` | `dsihartpark/dharini-snpeff` | snpEff 5.4 with the LSDV database |

Both are shared with `ont-lsdv` and pinned in `images.lock.json`.

---

## 📊 Published Outputs

| File | Kind | Description |
|---|---|---|
| `snps.vcf` | vcf | Snippy's variants (raw) |
| `annotated.vcf` | vcf | The same variants with snpEff `ANN` annotation |
| `visualization.html` | report | Interactive variant plot |

Variant rows and summary metrics (counts by type and effect) are also stored with the run.

---

## 🚀 Local Test

```bash
make smoke PIPELINE=snippy-fasta INPUT=runs/your-assembly.fasta \
  ARGS="--run-id test-snippy --param sample_name=PP488411.1"

# check the sample name
grep "^#CHROM" runs/test-snippy/output/snps.vcf
```

The input must be under the checkout (for example `runs/`), because that is what the dev container mounts.

---

## 📝 Changelog

| Version | Change |
|---|---|
| `2.1.0` | Added `sample_name`; Snippy now runs with `--rgid`, so the VCF sample column is the sample name (or the run id) instead of `snippy`. |
| `2.0.0` | Restructured as its own package with its own image manifest and stage specs. |
