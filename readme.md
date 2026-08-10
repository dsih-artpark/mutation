# 🧬 Dharini Pipeline Service

Runs bioinformatics pipelines asynchronously and **notifies Dharini by webhook**
when they finish. Built so that adding a new pipeline is a backend-only change —
no API, database or frontend work.

| Pipeline | Input | Tools |
| --- | --- | --- |
| `snippy-fasta` | assembled FASTA | Snippy → snpEff → interactive plot |
| `ont-lsdv` | Oxford Nanopore FASTQ | NanoPlot/fastp → minimap2 → ARTIC align_trim → bcftools/Medaka/Clair3 → consensus → snpEff |

---

## Architecture

Two planes, deliberately separated.

```
Dharini (NestJS)
      │  POST /v1/runs   { pipeline_id, params, callback_url }
      │  ◄── 202 { run_id }
      ▼
┌─────────────────────────────────────────────────┐
│ CONTROL PLANE — FastAPI                         │
│  validates params against the pipeline's schema │
│  persists the run ──────────────► Postgres      │
│  enqueues ──────────────────────► Redis/Celery  │
└─────────────────────────────────────────────────┘
      ▼
┌─────────────────────────────────────────────────┐
│ EXECUTION PLANE — Celery worker                 │
│  PipelineRegistry.get(id).run(ctx)              │
│         └─► ContainerRunner ─► docker run       │
│                (argv, no shell, path parity)    │
│         └─► ctx.stage("align") ─► Postgres      │
└─────────────────────────────────────────────────┘
      ▼
  $RUNS_ROOT/<run_id>/{input,work,output,logs}
      ▼
  webhook ──► Dharini      (push: low latency)
  GET /v1/runs/{id} ◄────── Dharini  (pull: certainty)
```

**Key components**

- **`app/pipelines/`** — one package per pipeline, discovered at startup and
  registered by id. The only place that changes when a pipeline is added.
- **`app/runtime/runner.py`** — the *only* code in the repo that starts a
  process. Every safety property is enforced here, once, for all pipelines.
- **`app/runtime/context.py`** — what a pipeline author programs against.
- **`app/delivery.py` + `app/tasks.py`** — signed webhooks with retry, and the
  recovery paths for when delivery fails.

### Why tools run in separate containers

Medaka, Clair3 and ARTIC align_trim have irreconcilable Python,
PyTorch/TensorFlow and htslib requirements — no single conda environment holds
all three. Each tool therefore runs in its own image.

They never communicate. Each is **ephemeral**: it starts, runs one command,
writes a file, and exits. The next tool reads that file. The exchange medium is
the shared run directory, not the network — `docker run` behaves as a function
call, not a service.

### ⚠ Path parity

The worker starts sibling containers through the host Docker daemon, and the
daemon resolves bind-mount sources **on the host** — never inside the calling
container. `$RUNS_ROOT` is therefore mounted at the *same absolute path* on both
sides.

Get this wrong and the failure is silent: Docker creates an empty directory at
the missing host path, mounts that, and the tool reports "file not found"
against data that plainly exists.

---

## Quick start (production — single host)

```bash
sudo mkdir -p /srv/dharini/runs /srv/dharini/references
sudo chown -R $USER /srv/dharini
cp data/lsdv_reference_sequence.fasta /srv/dharini/references/

cp .env.example .env
# Set RUNS_ROOT, REFS_ROOT, WEBHOOK_SECRET, WEBHOOK_ALLOWED_HOSTS,
# POSTGRES_PASSWORD, PUBLIC_BASE_URL, and:
#   DOCKER_GID=$(getent group docker | cut -d: -f3)

docker compose up -d --build

# Pre-pull the tool images so the first real run isn't a long download.
# Reads images.lock.json and pulls every image at its locked digest.
make images-pull

curl localhost:8000/health
```

`make images-pull` fetches roughly **9 GB** across six images. It is optional —
anything missing is pulled on demand by the first run that needs it — but doing
it up front means the first submission does not appear to hang for ten minutes.

Set `REQUIRE_PINNED_IMAGES=true` in production. Every image in
`images.lock.json` resolves to a `@sha256:` digest, and with this set the
service refuses to start if any image would resolve to a mutable tag instead.

---

## API

Interactive docs at `/docs`.

