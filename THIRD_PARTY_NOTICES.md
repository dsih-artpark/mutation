# Third-party notices

This repository's code is licensed under the [Apache License 2.0](LICENSE). The
tools below are **not** part of it. Each runs as a separate process in its own
container (see `app/runtime/runner.py`), is never linked or modified, and stays
under its own licence.

Compiled from the LSDV tool inventory and the pinned Dockerfiles. Rows marked †
come from conda package metadata; confirm them against upstream before a formal
release.

## Not OSI-approved open source

| Component | Licence | Where it appears | What this means |
| --- | --- | --- | --- |
| **Medaka** — <https://github.com/nanoporetech/medaka> | ONT PLC Public License v1.0 (research-oriented, **not** OSI-approved); the models too | `ont-lsdv`, only when `variant.tool=medaka` or `consensus.tool=medaka` | **Optional, never the default.** Check the licence permits your use before enabling it. Open alternatives: Clair3 or bcftools for variants, bcftools for consensus. |

Other components worth a deliberate choice:

| Component | Licence | Where it appears | What this means |
| --- | --- | --- | --- |
| **Redis** (`redis:7`) | 7.4+: RSALv2 / SSPL (not OSI). ≤ 7.2: BSD-3-Clause. | Celery broker. Pulled from Docker Hub when you run the stack; not redistributed here. | For a strictly OSI-licensed broker use [Valkey](https://valkey.io) (BSD-3-Clause, compatible) or pin Redis ≤ 7.2. |
| **MinIO** (`minio/minio`) | AGPL-3.0 | `dev` compose profile only, as a test S3 endpoint | Not part of a deployment. |

## Tools run inside the container images

| Tool | Role | Image(s) | Upstream | Licence |
| --- | --- | --- | --- | --- |
| fastp | read QC, filtering, adapters | illumina core, ont core | <https://github.com/OpenGene/fastp> | MIT |
| FastQC † | read QC | illumina core | <https://github.com/s-andrews/FastQC> | GPL-3.0-or-later |
| Trimmomatic † | read trimming | illumina core | <https://github.com/usadellab/Trimmomatic> | GPL-3.0 |
| BWA | short-read alignment | illumina core | <https://github.com/lh3/bwa> | GPL-3.0 |
| Bowtie2 | short-read alignment (optional) | illumina core | <https://github.com/BenLangmead/bowtie2> | GPL-3.0 |
| minimap2 | long-read alignment | ont core | <https://github.com/lh3/minimap2> | MIT |
| SAMtools | BAM sort, index, stats | all | <https://github.com/samtools/samtools> | MIT |
| BCFtools | variant call, filter, consensus | illumina core, ont core | <https://github.com/samtools/bcftools> | MIT; **GPL-3.0 when built with GSL**, which Bioconda builds are † |
| iVar | amplicon primer trimming | illumina core | <https://github.com/andersen-lab/ivar> | GPL-3.0 |
| LoFreq | sensitive variant calling | illumina core | <https://github.com/CSB5/lofreq> | MIT |
| FreeBayes | haplotype variant calling | illumina core | <https://github.com/freebayes/freebayes> | MIT |
| NanoPlot | ONT QC plots | ont core | <https://github.com/wdecoster/NanoPlot> | MIT |
| NanoFilt | ONT read filtering | ont core | <https://github.com/wdecoster/nanofilt> | GPL-3.0 |
| ARTIC `align_trim` | ARTIC-scheme primer trimming | align-trim | <https://github.com/artic-network/fieldbioinformatics> | MIT |
| BamClipper | primer soft-clipping | bamclipper | <https://github.com/tommyau/bamclipper> | MIT |
| GNU parallel † | dependency of BamClipper | bamclipper | <https://www.gnu.org/software/parallel/> | GPL-3.0-or-later |
| Clair3 | ONT variant calling (**default**) | `hkubal/clair3:v1.0.10` (upstream image, unmodified) | <https://github.com/HKU-BAL/Clair3> | BSD-3-Clause |
| Longshot | long-read SNV calling | with Clair3, where used | <https://github.com/pjedge/longshot> | MIT |
| **Medaka** | optional consensus / variants | `quay.io/biocontainers/medaka` (unmodified) | <https://github.com/nanoporetech/medaka> | **ONT PLC Public License v1.0 — see above** |
| Snippy | reference-based variant calling | snippy | <https://github.com/tseemann/snippy> | GPL-2.0 |
| vcflib † | Snippy dependency (pinned 1.0.2) | snippy | <https://github.com/vcflib/vcflib> | MIT |
| OpenJDK † | Java runtime for snpEff | snippy, snpeff | <https://openjdk.org> | GPL-2.0 with Classpath Exception |
| BioPerl †, MUMmer4 †, gffread † | Snippy dependencies | snippy | Bioconda recipes | Artistic / GPL / MIT — see each recipe |
| SnpEff (5.4 / 5.4c) | variant effect annotation | snpeff, snippy | <https://pcingola.github.io/SnpEff/> | MIT per upstream docs; **LGPL-3.0** per Bioconda metadata. Both are OSI-approved; keep the notices when redistributing. |
| Python, pandas, Plotly | metrics and HTML visualisation | all | python.org / pandas / plotly | PSF / BSD-3-Clause / MIT |

## Python dependencies of the service

Installed from PyPI; none is vendored here.

| Package | Licence |
| --- | --- |
| FastAPI | MIT |
| Uvicorn | BSD-3-Clause |
| Celery, Kombu | BSD-3-Clause |
| redis-py | MIT |
| python-dotenv | BSD-3-Clause |
| pandas | BSD-3-Clause |
| Plotly (including the bundled plotly.js) | MIT |
| SQLAlchemy | MIT |
| Alembic | MIT |
| psycopg 3 (`psycopg[binary]`) | **LGPL-3.0** — used as an unmodified, dynamically imported library; the binary wheel also bundles libpq (PostgreSQL licence) and OpenSSL (Apache-2.0) |
| jsonschema | MIT |
| PyYAML | MIT |
| HTTPX | BSD-3-Clause |
| boto3 | Apache-2.0 |
| PyJWT, cryptography | MIT; Apache-2.0 OR BSD-3-Clause |
| pytest (dev only) | MIT |

## Container base images and services

| Image | Licence |
| --- | --- |
| `python:3.13-slim` (Debian + CPython) | PSF; Debian packages under their own licences |
| Docker CLI (`docker-ce-cli`, `docker-buildx-plugin`) | Apache-2.0 |
| `mambaorg/micromamba` | BSD-3-Clause (micromamba); conda-forge/Bioconda packages under their own licences |
| `postgres:16-alpine` | PostgreSQL Licence |

## Data

`data/lsdv_reference_sequence.*` is the LSDV reference genome (GenBank
`NC_003027`) from NCBI; the snpEff image builds its database from the same
record. See the [NCBI data-usage policy](https://www.ncbi.nlm.nih.gov/home/about/policies/).

## Redistributing the published container images

The images under `dsihartpark/*` contain GPL software (for example Snippy, BWA,
Bowtie2, iVar, NanoFilt, and BCFtools built with GSL). Distributing them carries
a source obligation. Every Dockerfile pins exact package versions, so the same
environment can be rebuilt from Bioconda and conda-forge, which host the
upstream sources. If you republish these images, keep the Dockerfiles and this
file with them.
