# Multi-Pipeline Architecture

**Date:** 2026-08-04 (revised 2026-08-05)
**Status:** Approved for implementation

Restructure the Dharini pipeline service so a new analysis pipeline is one
self-contained directory, every tool runs in a declared container image, and
large inputs arrive through S3 instead of through the API.

---

## 1. Problem

The platform/pipeline boundary is already well drawn: `app/tasks.py` never
branches on pipeline identity, `POST /v1/runs` is one generic route, and the
`Pipeline` + `RunContext` contract funnels tool execution through a single
`StageRunner`. That part works and is preserved.

Five things break when a third pipeline is added.

**Images are declared in three unsynchronised places.** `ont_lsdv/specs.py`
holds six `os.getenv("ONT_IMAGE_*", ...)` calls; `.env.example` repeats them;
`readme.md` repeats them again in a hand-written `docker pull` loop. `ALL_IMAGES`
is defined and read by nothing. Three of the six images (`suhas0/*`) have no
Dockerfile in the repository and cannot be rebuilt or audited. Every tag is
mutable. `Dockerfile_lsdv_core_v4` sits at the repository root, untracked, wired
to nothing. There is no build step for tool images at all.

**Half the tools run outside containers, and the resource caps silently do not
apply to them.** `StageSpec(image=None)` exists solely for `snippy-fasta`.
`StageRunner._run_local` applies only `spec.timeout_s` — it never applies `cpus`
or `memory_gb`, and never clamps to `MAX_STAGE_CPUS` / `MAX_STAGE_MEMORY_GB`.
So `snippy_fasta.py:76`'s `LOCAL.with_(cpus=cpus, memory_gb=ram + 1)` is
silently ignored, and a submission with `ram_gb: 32` is not clamped to the 8 GB
ceiling. The documented guarantee that "a mis-declared spec cannot claim the
whole box" is currently false for exactly this pipeline. Supporting this path
also costs a conda base image, `_conda_env()`'s `JAVA_HOME`/`PERL5LIB` path
guessing, three conda-prefix fallbacks in config, a second execution path in the
runner, and `setup_snpeff_lsdv.sh` running inside the application image build —
which makes an NCBI outage break the application build.

**The API has a pipeline-specific leak.** `primer_bed` is hardcoded in the
`POST /v1/runs` signature. A pipeline needing a different secondary file
requires an API change.

**Reference data shares one flat namespace.** `REFS_ROOT` is a single directory
and `prepare_reference()` reads a bare filename out of it, so two pipelines both
wanting `reference.fasta` collide silently. `REFERENCE_PATH` and `SNPEFF_DB` are
global environment variables serving one pipeline each.

**Large inputs go through the API process.** A 1 GB FASTQ streamed through
uvicorn holds a connection for minutes, consumes local disk, and makes
`MAX_UPLOAD_SIZE=5GB` a timeout problem rather than a limit. There is also no
idempotency key, so a client retry after a timeout starts a second run.

Two secondary problems are folded in because they touch the same files:
`ONTpipeline/` duplicates `app/pipelines/ont_lsdv/` (the same science in two
places, guaranteed to drift), and `app/visualization.py` is 493 lines of
snippy-specific code sitting in the shared application layer.

---

## 2. Goals

- Adding a pipeline touches exactly one new directory and no existing file.
- Every tool runs in a container. One execution path, one set of resource caps.
- Container images are declared once, built by CI, and pinned by digest at
  runtime without anyone remembering to do it.
- Custom images are reproducible: rebuilding a Dockerfile next year produces the
  same tool versions.
- Reference data and tool databases are declared, checksummed, and namespaced.
- Inputs of any size upload directly to S3; the service never handles the bytes.
- A developer can run a pipeline end to end on a committed fixture with no
  Postgres, Redis, S3 or Celery.

### Non-goals

- A declarative workflow/DAG engine, or adopting Nextflow/Snakemake. Evaluated
  and rejected at this scale — see §15.
- Multi-host scheduling, GPU support, or autoscaling.
- S3 multipart upload. Single-part presigned PUT caps at 5 GB, matching the
  existing `MAX_UPLOAD_BYTES`.
- Full Alembic adoption. One hand-written migration is required (§12).

---

