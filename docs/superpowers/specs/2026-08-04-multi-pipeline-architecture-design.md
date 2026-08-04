# Multi-Pipeline Architecture

**Date:** 2026-08-04
**Status:** Approved for planning

Restructure the Dharini pipeline service so a new analysis pipeline is one
self-contained directory, its container images are declared rather than
scattered, and large inputs arrive through S3 instead of through the API.

---

## 1. Problem

The platform/pipeline boundary is already well drawn: `app/tasks.py` never
branches on pipeline identity, `POST /v1/runs` is one generic route, and the
`Pipeline` + `RunContext` contract keeps tool execution funnelled through a
single `StageRunner`. That part works and is preserved.

Four things break when a third pipeline is added.

**Images are declared in three unsynchronised places.** `ont_lsdv/specs.py`
holds six `os.getenv("ONT_IMAGE_*", ...)` calls; `.env.example` repeats them;
`readme.md` repeats them again in a hand-written `docker pull` loop. `ALL_IMAGES`
is defined and read by nothing. Three of the six images (`suhas0/*`) have no
Dockerfile in the repository and cannot be rebuilt or audited. Every tag is
mutable, so two runs a month apart may use different tool versions.
`Dockerfile_lsdv_core_v4` sits at the repository root, untracked, wired to
nothing. There is no build step for tool images at all.

**The API has a pipeline-specific leak.** `primer_bed` is hardcoded in the
`POST /v1/runs` signature. A pipeline needing a different secondary file
requires an API change.

**Reference data shares one flat namespace.** `REFS_ROOT` is a single directory
and `prepare_reference()` reads a bare filename out of it, so two pipelines both
wanting `reference.fasta` collide silently. `REFERENCE_PATH` and `SNPEFF_DB` are
global environment variables serving one pipeline each.

**Large inputs go through the API process.** A 1 GB FASTQ streamed through
uvicorn holds a connection for minutes, consumes local disk, and makes
`MAX_UPLOAD_SIZE=5GB` a timeout problem rather than a limit.

Two secondary problems are folded in because they touch the same files:
`ONTpipeline/` duplicates `app/pipelines/ont_lsdv/` (the same science in two
places, guaranteed to drift), and `app/visualization.py` is 493 lines of
snippy-specific code sitting in the shared application layer.

---

## 2. Goals

- Adding a pipeline touches exactly one new directory and no existing file.
- Container images are declared once, built by CI, and pinned by digest at
  runtime without anyone remembering to do it.
- An image usable by more than one pipeline is declared once and shared.
- Reference data and tool databases are declared, checksummed, and namespaced
  per pipeline.
- Inputs of any size are uploaded directly to S3; the service never handles the
  bytes.
- A pipeline can be authored, tested, and reviewed as a normal pull request.

### Non-goals

- A declarative workflow/DAG engine. Stage logic stays in Python; per-pipeline
  logic genuinely differs and YAML would fight it.
- Multi-host scheduling, GPU support, or autoscaling.
- S3 multipart upload. Single-part presigned PUT caps at 5 GB, which matches the
  existing `MAX_UPLOAD_BYTES`. Multipart is deferred until a real input exceeds
  it.
- Full Alembic adoption. One hand-written migration is required (§11); broader
  migration tooling is separate work.

---

## 3. Decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | YAML declares **images and assets only**; all execution logic, stage order and resource limits stay in Python | A `StageSpec` is only meaningful beside the argv it constrains. Deploy tooling needs to read images without importing FastAPI/Celery/SQLAlchemy. |
| D2 | Images are **built and pushed by CI**; the production host only pulls | Fast, reproducible deploys. Builds do not compete with running jobs for CPU. Staging and production run byte-identical images. |
| D3 | Images are published to **public Docker Hub** under `artpark/` | Simple pulls, no registry credentials on the host for reads. All content is open-source tooling. |
| D4 | Assets are **declared in the manifest, fetched host-side, namespaced** per scope | References stay swappable, checksums are verifiable, and no two pipelines can collide on a filename. |
| D5 | Pipelines are **authored directly against the `Pipeline` contract** | One source of truth. No port step, no drift. `ONTpipeline/` is deleted once `ont-lsdv` passes an end-to-end run. |
| D6 | Clients **PUT to a presigned S3 URL**; the service issues the URL and later fetches the object | Bytes never touch our host. No timeouts, no disk pressure, no worker occupied by a transfer. |
| D7 | **One generic route for every pipeline** — no per-pipeline routes | A route per pipeline reintroduces exactly the coupling being removed. `pipeline_id` already selects the implementation. |
| D8 | BamClipper is pulled from **`quay.io/biocontainers/bamclipper`** | Published upstream; no Dockerfile to maintain or reconstruct. |
| D9 | snpEff is **built from a parameterised Dockerfile**, organism supplied as a build arg | Upstream snpEff carries no LSDV database. Parameterising means pathogen #2 is a manifest entry, not a second Dockerfile. |
| D10 | **Both pipelines use the same shared snpEff image** | They currently annotate with different snpEff versions (conda 5.4 vs whatever is inside `suhas0/snpeff_lsdv:v1`), so the same variant can be annotated differently depending on which pipeline was used. |