### Discover pipelines

```bash
curl localhost:8000/v1/pipelines
```

Returns each pipeline's `stages` and its `params_schema` (JSON Schema).
**Dharini renders its parameter form from this** — which is why a new pipeline
needs no frontend change.

### Submit a run

```bash
curl -X POST localhost:8000/v1/runs \
  -F pipeline_id=ont-lsdv \
  -F file=@reads.fastq.gz \
  -F 'params={"variant":{"tool":"clair3"},"threads":4}' \
  -F callback_url=https://dharini.example.org/api/snp/webhook
```

```json
{ "run_id": "…", "status": "QUEUED", "stages": ["prepare","qc","align",…],
  "queue_position": 0 }
```

Params are validated **before** the job is enqueued, so a bad value is a `422`
in milliseconds rather than a failure eight minutes into a run.

### Check status

```bash
curl localhost:8000/v1/runs/<run_id>
```

Includes `progress` (`"4/8 · consensus"`), per-stage timings and peak memory,
artifact download URLs, metrics, and webhook delivery state.

### Other endpoints

| Endpoint | Purpose |
| --- | --- |
| `GET /v1/runs?pipeline_id=&status=&limit=` | list runs |
| `GET /v1/runs/{id}/artifacts/{filename}` | download a result file |
| `POST /v1/runs/{id}/redeliver` | re-send the webhook manually |
| `GET /health` | liveness + free disk |

`POST /upload`, `GET /status/{job_id}`, `GET /download/...` and
`GET /visualization/{run_id}` still work unchanged, so Dharini can migrate to
`/v1` on its own schedule.

---

## Webhook contract

Sent when a run reaches a terminal state.

```
POST <callback_url>
X-Dharini-Event:     run.completed | run.failed
X-Dharini-Delivery:  <uuid>          ← idempotency key
X-Dharini-Timestamp: <unix seconds>
X-Dharini-Signature: sha256=<hmac>
```

```jsonc
{
  "event": "run.completed",
  "delivery_id": "…", "attempt": 1,
  "run": { "id": "…", "pipeline_id": "ont-lsdv", "pipeline_version": "1.1.0",
           "status": "SUCCEEDED", "duration_s": 412.7, "params": {…},
           "error": null },
  "metrics": { "variant_count": 37, "snp_count": 31, "by_impact": {"HIGH": 2},
               "reads_mapped_percent": 94.2,
               "consensus_completeness_percent": 98.1 },
  "stages":  [ { "name": "align", "status": "SUCCEEDED",
                 "duration_s": 84.2, "peak_memory_mb": 2140 } ],
  "variants": [ { "chrom": "LSDV", "pos": 1042, "ref": "A", "alt": "G",
                  "effect": "missense_variant", "impact": "MODERATE",
                  "gene": "LSDV008", "hgvs_p": "p.Lys34Glu",
                  "depth": 120, "allele_freq": 0.98 } ],
  "variants_truncated": false, "variants_total": 37,
  "artifacts": [ { "kind": "vcf", "label": "Annotated variants",
                   "filename": "annotated.vcf", "size_bytes": 48210,
                   "url": "https://…/v1/runs/…/artifacts/annotated.vcf" } ]
}
```

Parsed variants are inline so you can store queryable rows immediately; bulk
files stay behind URLs so payload size doesn't scale with variant count.

### Receiving it (NestJS)

```ts
import { createHmac, timingSafeEqual } from 'crypto';

@Post('webhook')
async handle(@Req() req: RawBodyRequest<Request>, @Headers() headers) {
  const signature = headers['x-dharini-signature'];
  const timestamp = headers['x-dharini-timestamp'];

  // Reject replays.
  if (Math.abs(Date.now() / 1000 - Number(timestamp)) > 300) {
    throw new UnauthorizedException('stale signature');
  }

  const expected = 'sha256=' + createHmac('sha256', process.env.DHARINI_WEBHOOK_SECRET)
    .update(`${timestamp}.`).update(req.rawBody)      // ← raw body, not re-serialised
    .digest('hex');

  if (!timingSafeEqual(Buffer.from(signature), Buffer.from(expected))) {
    throw new UnauthorizedException();
  }

  // Idempotent: retries reuse the same delivery id.
  await this.snp.upsertResult(headers['x-dharini-delivery'], req.body);
  return { received: true };   // any 2xx stops the retries
}
```