## 3. Decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | YAML declares **images, assets and fixtures only**; all execution logic, stage order and resource limits stay in Python | A `StageSpec` is only meaningful beside the argv it constrains. Deploy tooling must read images without importing FastAPI/Celery/SQLAlchemy. |
| D2 | Images are **built and pushed by CI**; the production host only pulls | Fast, reproducible deploys. Builds do not compete with running jobs. Staging and production run byte-identical images. |
| D3 | Images are published to **public Docker Hub** under `dsihartpark/` | Simple pulls, no read credentials on the host. All content is open-source tooling. |
| D4 | Assets are **declared in the manifest, fetched host-side, namespaced** per scope | References stay swappable, checksums verifiable, no filename collisions between pipelines. |
| D5 | Pipelines are **authored directly against the `Pipeline` contract** | One source of truth. No port step, no drift. `ONTpipeline/` is deleted once `ont-lsdv` passes an end-to-end run. |
| D6 | Clients **PUT to a presigned S3 URL**; the service issues the URL and later fetches the object | Bytes never touch our host. |
| D7 | **One generic route for every pipeline** — no per-pipeline routes | A route per pipeline reintroduces exactly the coupling being removed. |
| D8 | BamClipper is pulled from **`quay.io/biocontainers/bamclipper`** | Published upstream; no Dockerfile to maintain. |
| D9 | snpEff is **built from a parameterised Dockerfile** at **version 5.4**, organism supplied as a build arg | Upstream carries no LSDV database. 5.4 matches the version `snippy-fasta` uses today, so that pipeline's output does not change. Parameterising means pathogen #2 is a manifest entry, not a second Dockerfile. |
| D10 | **Both pipelines annotate with the same shared snpEff image** | They currently use different snpEff versions, so the same variant can be annotated differently depending on which pipeline was used. |
| D11 | **Every stage runs in a container.** `StageSpec.image` becomes required; `_run_local`, `_conda_env` and the conda base image are deleted | Removes more code than it adds. Makes the resource-cap guarantee true instead of documented-with-an-exception. |
| D12 | Self-built images use **micromamba + bioconda with pinned versions** | `Dockerfile_lsdv_core_v4` pins nothing, so a rebuild silently changes tool versions; and Ubuntu 22.04's bioinformatics packages are several years old. |
| D13 | Images are grouped by the **pipe rule** (§6), not one-per-tool and not one-per-pipeline | Tools on either side of a shell pipe must share an image; materialising an intermediate SAM is not acceptable. |
| D14 | Test fixtures are **two-tier**: committed micro-fixtures and S3-hosted realistic samples | A real ONT FASTQ is ~1 GB and cannot live in git; a subsampled one exercises every stage in CI. |
| D15 | Clients supply an **`external_id`**, unique per run | Provides idempotency on retry and correlation that survives a lost submit response. |
| D16 | **Webhook for completion, status endpoint for progress, reconciliation for missed deliveries** | Each answers a different question; none of the three substitutes for the others. |

---

## 4. Repository layout

```
shared/
├── manifest.yaml                  # cross-pipeline images and assets
└── docker/
    ├── snpeff/Dockerfile
    └── snippy/Dockerfile

app/
├── config.py                      # platform config only
├── main.py  db.py  models.py  schemas.py  worker.py  tasks.py
├── vcf.py  delivery.py
├── run_local.py                   # NEW  CLI: run a pipeline with no infra
├── api/
│   ├── uploads.py                 # NEW  presigned PUT issuance
│   ├── runs.py                    # generic; extra_inputs-driven
│   ├── pipelines.py
│   └── legacy.py
├── runtime/
│   ├── spec.py                    # StageSpec (image now required)
│   ├── runner.py                  # container execution only
│   ├── context.py                 # RunContext (+ ctx.asset)
│   ├── manifest.py                # NEW  manifest parsing + validation
│   ├── images.py                  # NEW  logical key -> pinned image ref
│   ├── assets.py                  # NEW  logical key -> absolute path
│   └── storage.py                 # NEW  ObjectStore: S3Store | LocalStore
└── pipelines/
    ├── __init__.py                # pkgutil scan — no import list
    ├── base.py  registry.py
    ├── snippy_fasta/
    │   ├── manifest.yaml
    │   ├── pipeline.py  specs.py
    │   ├── visualization.py       # moved from app/
    │   └── tests/
    │       ├── test_pipeline.py
    │       └── fixtures/assembly.fasta
    └── ont_lsdv/
        ├── manifest.yaml
        ├── pipeline.py  specs.py  common.py
        ├── qc.py  align.py  primer.py  variant.py
        ├── consensus.py  annotate.py  report.py
        ├── docker/core/Dockerfile
        └── tests/
            ├── test_pipeline.py
            └── fixtures/reads.subsampled.fastq.gz

tools/                             # stdlib + PyYAML only; no app imports
├── images.py                      # build | push | lock | pull
├── assets.py                      # fetch | verify
├── fixtures.py                    # fetch tier-2 samples
└── scaffold.py                    # make new-pipeline

images.lock.json                   # committed; CI-generated
Makefile
.github/workflows/images.yml
```

Stage modules stay flat inside the pipeline package. Moving seven 30–200 line
files into a `stages/` subdirectory was considered and rejected: churn with no
benefit.

---

## 5. Manifests

Two scopes. A pipeline manifest lives beside its code. The shared manifest holds
anything more than one pipeline uses.

### 5.1 Shared — `shared/manifest.yaml`