---

## 4. Repository layout

```
shared/
├── manifest.yaml                  # cross-pipeline images and assets
└── docker/
    └── snpeff/Dockerfile

app/
├── config.py                      # platform config only
├── main.py  db.py  models.py  schemas.py  worker.py  tasks.py
├── vcf.py  delivery.py
├── api/
│   ├── uploads.py                 # NEW  presigned PUT issuance
│   ├── runs.py                    # generic; extra_inputs-driven
│   ├── pipelines.py
│   └── legacy.py
├── runtime/
│   ├── spec.py                    # StageSpec, stage errors
│   ├── runner.py                  # the only place a process is spawned
│   ├── context.py                 # RunContext (+ ctx.asset)
│   ├── manifest.py                # NEW  manifest.yaml parsing + validation
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
    └── ont_lsdv/
        ├── manifest.yaml
        ├── pipeline.py  specs.py  common.py
        ├── stages/{qc,align,primer,variant,consensus,annotate,report}.py
        ├── docker/core/Dockerfile
        └── tests/

tools/                             # stdlib + PyYAML only; no app imports
├── images.py                      # build | push | lock | pull
├── assets.py                      # fetch | verify
└── scaffold.py                    # make new-pipeline

images.lock.json                   # committed; CI-generated
Makefile
.github/workflows/images.yml
```

---

## 5. Manifests

Two scopes. A pipeline manifest lives beside its code and is owned by that
pipeline. The shared manifest holds anything more than one pipeline uses.

### 5.1 Shared manifest — `shared/manifest.yaml`

```yaml
schema_version: 1
scope: shared

vars:
  accession: NC_003027
  db_name: LSDV

images:
  snpeff-lsdv:
    source: build
    dockerfile: docker/snpeff/Dockerfile
    context: docker/snpeff
    repository: artpark/dharini-snpeff
    version: "5.2f-lsdv-1"
    platform: linux/amd64
    build_args:
      ACCESSION: "${vars.accession}"
      DB_NAME: "${vars.db_name}"

assets:
  reference:
    url: "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=nuccore&id=${vars.accession}&rettype=fasta&retmode=text"
    filename: lsdv_reference.fasta
    sha256: ""
  annotation:
    url: "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=nuccore&id=${vars.accession}&rettype=gff3&retmode=text"
    filename: lsdv_reference.gff3
    sha256: ""
```

An empty `sha256` means "not yet pinned". `tools/assets.py fetch --write-checksums`
downloads, computes, and writes the digests back into the manifest; the value is
committed and thereafter verified on every fetch. A non-empty checksum that does
not match is a hard failure. The contract test rejects an empty `sha256` outside
of local development, so an unpinned asset cannot reach production.

**The LSDV reference genome and the snpEff database are both shared**, because
both pipelines use both. This is not merely convenient — it is the mechanism
that keeps them consistent. The alignment reference and the annotation database
must derive from the same accession or variants are annotated against the wrong
coordinates, and nothing errors. `vars.accession` is declared once and
interpolated into both the asset URLs and the image build args, so the two
cannot diverge. Today this is guaranteed only because `setup_snpeff_lsdv.sh`
happens to write both from one shell variable.

### 5.2 Pipeline manifest — `app/pipelines/ont_lsdv/manifest.yaml`

```yaml
schema_version: 1
scope: ont-lsdv                    # must equal Pipeline.id

images:
  core:
    source: build
    dockerfile: docker/core/Dockerfile
    context: docker/core
    repository: artpark/dharini-ont-core
    version: "1.0.0"
    platform: linux/amd64

  medaka:
    source: registry
    repository: ontresearch/medaka
    version: "2.0.1"

  clair3:
    source: registry
    repository: hkubal/clair3
    version: "v1.0.10"

  longshot:
    source: registry
    repository: staphb/longshot
    version: "0.4.5"

  bamclipper:
    source: registry
    repository: quay.io/biocontainers/bamclipper
    version: "1.0.0--hdfd78af_2"
```

`ont-lsdv` declares no assets of its own; it consumes `shared:reference`.

### 5.3 Field reference