Three things that matter:

1. **Verify against the raw body.** Re-serialising the parsed JSON changes the
   bytes and the signature will not match — enable `rawBody: true` in
   `NestFactory.create`.
2. **Be idempotent on `X-Dharini-Delivery`.** A timeout after you've committed
   still triggers a retry.
3. **Return 2xx quickly.** Queue the heavy work; a slow handler burns the
   15-second delivery timeout and forces a pointless retry.

### When delivery fails

Retries run at **10s → 1m → 5m → 30m → 2h → 6h** (~9 hours total). After that
the delivery is `ABANDONED` — but **the run stays `SUCCEEDED`**. The science was
valid; only the notification failed. Recover with:

- `GET /v1/runs?status=SUCCEEDED` — reconcile anything missed
- `POST /v1/runs/{id}/redeliver` — re-send one

Artifacts are **never** purged by retention while a delivery is still
outstanding.

---

## Operations

### Sizing

The `run_stages` table records real peak memory and wall time for every stage.
After ~20 runs, stop guessing:

```sql
SELECT name,
       count(*)                    AS runs,
       round(avg(duration_s))      AS avg_seconds,
       round(max(peak_memory_mb))  AS peak_mb
FROM run_stages
WHERE status = 'SUCCEEDED'
GROUP BY name
ORDER BY peak_mb DESC;
```

Peak memory across concurrent runs is roughly `WORKER_CONCURRENCY × heaviest
stage`. Start at `WORKER_CONCURRENCY=1` — two concurrent Medaka or Clair3
stages contend for the same cores and both finish slower than they would back
to back.

⚠ **Burstable hosts (Lightsail, EC2 T-family).** Genomics pins CPU for minutes
at a time. Watch the CPU burst-capacity metric; if it trends to zero you're
being throttled and want a non-burstable compute instance.

### Storage

`work/` is deleted as soon as a run ends — only files a pipeline explicitly
publishes survive. Artifacts are swept after `RETENTION_HOURS` (default 72);
**run records are kept indefinitely**, since the history is the point of a
surveillance platform.

### Debugging a failed run

```bash
curl localhost:8000/v1/runs/<run_id> | jq '.error_stage, .error_message'
ls $RUNS_ROOT/<run_id>/logs/          # full stdout+stderr per stage
```

### Callback URL safety

A callback URL is client-supplied and the worker sits inside your network, so
two independent controls apply — **both always, neither is a bypass for the
other**:

1. the hostname must match `WEBHOOK_ALLOWED_HOSTS`, when configured;
2. every address it resolves to must be acceptable.

Loopback, link-local (the cloud metadata endpoint at `169.254.169.254`),
multicast, reserved and unspecified addresses are refused unconditionally.
Private RFC1918 ranges are refused unless `WEBHOOK_ALLOW_PRIVATE_IPS=true`.

⚠ **If Dharini sits on a private address inside the same network you will need
that opt-in.** Set `WEBHOOK_ALLOWED_HOSTS` alongside it, never alone — the
allowlist becomes your only remaining control.

The address check re-runs immediately before every delivery attempt, not just
at submission, so a DNS record that changes during the ~9-hour retry window is
still caught.

---

## Local development

**Docker is the only prerequisite.** No virtualenv, no `pip install`, no conda —
not for the bioinformatics tools, which have always run in their own containers,
and not for the application either. Every `make` target runs inside the `dev`
stage of this repository's own `Dockerfile`. If a command here asks you to
install something on your machine, that is a bug in the command.

### First time

```bash
git clone <this repo> && cd sequencing

make images-pull          # ~9 GB, once. All six tool images at their locked digest.
make check                # contracts and manifests          (~1s)
make test                 # the whole suite, in a container  (~7s)
```

**No `.env` is needed for local work.** The `dev` container deliberately does
not read one: the test suite points itself at SQLite in a temporary directory,
and `make smoke` sets `RUNS_ROOT` and `REFS_ROOT` to paths inside the checkout.
`.env.example` is for deploying to a server — it points `RUNS_ROOT` at
`/srv/dharini`, which does not exist on your machine. Copy it when you deploy,
not when you clone.