```yaml
schema_version: 1
scope: shared

images:
  snpeff-lsdv:
    source: build
    dockerfile: docker/snpeff/Dockerfile
    context: docker/snpeff
    repository: dsihartpark/dharini-snpeff
    version: "5.4-lsdv-1"
    platform: linux/amd64
    build_args:
      ACCESSION: NC_003027
      DB_NAME: LSDV

  snippy:
    source: build
    dockerfile: docker/snippy/Dockerfile
    context: docker/snippy
    repository: dsihartpark/dharini-snippy
    version: "4.6.0-1"
    platform: linux/amd64

assets:
  reference:
    url: "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=nuccore&id=NC_003027&rettype=fasta&retmode=text"
    filename: lsdv_reference.fasta
    sha256: ""
  annotation:
    url: "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=nuccore&id=NC_003027&rettype=gff3&retmode=text"
    filename: lsdv_reference.gff3
    sha256: ""
```

**The LSDV reference genome and the snpEff database are both shared**, because
both pipelines use both. This is not merely convenient. The alignment reference
and the annotation database must derive from the same accession, or variants are
annotated against the wrong coordinates and nothing errors. Today
`setup_snpeff_lsdv.sh` guarantees it by accident — one shell variable writes
both.

`NC_003027` is therefore written in two places in this file, and a contract test
asserts every occurrence agrees. A templating layer (`${vars.accession}`) was
considered and rejected: the test is required either way, since the templating
could itself be wired wrong, so templating would be pure addition.

An empty `sha256` means "not yet pinned". `tools/assets.py fetch
--write-checksums` downloads, computes, and writes digests back into the
manifest; the value is then committed and verified on every fetch. A non-empty
checksum that does not match is a hard failure. The contract test rejects an
empty `sha256` outside local development.

### 5.2 Pipeline — `app/pipelines/ont_lsdv/manifest.yaml`

```yaml
schema_version: 1
scope: ont-lsdv                    # must equal Pipeline.id

images:
  core:
    source: build
    dockerfile: docker/core/Dockerfile
    context: docker/core
    repository: dsihartpark/dharini-ont-core
    version: "1.0.0"
    platform: linux/amd64

  medaka:     { source: registry, repository: ontresearch/medaka,            version: "2.0.1" }
  clair3:     { source: registry, repository: hkubal/clair3,                 version: "v1.0.10" }
  longshot:   { source: registry, repository: staphb/longshot,               version: "0.4.5" }
  bamclipper: { source: registry, repository: quay.io/biocontainers/bamclipper, version: "1.0.0--hdfd78af_2" }

fixtures:
  realistic_reads:
    url: "s3://dharini-fixtures/ont-lsdv/SRR25593956.fastq.gz"
    filename: SRR25593956.fastq.gz
```

`ont-lsdv` declares no assets of its own; it consumes `shared:reference`.

### 5.3 Field reference

| Field | Applies to | Meaning |
|---|---|---|
| `schema_version` | manifest | Integer. Currently `1`. |
| `scope` | manifest | `shared`, or a pipeline id equal to `Pipeline.id`. |
| `images.<key>.source` | image | `build` or `registry`. |
| `images.<key>.dockerfile` | build | Path relative to the manifest directory. |
| `images.<key>.context` | build | Path relative to the manifest directory. Defaults to the Dockerfile's directory. |
| `images.<key>.build_args` | build | String map passed as `--build-arg`. |
| `images.<key>.platform` | build | Defaults to `linux/amd64`. |
| `images.<key>.repository` | image | Registry path without a tag. |
| `images.<key>.version` | image | Tag. `latest` is rejected by the contract test. |
| `assets.<key>.url` | asset | HTTPS source. |
| `assets.<key>.filename` | asset | Basename on disk. No path separators. |
| `assets.<key>.sha256` | asset | Verified on every fetch. |
| `fixtures.<key>` | fixture | Tier-2 test sample. Same fields as an asset; `sha256` optional. |

---

## 6. Image granularity

Three rules, in priority order.

**1. Tools on either side of a shell pipe must share an image.** This is a hard
constraint from the code, not a preference. `align.py:24` runs
`minimap2 ... | samtools sort ...`; splitting them would mean materialising an
intermediate SAM several times the size of the BAM. Same in `qc.py:80`
(`gzip | NanoFilt | gzip`) and `variant.py:44` (`bcftools mpileup | bcftools call`).

**2. Tools with irreconcilable dependencies must be separate.** Medaka
(TensorFlow), Clair3 (PyTorch) and Longshot (Rust/htslib) cannot coexist in one
environment. This is the original reason images exist here.

**3. Tools with different change cadence, or different consumers, should be
separate.** Snippy is frozen at 4.6.0 and used by one pipeline. The snpEff image
carries the *organism database* and changes whenever a pathogen is added, and is
used by both pipelines. Bundling them would rebuild Snippy for every new
pathogen and produce a new digest for a stage whose tooling did not change —
defeating the question digest pinning exists to answer.