| Field | Applies to | Meaning |
|---|---|---|
| `schema_version` | manifest | Integer. Currently `1`. |
| `scope` | manifest | `shared`, or a pipeline id that must equal `Pipeline.id`. |
| `vars` | manifest | Flat string map, interpolated as `${vars.<name>}` in `url` and `build_args` values only. |
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

### 5.4 The snpEff Dockerfile

Derived from `setup_snpeff_lsdv.sh`, which is deleted once this lands.

```dockerfile
# shared/docker/snpeff/Dockerfile
FROM staphb/snpeff:5.2f

ARG ACCESSION=NC_003027
ARG DB_NAME=LSDV
ARG SNPEFF_HOME=/snpEff

RUN set -eux; \
    d="${SNPEFF_HOME}/data/${DB_NAME}"; mkdir -p "$d"; \
    base="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=nuccore&id=${ACCESSION}"; \
    curl -fsSL --retry 3 --retry-delay 5 "${base}&rettype=fasta&retmode=text" -o "$d/sequences.fa"; \
    curl -fsSL --retry 3 --retry-delay 5 "${base}&rettype=gff3&retmode=text"  -o "$d/genes.gff"; \
    test -s "$d/sequences.fa"; \
    grep -q '##gff-version 3' "$d/genes.gff"; \
    echo "${DB_NAME}.genome : ${DB_NAME}" >> "${SNPEFF_HOME}/snpEff.config"; \
    snpEff build -gff3 -noCheckCds -noCheckProtein -v "${DB_NAME}"; \
    test -s "$d/snpEffectPredictor.bin"
```

Two deliberate differences from the shell script:

- **`test -s snpEffectPredictor.bin`.** The script's retry loop treats
  `snpEff build` exiting 0 as success, but it can exit 0 having produced no
  usable database — discovered only during a real annotation. Asserting on the
  artifact at build time is the same rule `ctx.require()` enforces at runtime.
- **NCBI is contacted once at image build, not on every application image
  build.** Today an NCBI outage blocks the app image entirely.

`SNPEFF_HOME` is assumed to be `/snpEff` in the `staphb/snpeff:5.2f` base. This
is verified on first build (§12, R1).

---

## 6. Image resolution

Pipelines reference images by **logical key**, never by string.

```python
# app/pipelines/ont_lsdv/specs.py
from ...runtime.images import images_for

img = images_for(__package__)          # bound to this pipeline's manifest

ALIGN  = StageSpec(image=img("core"),             cpus=4, memory_gb=6, timeout_s=2 * HOUR)
MEDAKA = StageSpec(image=img("medaka"),           cpus=4, memory_gb=8, timeout_s=4 * HOUR)
SNPEFF = StageSpec(image=img("shared:snpeff-lsdv"), cpus=2, memory_gb=4, timeout_s=HOUR)
```

A bare key resolves within the pipeline's own manifest. A `shared:` prefix
resolves against `shared/manifest.yaml`. There is no implicit fallback between
the two — where an image comes from is always explicit.

Resolution order:

1. **Environment override** — `DHARINI_IMAGE__<SCOPE>__<KEY>`, where scope and
   key are uppercased with `-` replaced by `_`. `shared:snpeff-lsdv` becomes
   `DHARINI_IMAGE__SHARED__SNPEFF_LSDV`; `ont-lsdv`'s `core` becomes
   `DHARINI_IMAGE__ONT_LSDV__CORE`. Break-glass for an operator, requires no
   code change.
2. **`images.lock.json`** — the digest-pinned reference. The normal production
   path.
3. **`repository:version` from the manifest** — unpinned. Local development
   only.

When `REQUIRE_PINNED_IMAGES=true`, a key that resolves to anything without an
`@sha256:` raises at import, so the worker refuses to start rather than
producing an untraceable result. This closes readme "Known gaps" 1 and 2
structurally rather than by note.

`images_for()` records every ref it resolves per package, which gives preflight
(§9.3) the exact set of images a pipeline uses.

### 6.1 `images.lock.json`

Generated by CI, committed to git. Git provides the timestamp and author, so the
file carries no generation metadata and stays diff-clean.

```json
{
  "schema_version": 1,
  "images": {
    "shared:snpeff-lsdv": {
      "repository": "artpark/dharini-snpeff",
      "version": "5.2f-lsdv-1",
      "digest": "sha256:…",
      "ref": "artpark/dharini-snpeff@sha256:…"
    },
    "ont-lsdv:core":       { "…": "…" },
    "ont-lsdv:bamclipper": { "…": "…" }
  }
}
```

Digest pinning stops being a discipline someone has to remember and becomes a
property of the deploy.