The custom images are already published to
[`dsihartpark`](https://hub.docker.com/u/dsihartpark) on Docker Hub, so
`make images-pull` is a download — you do **not** need to build them:

| Image | Contents |
| --- | --- |
| `dsihartpark/dharini-ont-core` | minimap2, samtools, bcftools, fastp, NanoFilt, NanoPlot |
| `dsihartpark/dharini-align-trim` | ARTIC `align_trim` + samtools |
| `dsihartpark/dharini-snpeff` | snpEff with the LSDV database prebuilt |
| `dsihartpark/dharini-snippy` | Snippy and its full toolchain |

Medaka and Clair3 are pulled unmodified from upstream registries.

`make help` lists every target.

### Run a pipeline on your own file

This is the loop to live in. `tools/run_local.py` builds the **same**
`RunContext` the Celery worker builds, applies the **same** schema validation
and defaults the API applies, and executes the **same** stages in the **same**
tool containers. It skips only Postgres, Redis, the queue and HTTP — the parts
you are not debugging. A green run here means the pipeline works, not that a
test harness is happy.

**First, copy your data into the checkout.** The dev container mounts this
directory and nothing else, so a path like `~/data/reads.fastq.gz` does not
exist inside it. `fixtures/` is git-ignored and exists for exactly this:

```bash
mkdir -p fixtures
cp ~/wherever/reads.fastq.gz fixtures/
cp ~/wherever/scheme.bed     fixtures/
```

Then run it. Paths are relative to the repository root:

```bash
# ONT FASTQ, default settings (Clair3)
make smoke INPUT=fixtures/reads.fastq.gz

# Pick a caller, turn on primer trimming, supply a scheme
make smoke INPUT=fixtures/reads.fastq.gz \
  ARGS="--param variant.tool=bcftools \
        --param primer.enabled=true \
        --extra primer_bed=fixtures/scheme.bed \
        --keep"

# The other pipeline
make smoke PIPELINE=snippy-fasta INPUT=fixtures/your-assembly.fasta
```

`--param` takes dotted keys and JSON values, so `--param threads=8` is a number
and `--param primer.enabled=true` is a boolean. `--extra key=path` supplies a
secondary input. `--keep` retains `work/`, which the worker normally purges.
`--run-id NAME` gives the run a name you choose instead of a timestamp.

Every parameter either pipeline accepts, with its default and its meaning:

```bash
curl -s localhost:8000/v1/pipelines | jq '.[] | {id, params_schema}'
```

Output goes to `runs/<run-id>/`:

```text
runs/<run-id>/input/     what you supplied, under a normalised filename
runs/<run-id>/work/      scratch — deleted unless you passed --keep
runs/<run-id>/output/    published artifacts: VCFs, consensus, QC report
runs/<run-id>/logs/      one log per stage
```

### Don't have real data yet?

```bash
make fixtures
```

Synthesises reads, an ARTIC primer scheme and an assembly from the LSDV
reference **with substitutions planted at known positions**, and tells you which:

```text
planted   : 25001A>G, 32346T>C, 41001A>G
  (1-based positions; every run of either pipeline must report these 3)
```

So you can tell a working run from a broken one without a reference result to
diff against. It is seeded — everyone's fixtures are byte-identical.

It writes three files — `reads.fastq.gz`, `scheme.bed` and `query.fasta` — so
these run as written:

```bash
make smoke INPUT=fixtures/reads.fastq.gz \
  ARGS="--param primer.enabled=true --extra primer_bed=fixtures/scheme.bed"

# amd64 hosts only — see the Apple Silicon note below
make smoke PIPELINE=snippy-fasta INPUT=fixtures/query.fasta
```

These prove the plumbing, the tool invocations and the resource limits. The
reads are perfect apart from a uniform substitution rate, so they prove nothing
biological. Use real data before trusting a variant call.

### When a stage fails

The full log is at `runs/<run-id>/logs/NN_<stage>.log`, and **its first line is
the exact `docker run` command**. Copy that line, run it yourself, and you have
reproduced the failure with no Python in the way.

### ⚠ One thing you cannot test on an Apple Silicon Mac

The tool images are `linux/amd64`. Most run fine under emulation, but binaries
compiled with SSE instructions do not: `vcfstreamsort` and `vcfuniq` inside the
Snippy image die with SIGILL (exit 132). **`snippy-fasta` therefore cannot
complete on an M-series Mac** — it reaches variant calling and dies there. Not a
code defect, and there is no workaround short of an amd64 host. `ont-lsdv` runs
end to end fine on both architectures.

### How the dev container is wired

It sits behind a compose profile, so `docker compose up` never starts it. It
mounts this checkout **at its own host path** and mounts the Docker socket, for
the reason described under [Path parity](#-path-parity): the pipeline and the
container-backed tests start sibling containers with bind mounts, and the daemon
resolves a bind-mount source on the host. Parity is what makes one path string
valid on both sides. The `Makefile` passes that path as `DHARINI_REPO`.

`make images-push` is the one target that does **not** run in the container.
Registry credentials never cross the container boundary — `docker login` stores
them through a credential helper backed by the OS keychain, so a container that
mounts only the socket authenticates as nobody and the push fails with
`insufficient_scope: authorization failed`, which reads like an account problem
rather than a missing credential. Walking the manifests still happens inside the
container; only `docker push` runs on the host.

### What images already exist

Nobody should have to open five manifests to find out. One command shows the
whole catalogue, discovered by walking the tree rather than from any list:

```bash
make images-list                # every scope
make images-list SCOPE=shared   # one scope
```

For each image it prints the key you would write in `specs.py`, the reference
actually in force and whether that came from an environment override, an
`images.lock.json` digest or the manifest tag, whether it is `build` or
`registry`, whether your daemon already holds it, and which pipelines reference
it. The shared scope is printed first and its keys are shown fully qualified
(`shared:snpeff-lsdv`), because that is exactly what an author writing a sixth
pipeline should type instead of declaring a seventh copy of an image that
already exists.

### Tests

```bash
make test        # everything, including the container-backed runner tests
make test-fast   # skips anything that needs Docker
make check       # contracts and manifests only
```

Seven tests are marked `docker` and run a real container to prove the runner's
safety properties; they skip when no daemon is available. `make test-fast` skips
them explicitly. `.github/workflows/tests.yml` sets `REQUIRE_DOCKER=1` so that on
CI — where a daemon is always present — the skip becomes a failure instead.

Those tests start sibling containers through the mounted socket, which is the
same mechanism the worker uses, so running the suite inside a container tests the
real thing rather than a simulation of it.

They use synthetic pipelines built from ordinary shell tools, so the whole
control plane is covered without installing anything heavy. `test_runner.py` is
the important one — every test there corresponds to a real defect, and a
failure means a class of bug has come back.

---

## Adding a pipeline

See **[docs/PIPELINE_AUTHORING.md](docs/PIPELINE_AUTHORING.md)** for the full
contract, the rules and a worked example.

Short version: **a pipeline is one new directory under `app/pipelines/`** — a
package implementing `Pipeline`, plus a `manifest.yaml` declaring the container
images it needs. No existing file changes: `app/pipelines/__init__.py` discovers
every subpackage with `pkgutil` at startup, and each one registers itself.

---

## Adding a custom tool image

Most tools already have a usable image somewhere. **Check before you build one:**

```bash
make images-list
```

If the tool exists on Docker Hub, quay.io or a biocontainer, declare it as
`source: registry` and stop — there is nothing to build or maintain. If the
image you need already appears in the `shared` scope, reference it by its
qualified key (`shared:snpeff-lsdv`) instead of declaring a copy.

Build your own only when no published image works — the tool is unpublished
(ARTIC `align_trim`), or the published one is not configured for our data
(snpEff without the LSDV database).

### 1. Where the code goes

| Used by | Dockerfile | Declared in |
| --- | --- | --- |
| One pipeline | `app/pipelines/<your_pipeline>/docker/<key>/Dockerfile` | that pipeline's `manifest.yaml` |
| Two or more | `shared/docker/<key>/Dockerfile` | `shared/manifest.yaml` |

Start it in your own pipeline. Move it to `shared/` the day a second pipeline
needs it — not in anticipation of one.

### 2. Write the Dockerfile

Copy `app/pipelines/ont_lsdv/docker/align-trim/Dockerfile` as a template:

```dockerfile
FROM mambaorg/micromamba:1.5-jammy
USER root
RUN micromamba install -y -n base -c bioconda -c conda-forge \
      your-tool=1.2.0 \
      samtools=1.21 \
 && micromamba clean -a -y
ENV PATH=/opt/conda/bin:$PATH
```

Four rules, each of which exists because breaking it has already cost us:

- **Pin every version, including transitive ones.** `nanoplot=1.42.0` was
  pinned and satisfied, but an unpinned transitive `python-kaleido` resolved to
  a major version that removed a module NanoPlot imports, and the default QC
  tool died at import. Pin what breaks you, not just what you asked for.
- **Never set an `ENTRYPOINT`.** The runner always supplies a complete argv; an
  entrypoint would prepend itself to it and corrupt the command.
- **Tools on either side of a shell pipe must share one image.** `align_trim`
  streams SAM into `samtools sort`, so both live in the align-trim image.
  Splitting them would mean materialising an intermediate SAM several times the
  size of the BAM.
- **Match shared tool versions across images.** align-trim pins the same
  `samtools=1.21` as `core`, so the two cannot disagree about BAM handling.

Find available versions without installing anything locally:

```bash
docker run --rm --platform linux/amd64 mambaorg/micromamba:1.5-jammy \
  micromamba search -c bioconda -c conda-forge your-tool
```

### 3. Declare it

In the `manifest.yaml` beside your pipeline:

```yaml
images:
  your-tool:
    source: build                       # `registry` if you are pulling one
    dockerfile: docker/your-tool/Dockerfile
    context: docker/your-tool
    repository: dsihartpark/dharini-your-tool
    version: "1.0.0"                    # never `latest` — the loader rejects it
    platform: linux/amd64
```

`repository` must sit under `dsihartpark/`; a contract test fails the build if
an image is declared under any other namespace, so a personal account cannot
reach production by accident.

### 4. Use it

In your pipeline's `specs.py` — by key, never by image string, so the manifest
and the digest lock stay authoritative:

```python
YOUR_TOOL = StageSpec(image=img("your-tool"), cpus=2, memory_gb=4, timeout_s=3600)
```

### 5. Build, test, publish

```bash
make images-build SCOPE=<your-pipeline-scope>   # or omit SCOPE for everything
make smoke INPUT=fixtures/reads.fastq.gz        # prove it actually runs
make images-push                                # needs `docker login` on the host
make images-lock                                # record the digest
```

Commit the updated `images.lock.json`. **An image that is built but not pushed
works on your laptop and nowhere else**, and that failure appears in production
rather than in CI. `.github/workflows/images.yml` rebuilds, pushes and refreshes
the lock whenever a manifest or Dockerfile changes on `main`.

---

## Known gaps

1. **Large inputs still go through a multipart upload.** Dharini posts the file
   to `POST /v1/runs` and the request holds it for the duration. That is fine
   for an assembled FASTA and wrong for a ~1 GB ONT FASTQ. The intended flow —
   Dharini uploads to S3, then submits a JSON body naming the object plus a
   callback URL, and the run fetches it as its first stage — is designed but
   **not built**. This is the main thing between here and production ONT use.
2. **No real LSDV primer scheme exists in this repository.** Primer trimming is
   implemented and verified against a *synthesised* scheme, and a scheme whose
   contigs do not match the reference is now rejected outright. But no run has
   ever used a genuine LSDV amplicon design, because nobody has supplied one.
   Until then, leave `primer.enabled` false.
3. **`snippy-fasta` has not been run end to end since containerisation.** Its
   reference-path bug is fixed and it now reaches variant calling, but the
   vcflib binaries in the Snippy image cannot execute under amd64 emulation on
   Apple Silicon (SIGILL), so the run cannot be completed on a developer Mac.
   **Run it once on the amd64 host before trusting it.**
4. **Variant calls have only been checked against synthetic reads.** All three
   ONT callers report exactly the planted substitutions, which proves the
   invocations, the plumbing and the resource limits. It proves nothing about
   accuracy on real basecalled data — in particular the Medaka model must match
   the basecaller that produced the reads, and a mismatch degrades calls
   silently.
5. **The worker mounts the Docker socket**, which is root-equivalent access to
   the host. Acceptable only because no client input ever reaches a shell — do
   not add a code path that changes that.
6. `init_db()` uses `create_all`. Fine for this schema — introduce Alembic
   before the first migration that has to preserve data.