Resulting set:

| Image | Source | Maintained by |
|---|---|---|
| `shared:snpeff-lsdv` | self-built — snpEff 5.4 + LSDV database | **us** |
| `shared:snippy` | self-built — bioconda `snippy=4.6.0` | **us** |
| `ont-lsdv:core` | self-built — minimap2, samtools, bcftools, fastp, NanoPack | **us** |
| `ont-lsdv:medaka` | `ontresearch/medaka` | upstream |
| `ont-lsdv:clair3` | `hkubal/clair3` | upstream |
| `ont-lsdv:longshot` | `staphb/longshot` | upstream |
| `ont-lsdv:bamclipper` | `quay.io/biocontainers/bamclipper` | upstream |

Seven images, three self-built. Every self-built image is a CVE surface and a
version owned forever, so the ratio matters.

> **Note on Snippy.** `staphb/snippy` publishes `4.6.0-SC2` as its only 4.6.0
> tag. The `-SC2` suffix indicates a SARS-CoV-2 build variant, and `snippy-fasta`
> is the pipeline whose output is already trusted — so this spec builds Snippy
> from bioconda with the identical `snippy=4.6.0` pin used in the current conda
> environment, rather than adopting an image whose specialisation is unverified.
> If the team would rather maintain two images than three, verify
> `staphb/snippy:4.6.0-SC2` reports plain snippy 4.6.0 and applies no implicit
> reference, then substitute it.

### 6.1 Dockerfiles

All self-built images follow one pattern: micromamba, bioconda, pinned versions.

```dockerfile
# ont_lsdv/docker/core/Dockerfile
FROM mambaorg/micromamba:1.5-jammy
RUN micromamba install -y -n base -c bioconda -c conda-forge \
      minimap2=2.28 samtools=1.21 bcftools=1.21 fastp=0.24.0 \
      nanofilt=2.8.0 nanoplot=1.42.0 \
 && micromamba clean -a -y
ENV PATH=/opt/conda/bin:$PATH
```

```dockerfile
# shared/docker/snippy/Dockerfile
# Replicates the current application conda environment exactly, so snippy-fasta
# produces identical output. snpeff is present because snippy declares it as a
# dependency; the annotate stage uses the separate shared snpeff image.
FROM mambaorg/micromamba:1.5-jammy
RUN micromamba install -y -n base -c bioconda -c conda-forge \
      snippy=4.6.0 samtools snpeff=5.4 openjdk=21 \
      perl-bioperl gffread mummer4 \
 && micromamba clean -a -y
ENV PATH=/opt/conda/bin:$PATH
```

```dockerfile
# shared/docker/snpeff/Dockerfile
FROM mambaorg/micromamba:1.5-jammy

ARG ACCESSION=NC_003027
ARG DB_NAME=LSDV

RUN micromamba install -y -n base -c bioconda -c conda-forge \
      snpeff=5.4 openjdk=21 curl \
 && micromamba clean -a -y
ENV PATH=/opt/conda/bin:$PATH

RUN set -eux; \
    share="$(ls -d /opt/conda/share/snpeff-* | head -n1)"; \
    ln -s "$share" /snpEff; \
    cfg="/snpEff/snpEff.config"; \
    d="/snpEff/data/${DB_NAME}"; mkdir -p "$d"; \
    base="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=nuccore&id=${ACCESSION}"; \
    curl -fsSL --retry 3 --retry-delay 5 "${base}&rettype=fasta&retmode=text" -o "$d/sequences.fa"; \
    curl -fsSL --retry 3 --retry-delay 5 "${base}&rettype=gff3&retmode=text"  -o "$d/genes.gff"; \
    test -s "$d/sequences.fa"; \
    grep -q '##gff-version 3' "$d/genes.gff"; \
    grep -q "^${DB_NAME}\.genome" "$cfg" || echo "${DB_NAME}.genome : ${DB_NAME}" >> "$cfg"; \
    snpEff build -gff3 -noCheckCds -noCheckProtein -v "${DB_NAME}"; \
    test -s "$d/snpEffectPredictor.bin"; \
    test -s "$cfg"

ENV SNPEFF_CONFIG=/snpEff/snpEff.config
```

**The `/snpEff` symlink is load-bearing.** bioconda installs to
`snpeff-5.4-<build>`, and the build suffix is not determined by the version pin —
so the directory is discovered, never assumed. Symlinking it to `/snpEff` gives
both pipelines a stable path: `ont-lsdv` passes `-c /snpEff/snpEff.config`
(where the previous `suhas0` image also kept it), and `snippy-fasta` lets snpEff
self-locate. Neither needs a code change, now or when a future bioconda rebuild
changes the build number.

Three deliberate differences from `setup_snpeff_lsdv.sh`:

- **`test -s snpEffectPredictor.bin`.** The script's retry loop treats
  `snpEff build` exiting 0 as success, but it can exit 0 having produced no
  usable database — discovered only during a real annotation. Asserting on the
  artifact at build time is the same rule `ctx.require()` enforces at runtime.
- **NCBI is contacted once at image build**, not on every application image
  build. Today an NCBI outage blocks the app image entirely.

The application image becomes `python:3.13-slim` plus the Docker CLI: no conda,
no Anaconda terms acceptance, no `setup_snpeff_lsdv.sh`, and a build measured in
seconds.

---

## 7. Image resolution

Pipelines reference images by **logical key**, never by string.

```python
# app/pipelines/ont_lsdv/specs.py
from ...runtime.images import images_for

img = images_for(__package__)          # bound to this pipeline's manifest

ALIGN  = StageSpec(image=img("core"),               cpus=4, memory_gb=6, timeout_s=2 * HOUR)
MEDAKA = StageSpec(image=img("medaka"),             cpus=4, memory_gb=8, timeout_s=4 * HOUR)
SNPEFF = StageSpec(image=img("shared:snpeff-lsdv"), cpus=2, memory_gb=4, timeout_s=HOUR)
```

A bare key resolves within the pipeline's own manifest; a `shared:` prefix
resolves against `shared/manifest.yaml`. There is no implicit fallback — where
an image comes from is always explicit.

Resolution order:

1. **Environment override** — `DHARINI_IMAGE__<SCOPE>__<KEY>`, uppercased with
   `-` replaced by `_`. `shared:snpeff-lsdv` becomes
   `DHARINI_IMAGE__SHARED__SNPEFF_LSDV`. Break-glass; no code change.
2. **`images.lock.json`** — the digest-pinned reference. The production path.
3. **`repository:version` from the manifest** — unpinned. Local development only.

When `REQUIRE_PINNED_IMAGES=true`, a key resolving to anything without an
`@sha256:` raises at worker startup, so the worker refuses to start rather than
producing an untraceable result.

`images_for()` records every ref it resolves per package, giving preflight (§10.3)
the exact image set a pipeline uses.

### 7.1 `images.lock.json`

Generated by CI, committed to git. Git supplies timestamp and author, so the
file carries no generation metadata and stays diff-clean.

```json
{
  "schema_version": 1,
  "images": {
    "shared:snpeff-lsdv": {
      "repository": "dsihartpark/dharini-snpeff",
      "version": "5.4-lsdv-1",
      "digest": "sha256:…",
      "ref": "dsihartpark/dharini-snpeff@sha256:…"
    }
  }
}
```

Digests are resolved uniformly for built and pulled images via
`docker inspect --format '{{index .RepoDigests 0}}'` after build-or-pull.

**Provenance falls out of this for free.** `RunStage.image` already persists the
image string per stage (`models.py:141`), so once keys resolve to digests, every
run permanently records the exact tool digest that produced it, queryable in
SQL. For a platform whose outputs inform animal-health decisions, that is the
provenance record — and it requires no new schema.

---

## 8. Assets and fixtures

Assets resolve to `REFS_ROOT/<scope>/<filename>`:

```
/srv/dharini/references/
└── shared/
    ├── lsdv_reference.fasta
    └── lsdv_reference.gff3
```

```python
reference = ctx.asset("shared:reference")     # absolute Path
```

`ctx.asset()` raises a clear, actionable error if the file is absent. It replaces
the client-supplied-filename plus path-traversal check in `ont_lsdv/common.py`:
the parameter is now a manifest key, not a path, so traversal is not
expressible.

`tools/assets.py fetch` downloads every declared asset, verifies its `sha256`,
and skips anything already present and matching. `tools/assets.py verify` checks
without downloading, for preflight and CI.

The `reference` parameter in `ont_lsdv`'s schema changes from a free filename to
an `enum` of asset keys — today `["shared:reference"]`, with a matching
`default`. A single-entry enum looks redundant but is the point: Dharini renders
a dropdown that grows by itself, and an unknown reference is a 422 at submission
rather than a failure at stage 1. A contract test asserts the enum matches the
declared assets.

### 8.1 Test fixtures — two tiers

**Tier 1, committed.** `app/pipelines/<id>/tests/fixtures/` holds subsampled
inputs under ~10 MB total: roughly 5,000 reads for `ont-lsdv`, a small assembly
for `snippy-fasta`. Small enough for git, large enough to exercise every stage
in a couple of minutes. CI runs the **actual pipeline** on these on every pull
request, not just unit tests. This is the highest-value testing decision in the
spec.

**Tier 2, S3-hosted.** Realistic full-size samples declared under `fixtures:` and
pulled by `make fixtures`. Used for the verification gates and performance work;
never in CI.

### 8.2 Local runner

Folders alone do not make a pipeline testable. `app/run_local.py` runs a pipeline
end to end with no Postgres, Redis, S3 or Celery:

```bash
python -m app.run_local --pipeline ont-lsdv \
  --input app/pipelines/ont_lsdv/tests/fixtures/reads.subsampled.fastq.gz \
  --params '{"variant":{"tool":"bcftools"}}' \
  --out ./scratch
```

It is roughly 60 lines: `NullRecorder` and `RunPaths` already exist, and
`LocalStore` comes from §9. This is what "a developer can test and see if
anything goes wrong" means in practice.

---

## 9. Object storage and the submission flow

### 9.1 Interface

```python
@dataclass(frozen=True)
class ObjectInfo:
    key: str
    size: int
    etag: str

class ObjectStore(Protocol):
    def presign_put(self, key: str, content_type: str, expires_s: int) -> str: ...
    def head(self, key: str) -> ObjectInfo | None: ...
    def download(self, key: str, dest: Path) -> int: ...
    def delete(self, key: str) -> None: ...
```

`S3Store` (boto3) and `LocalStore` (filesystem) implement it, selected by
`STORAGE_BACKEND=s3|local`. `LocalStore.presign_put()` returns a local path
rather than an HTTP URL — tests and `run_local` write the file directly, with no
round-trip and no token-signing machinery to build.

Credentials come from the instance IAM role. No access keys in `.env`.

### 9.2 Flow

```
POST /v1/uploads  {pipeline_id, filename, input: "primary" | "<extra_key>"}
  201 {object_key, upload_url, expires_at, max_bytes}

  Dharini ── PUT (up to 5 GB) ─────────────────────────────────► S3

POST /v1/runs     {pipeline_id, external_id, input_key, extra_input_keys,
                   params, callback_url}
  202 {run_id, external_id, pipeline_id, pipeline_version, status, stages,
       queue_position}
```

`POST /v1/runs` becomes a JSON body rather than multipart. `extra_input_keys`
maps an `extra_inputs` key to an object key —
`{"primer_bed": "uploads/9f3c…/primers.bedpe"}` — and is omitted when the
pipeline declares none.

**The server generates `object_key`; the client never invents one.** Format is
`uploads/<uuid4>/<pipeline.input_filename>`. The client's filename is validated
for extension and retained as run metadata only, consistent with the existing
principle that client filenames are hostile input.

**If Dharini prefers to upload with its own credentials** and skip
`POST /v1/uploads`, `POST /v1/runs` still accepts an `input_key` — but the key is
then client-controlled, so it is validated against a configured bucket and key
prefix (`S3_ALLOWED_PREFIX`) and a strict key shape. Without that guard the
service will fetch any object in the bucket it is pointed at. The
service-issued path is preferred because extension and size are validated
*before* a gigabyte moves.

Validation at submission, before anything is queued:

- pipeline exists; extension matches `accepted_extensions` or
  `extra_inputs[key].extensions`
- `HeadObject` confirms the object exists and is within `MAX_UPLOAD_BYTES`
- params validate against `params_schema`
- every `required` extra input is present

### 9.3 `external_id`, idempotency and correlation

Dharini supplies `external_id` at submission. It carries a unique constraint.

- **Idempotency.** Resubmitting the same `external_id` returns the existing run
  with `200` rather than creating a second one. Without this, a client timeout
  and retry starts two runs — on a single box, an hour of duplicated Medaka
  compute.
- **Correlation that survives a lost response.** If the `202` never reaches
  Dharini, it never learned the `run_id` and cannot map the eventual webhook.
  Because `external_id` is chosen by Dharini before submitting and echoed in the
  webhook payload, correlation works regardless.

---

## 10. Platform changes

### 10.1 Pipeline contract

```python
@dataclass(frozen=True)
class ExtraInput:
    filename: str                       # normalised name on disk
    extensions: tuple[str, ...]
    required: bool = False
    title: str = ""
    description: str = ""

class Pipeline(ABC):
    ...
    extra_inputs: dict[str, ExtraInput] = {}
```

`describe()` includes `extra_inputs`, so Dharini renders additional file pickers
from the registry by the same mechanism it already uses for `params_schema`.
This removes the hardcoded `primer_bed` parameter from `POST /v1/runs`.

### 10.2 Registration by discovery

`app/pipelines/__init__.py` replaces its hand-maintained import list with a
`pkgutil.iter_modules` scan, skipping names beginning with `_`. Import failures
re-raise with the offending module name, so a broken pipeline fails loudly
rather than silently vanishing from the registry.

### 10.3 The `fetch` stage and preflight

The platform prepends `fetch` to every pipeline's declared stages, so progress
reads `1/9 · fetch` and a 90-second download is visible rather than looking like
a hung run. `fetch` is a reserved stage name, rejected by the contract test if a
pipeline declares it. The prepended list is what `RunContext.declared_stages`
receives and what `POST /v1/runs` and `GET /v1/runs/{id}` report, so the
denominator a client sees matches the stages that will run.

Preflight runs as the first action inside `fetch`:

- every image the pipeline resolved is present (`docker image inspect`)
- every asset it declares exists on disk

A missing image fails the run in under a second with
`image dsihartpark/dharini-ont-core@sha256:… not present on this host; run 'make images-pull'`,
rather than dying at stage 5 with a confusing Docker error. `PreflightFailed`
subclasses `StageError`, so it flows through the existing failure, recording and
webhook paths unchanged.

### 10.4 Runner simplification

With `StageSpec.image` required, `StageRunner` loses `_run_local`,
`_conda_env()` and `_local_peak_mb()`, and `config.py` loses `DOCKER_CONDA`,
`DOCKER_CONDA_ALT` and `MAC_CONDA`. One execution path. Every stage gets real
CPU and memory caps and accurate peak-memory sampling — neither of which
currently applies to `snippy-fasta`.

### 10.5 Retention

`Run` gains `external_id`, `input_key` and `extra_input_keys`, replacing the
current practice of stashing `_input_name` and `_extra_inputs` inside the
`params` JSON column and stripping reserved keys later.
`cleanup_expired_runs` deletes the S3 objects alongside the local run directory.

---

## 11. Response flow

Three mechanisms answering three different questions. None substitutes for
another.

| Question | Mechanism |
|---|---|
| "Is it done?" | **Webhook** — `run.completed` / `run.failed`, HMAC-signed, retried across ~9 hours |
| "How far along is it?" | **`GET /v1/runs/{id}`** — per-stage progress from `run_stages` |
| "Did I miss a delivery?" | **Reconciliation sweep on Dharini's side** |

Polling alone is wasteful: a 40-minute Medaka run polled every 5 seconds is 480
requests for one bit of information. Webhooks alone have a real failure mode —
after `WEBHOOK_MAX_ATTEMPTS` the delivery becomes `ABANDONED`, the run succeeded,
and Dharini never finds out.

**Dharini implements a reconciliation sweep**: periodically ask "any run I
submitted more than N minutes ago with no terminal status?" and call
`GET /v1/runs/{id}`. Roughly 20 lines, and it makes the system self-healing.

The existing delivery layer already provides HMAC-SHA256 over `timestamp.body`,
an SSRF allowlist, backoff across ~9 hours, and `sweep_pending_deliveries` as a
lost-message backstop. Two additions: the payload echoes `external_id`, and the
receiver must reject an `X-Dharini-Timestamp` older than ~5 minutes — the
signature covers the timestamp, but replay protection only works if the receiver
enforces freshness.

---

## 12. Deployment

### CI, on merge to `main` touching any manifest or Dockerfile

```bash
python tools/images.py build --all
python tools/images.py push  --all
python tools/images.py lock            # resolves digests
git commit images.lock.json
```

### Production host

```bash
git pull
python tools/images.py pull            # digest-pinned, from the lock file
python tools/assets.py  fetch          # checksummed, into REFS_ROOT/<scope>/
docker compose up -d --build           # application image only
```

The host authenticates to Docker Hub for pulls. Anonymous pulls are rate-limited
to 100 per 6 hours per IP; with a lock file pulls are rare, but authenticating
removes the failure mode.

### Makefile

```
make new-pipeline id=<slug>
make images-build [PIPELINE=<id>]
make images-push
make images-lock
make images-pull
make assets
make fixtures        # tier-2 samples
make dev             # STORAGE_BACKEND=local, no AWS required
make test
make check           # manifest validation + contract tests + lint
```

---

## 13. Testing

`tests/contract.py` exposes `assert_pipeline_contract(pipeline)`, checking:

1. `id` is non-empty and kebab-case; `version` is set
2. `params_schema` is a valid Draft 2020-12 schema
3. `additionalProperties: False` at every object level
4. every non-object leaf property declares a `default`
5. top-level properties carry `title` and `description`
6. `stages` is non-empty, has no duplicates, and does not contain `fetch`
7. `manifest.yaml` exists, parses, and its `scope` equals `pipeline.id`
8. every image key resolved through `img()` is declared in that manifest or in
   `shared`
9. no image `version` is `latest`; **no `StageSpec` has `image=None`**
10. every asset declares `url`, `filename` and `sha256`
11. `input_filename` and every `extra_inputs[*].filename` contain no path
    separators
12. any `enum` of asset keys matches the declared assets
13. every occurrence of an organism accession across a manifest agrees

`tests/test_all_pipelines.py` parametrises it over `registry.all()`, so a new
pipeline is covered the moment it is registered, without writing a test file.

`tests/test_e2e.py` runs each pipeline end to end on its **tier-1 fixture**,
marked `@pytest.mark.e2e` and gated on Docker being available. This runs in CI on
every pull request.

### Database migration

Adding `external_id`, `input_key` and `extra_input_keys` to `Run` is the first
change that must preserve existing data, and `init_db()`'s `create_all` cannot
alter an existing table. This phase ships one hand-written migration executed
before the new code starts, backfilling the new columns as NULL for historical
runs and adding the unique index on `external_id`. Broader Alembic adoption
remains separate work.