---

## 7. Assets

Assets resolve to `REFS_ROOT/<scope>/<filename>`:

```
/srv/dharini/references/
├── shared/
│   ├── lsdv_reference.fasta
│   └── lsdv_reference.gff3
└── ont-lsdv/            # only if ont-lsdv declares assets of its own
```

```python
reference = ctx.asset("shared:reference")     # absolute Path
```

`ctx.asset()` raises a clear, actionable error if the file is absent. It
replaces the client-supplied-filename plus path-traversal check in
`ont_lsdv/common.py:29-33`: the parameter is now a manifest key, not a path, so
traversal is not expressible.

`tools/assets.py fetch` downloads every declared asset, verifies its `sha256`,
and skips anything already present and matching. `tools/assets.py verify`
checks without downloading, for use in preflight and CI.

The `reference` parameter in `ont_lsdv`'s `params_schema` changes from a free
filename to an `enum` of asset keys visible to that pipeline — today
`["shared:reference"]`, with `default: "shared:reference"`. A single-entry enum
looks redundant but is the point: Dharini renders a dropdown that grows by
itself when a second reference is declared, and an unknown reference is a 422 at
submission rather than a failure at stage 1. A contract test asserts the enum
matches the declared asset keys, so the two cannot drift.

---

## 8. Object storage and the upload flow

### 8.1 Interface

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
`STORAGE_BACKEND=s3|local`. `LocalStore` presigns to a token-guarded
`PUT /v1/uploads/local/{key}` endpoint that is registered only when the local
backend is active, so `make dev` needs no AWS account and there is still only
one code path through the application.

Credentials come from the instance IAM role. No access keys in `.env`.

### 8.2 Flow

```
POST /v1/uploads  {pipeline_id, filename, input: "primary" | "<extra_key>"}
  201 {object_key, upload_url, expires_at, max_bytes}

  client ── PUT (up to 5 GB) ──────────────────────────────────► S3

POST /v1/runs     {pipeline_id, input_key, extra_input_keys, params, callback_url}
  202 {run_id, pipeline_id, pipeline_version, status, stages, queue_position}
```

`POST /v1/runs` becomes a JSON body rather than multipart. `extra_input_keys`
maps an `extra_inputs` key to an object key —
`{"primer_bed": "uploads/9f3c…/primers.bedpe"}` — and is omitted when the
pipeline declares none.

**The server generates `object_key`; the client never supplies it.** Format is
`uploads/<uuid4>/<pipeline.input_filename>`. The client's filename is validated
for extension and retained as run metadata only, consistent with the existing
principle that client filenames are hostile input and never reach a tool command
line or a storage path.

Validation at submission, before anything is queued:

- pipeline exists; extension matches `accepted_extensions` (primary) or
  `extra_inputs[key].extensions`
- `HeadObject` confirms the object exists and is within `MAX_UPLOAD_BYTES`
- params validate against `params_schema`
- every `required` extra input is present

---

## 9. Platform changes

### 9.1 Pipeline contract

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

### 9.2 Registration by discovery

`app/pipelines/__init__.py` replaces its hand-maintained import list with a
`pkgutil.iter_modules` scan of the package, skipping names beginning with `_`.
Adding a pipeline no longer edits any existing file.

### 9.3 The `fetch` stage and preflight

The platform prepends `fetch` to every pipeline's declared stages, so progress
reads `1/9 · fetch` and a 90-second download is visible rather than looking like
a hung run. `fetch` is a reserved stage name, rejected by the contract test if a
pipeline declares it. The prepended list is what `RunContext.declared_stages`
receives and what `POST /v1/runs` and `GET /v1/runs/{id}` report, so the
denominator a client sees always matches the stages that will actually run.

Preflight runs as the first action inside `fetch`:

- every image the pipeline resolved is present (`docker image inspect`)
- every asset it declares exists on disk

A missing image fails the run in under a second with
`image artpark/dharini-ont-core@sha256:… not present on this host; run 'make images-pull'`,
rather than dying at stage 5 with a confusing Docker error. `PreflightFailed`
subclasses `StageError`, so it flows through the existing failure, recording and
webhook paths unchanged.

### 9.4 Retention

`Run` gains `input_key` and `extra_input_keys`, replacing the current practice
of stashing `_input_name` and `_extra_inputs` inside the `params` JSON column
and stripping reserved keys later. `cleanup_expired_runs` deletes the S3 objects
alongside the local run directory; otherwise the bucket grows forever while the
local disk stays tidy.

---

## 10. Deployment

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
to 100 per 6 hours per IP; with a lock file pulls are rare, but an authenticated
host removes the failure mode entirely.

