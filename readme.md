# 🧬 Dharini Pipeline Service

Runs bioinformatics pipelines asynchronously and **notifies Dharini by webhook**
when they finish. Built so that adding a new pipeline is a backend-only change —
no API, database or frontend work.

| Pipeline | Input | Tools |
|---|---|---|
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
docker compose exec worker python tools/images.py pull

curl localhost:8000/health
```

⚠ **`tools/images.py pull` needs `images.lock.json`, and this repository does
not have one yet.** The lock file is produced by `make images-lock`, which can
only resolve a digest for an image that has already been **built and pushed** to
a registry — and the custom images have not been pushed (see [Known
gaps](#known-gaps)). Until that happens the command exits with
`images.lock.json is missing or malformed`, the resolver falls back to the
mutable tags in the manifests, and each image is pulled on demand during the
first run that needs it. That works; it is just slower and not reproducible.

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
|---|---|
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
  "run": { "id": "…", "pipeline_id": "ont-lsdv", "pipeline_version": "1.0.0",
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

**Docker is the only prerequisite.** There is no virtualenv to create, no
`pip install` to run and no conda environment to set up — not for the
bioinformatics tools, which have always run in their own containers, and not for
the application either. Every `make` target runs inside the `dev` stage of this
repository's own `Dockerfile`:

```bash
cp .env.example .env      # defaults to SQLite + local runs/ — no Postgres needed
docker compose up -d --build

make test                 # the suite, in a container
make images-list          # what images exist, and who uses them
```

`make help` lists the rest: `dev-image`, `images-build`, `images-push`,
`images-lock`, `images-pull`, `test-fast`, `check`, `shell`.

The `dev` service sits behind a compose profile, so `docker compose up` never
starts it. It mounts this checkout **at its own host path** and mounts the Docker
socket, for the reason described under [Path parity](#-path-parity): the
container-backed tests start sibling containers with bind mounts, and the daemon
resolves a bind-mount source on the host. Parity is what makes one path string
valid on both sides. The `Makefile` passes that path as `DHARINI_REPO`.

To pull the tool images each manifest declares, rather than waiting for the first
run to fetch them:

```bash
make images-pull
```

This needs `images.lock.json`, which does not exist yet — see the warning under
[Quick start](#quick-start-production--single-host). Without it, each image is
pulled on demand the first time a stage needs it, at whatever mutable tag its
manifest declares.

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

See **[docs/PIPELINE_AUTHORING.md](docs/PIPELINE_AUTHORING.md)**.

Short version: **a pipeline is one new directory under `app/pipelines/`** — a
package implementing `Pipeline`, plus a `manifest.yaml` declaring the container
images it needs. No existing file changes: `app/pipelines/__init__.py` discovers
every subpackage with `pkgutil` at startup, and each one registers itself.

---

## Known gaps

1. **The custom images have never been pushed.** Every image is now declared in
   a `manifest.yaml` and every custom one has a Dockerfile in this repository
   (`shared/docker/`, `app/pipelines/ont_lsdv/docker/`), built on micromamba +
   bioconda — the `suhas0/*` personal-account images are gone. What remains is
   that nothing has been published to `dsihartpark/*`: the registry namespace is
   unconfirmed and `docker push` has not been authorised. Until it is,
   `make images-push && make images-lock` cannot run.
2. **There is no `images.lock.json`, so nothing is digest-pinned yet.** The
   machinery is in place — `make images-lock` resolves every declared image to a
   digest, `app/runtime/images.py` prefers a locked digest over the manifest tag,
   `REQUIRE_PINNED_IMAGES=true` makes an unpinned image a startup failure, and
   `.github/workflows/images.yml` regenerates and commits the lock on every
   change to a manifest or Dockerfile. All of it is blocked on gap 1. Until the
   lock file exists, images resolve to their manifest tags: pinned to an exact
   version string, but a tag can be re-pushed, so two runs months apart are not
   provably identical.
3. **The worker mounts the Docker socket**, which is root-equivalent access to
   the host. Acceptable only because no client input ever reaches a shell — do
   not add a code path that changes that.
4. **Medaka and Clair3 invocations are adapted but unverified** against real
   data; they need one confirmation run each before being trusted.
5. `init_db()` uses `create_all`. Fine for this schema — introduce Alembic
   before the first migration that has to preserve data.