---

## 14. Migration plan

Each phase is independently mergeable and leaves both pipelines working.

| Phase | Content | Behaviour change |
|---|---|---|
| **0** | `manifest.py`, `images.py`, `assets.py`, `tools/*`, contract test kit, `pkgutil` discovery, manifests written for both pipelines and `shared` | None |
| **1** | **Containerise every stage.** Build `core`, `snippy`, `snpeff-lsdv` on micromamba+bioconda; move `Dockerfile_lsdv_core_v4`; switch `specs.py` to `img()`; delete `_run_local`, `_conda_env`, `_local_peak_mb`, conda base image, `setup_snpeff_lsdv.sh`; app image becomes `python:3.13-slim`; generate lock file | Resource caps now actually apply; snpEff unified at 5.4 |
| **2** | `ctx.asset()`; `REFS_ROOT/<scope>/`; `tools/assets.py`; `reference` param becomes an enum; remove `REFERENCE_PATH` and `SNPEFF_DB` from config | References resolve by key |
| **3** | `ObjectStore`; `POST /v1/uploads`; `POST /v1/runs` JSON body; `external_id` + idempotency; `fetch` stage; preflight; `Run` columns and migration; S3 retention | Submission flow changes — coordinate with Dharini |
| **4** | `extra_inputs` replaces hardcoded `primer_bed`; `snippy_fasta/` becomes a package; `visualization.py` moves; `run_local.py`; tier-1 fixtures committed | None functional |
| **5** | Verification gates; delete `ONTpipeline/`; rewrite `readme.md` and `docs/PIPELINE_AUTHORING.md` | None |

Phase 1 removes more code than it adds.

---

## 15. Alternative considered: Nextflow / Snakemake

Nearly every serious bioinformatics platform uses a workflow engine, and nf-core
publishes hundreds of validated pipelines. Rejected here because it would mean
embedding an engine (JVM, Groovy DSL, its own config and executor model) inside
the worker and rebuilding the stage-progress and provenance integration that
already exists in Postgres by parsing trace files. At two pipelines on one box
with a Python team, the migration cost is real and the payoff — community
pipelines, resume, HPC executors — is not currently needed.

Revisit if the team wants to run **nf-core pipelines as-is**, moves to
**multi-node / AWS Batch**, or passes roughly **5–10 pipelines** where the
per-pipeline Python begins to repeat itself.

---

## 16. Risks and verification gates

Gates are blocking. None is a formality.

| ID | Risk | Gate |
|---|---|---|
| **G1** | Containerising Snippy could change `snippy-fasta` output — the pipeline whose results are already trusted. | Run the same FASTA before and after; `snps.vcf` and `annotated.vcf` must be identical. Snippy 4.6.0 and snpEff 5.4 are unchanged, so any diff means a packaging problem, not a version change. |
| **G2** | `ont-lsdv` has never run end to end inside the platform. | One full run on a real FASTQ with the `bcftools` caller before `ONTpipeline/` is deleted. |
| **G3** | `quay.io/biocontainers/bamclipper` is minimal; `bamclipper.sh` shells out to samtools and GNU parallel. | One primer-trimming run with a real BEDPE. **If it fails, fold BamClipper into `core`** — which already has samtools — rather than building a fourth self-built image. |
| **G4** | Medaka and Clair3 invocations were adapted but never verified (pre-existing readme gap 4). | One confirmation run each. |
| **G5** | `core` moves from Ubuntu apt packages to pinned bioconda versions — minimap2 and samtools jump several releases. | Acceptable for `ont-lsdv`, which is unverified anyway, but the G2 run is the confirmation. Record the versions in the run record via the image digest. |
| **G6** | Snippy in a container writes into `--outdir` and reads a read-only reference under `REFS_ROOT`. | Confirm path parity holds and Snippy needs no write access beside the reference. |
| **G7** | The bioconda pins in the Dockerfiles must actually resolve together. | `make images-build` in CI is the check; a solver conflict fails the build, not a run. |
| **G8** | `ONTpipeline/` deletion is irreversible — it is currently untracked. | Commit it to git first, delete in a later commit, so history retains it. |

---

## 17. Outcome

Adding pipeline #3 after this lands:

| Step | Files touched |
|---|---|
| Write the pipeline | `app/pipelines/<id>/**` — one new directory |
| Register it | none — `pkgutil` scan |
| Expose it to Dharini | none — `GET /v1/pipelines` reads the registry |
| API route | none |
| Database | none |
| CI image build | none — CI walks the manifests |
| Deploy | none — the lock file regenerates |

`make new-pipeline id=<slug>` scaffolds the directory with a manifest, a
`Pipeline` stub, a tier-1 fixture placeholder, and a contract test that passes
immediately.