### Makefile

```
make new-pipeline id=<slug>
make images-build [PIPELINE=<id>]
make images-push
make images-lock
make images-pull
make assets
make dev            # STORAGE_BACKEND=local, no AWS required
make test
make check          # manifest validation + contract tests + lint
```

---

## 11. Testing

`tests/contract.py` exposes `assert_pipeline_contract(pipeline)`, checking:

1. `id` is non-empty and kebab-case; `version` is set
2. `params_schema` is a valid Draft 2020-12 schema
3. `additionalProperties: False` at every object level
4. every non-object leaf property declares a `default`
5. top-level properties carry `title` and `description`
6. `stages` is non-empty, has no duplicates, and does not contain `fetch`
7. `manifest.yaml` exists, parses, and its `scope` equals `pipeline.id`
8. every image key resolved through `img()` is declared in that pipeline's
   manifest or in `shared`
9. no image `version` is `latest`
10. every asset declares `url`, `filename` and `sha256`
11. `input_filename` and every `extra_inputs[*].filename` contain no path
    separators
12. any `enum` of asset keys matches the declared assets

`tests/test_all_pipelines.py` parametrises it over `registry.all()`, so a new
pipeline is covered the moment it is registered, without writing a test file.

### Database migration

Adding `input_key` and `extra_input_keys` to `Run` is the first change that must
preserve existing data, and `init_db()`'s `create_all` cannot alter an existing
table. This phase ships one hand-written migration executed before the new code
starts, backfilling `input_key` as NULL for historical runs (whose artifacts are
past retention anyway). Broader Alembic adoption remains separate work.

---

## 12. Migration plan

Each phase is independently mergeable and leaves both pipelines working.

| Phase | Content | Behaviour change |
|---|---|---|
| **0** | `manifest.py`, `images.py`, `assets.py`, `tools/*`, contract test kit, `pkgutil` discovery | None |
| **1** | Manifests for both pipelines and `shared`; build the snpEff image; move `Dockerfile_lsdv_core_v4` to `ont_lsdv/docker/core/`; switch `specs.py` to `img()`; generate the lock file | Images resolve through the lock; annotation moves to the shared snpEff image |
| **2** | `ctx.asset()`; `REFS_ROOT/<scope>/` namespacing; `tools/assets.py`; remove `snpeff`, `openjdk` and `setup_snpeff_lsdv.sh` from the application image | References resolve by key |
| **3** | `ObjectStore`; `POST /v1/uploads`; `POST /v1/runs` JSON body; `fetch` stage; preflight; `Run` columns and migration; S3 retention | Upload flow changes — coordinate with Dharini |
| **4** | `ont_lsdv/stages/`; `snippy_fasta/` becomes a package; `visualization.py` moves; `extra_inputs` replaces hardcoded `primer_bed` | None functional |
| **5** | Verification gates; delete `ONTpipeline/`; rewrite `readme.md` deploy section and `docs/PIPELINE_AUTHORING.md` | None |

---

## 13. Risks and verification gates

Gates are blocking. None is a formality.

| ID | Risk | Gate |
|---|---|---|
| **G1** | snpEff 5.4 → 5.2f is a **downgrade** for `snippy-fasta`, a pipeline whose output is already trusted. Annotation strings may change. | Run both versions on a known sample (`PP488397`), diff `annotated.vcf`, and sign off — or rebuild the shared image from a 5.4 base instead. |
| **G2** | `ont-lsdv` has never run end to end inside the platform. | One full run on a real FASTQ with the `bcftools` caller before `ONTpipeline/` is deleted. |
| **G3** | `quay.io/biocontainers/bamclipper` is minimal; `bamclipper.sh` shells out to samtools and GNU parallel. | One primer-trimming run with a real BEDPE. `ctx.require()` on the clipped BAM catches a soft failure. |
| **G4** | Medaka and Clair3 invocations were adapted but never verified (pre-existing readme gap 4). | One confirmation run each. |
| **G5** | `staphb/snpeff:5.2f` internal paths (`/snpEff`, its data directory) are assumed. | Confirmed on the first image build; the `test -s` assertions fail the build if wrong. |
| **G6** | Removing `snpeff` and `openjdk` from the conda environment assumes Snippy 4.6 does not require snpEff on `PATH` when given a plain FASTA reference. | Confirm with one `snippy-fasta` run before the Dockerfile change merges. |
| **G7** | `ONTpipeline/` deletion is irreversible in the working tree (it is currently untracked). | Commit it to git first, delete in a later commit, so history retains it. |

---

## 14. Outcome

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
`Pipeline` stub, and a contract test that passes immediately.
