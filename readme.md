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

Object storage bookends the run: the input is fetched from S3, the artifacts
are published back to it, and this service issues **no presigned URL at
either end**. Dharini uploads with its own credentials and hands over the
object key; Dharini also mints the eventual download link. What sits between
those two S3 calls is the same two planes as before.

```
   BROWSER            DHARINI (NestJS)        S3                 THIS SERVICE
      │                     │                  │                     │
      ├──"upload"──────────►│                  │                     │
      │◄──presigned PUT─────┤                  │                     │
      ├──────── PUT 1.2 GB ────────────────────►│                     │
      │                     │  uploads/u17/2f8c/reads.fastq.gz       │
      │                     ├─POST /v1/runs  X-API-Key ─────────────►│  validate
      │                     │                  │◄─HeadObject──────────┤  (§ Submit a run)
      │                     │◄──202 {run_id}───┤                     │
      │                     │                  │◄─GetObject───────────┤  fetch
      │                     │                  │                     │  … stages …
      ├─GET …/progress  Bearer <JWT> ─────────────────────────────────►│  (poll 3-5s)
      │                     │                  │◄─PutObject×N─────────┤  publish
      │                     │◄─run.completed (signed, bucket+key)─────┤
      │                     │◄─presign GET─────┤                     │
      │◄──────── download ───────────────────► │                     │
```

`fetch` and `publish` are stages the **platform** prepends and appends to
every pipeline's own declared list — never written by a pipeline author,
always first and last. `app/storage.py` is the only module in the repository
that imports `boto3`; a `Pipeline`'s `run(ctx)` never learns S3 exists, and
`ctx.artifact()` is unchanged whether the backend under it is S3 or a laptop's
filesystem.

```
┌─────────────────────────────────────────────────┐
│ CONTROL PLANE — FastAPI                         │
│  auth: X-API-Key (service) / Bearer JWT (user)  │
│  validates params against the pipeline's schema │
│  HeadObject on every input key before queueing  │
│  persists the run ──────────────► Postgres      │
│  enqueues ──────────────────────► Redis/Celery  │
└─────────────────────────────────────────────────┘
      ▼
┌─────────────────────────────────────────────────┐
│ EXECUTION PLANE — Celery worker                 │
│  fetch    ─► GetObject   into $RUNS_ROOT/input   │
│  PipelineRegistry.get(id).run(ctx)              │
│         └─► ContainerRunner ─► docker run       │
│                (argv, no shell, path parity)    │
│         └─► ctx.stage("align") ─► Postgres      │
│  publish  ─► PutObject   from $RUNS_ROOT/output  │
└─────────────────────────────────────────────────┘
      ▼
  webhook ──► Dharini      (push: low latency, artifacts by bucket/key)
  GET /v1/runs/{id} ◄────── Dharini  (pull: certainty)
```

**Key components**

- **`app/pipelines/`** — one package per pipeline, discovered at startup and
  registered by id. The only place that changes when a pipeline is added.
- **`app/runtime/runner.py`** — the *only* code in the repo that starts a
  process. Every safety property is enforced here, once, for all pipelines.
- **`app/runtime/context.py`** — what a pipeline author programs against.
- **`app/storage.py`** — the *only* module that imports `boto3`/`botocore`.
  `S3Store` backs production; `LocalStore` backs the test suite and
  `tools/run_local.py`, so the fast suite touches no S3 at all.
- **`app/auth.py`** — verifies `X-API-Key` and Bearer JWTs, entirely locally
  (see [Authentication](#authentication)).
- **`app/delivery.py` + `app/tasks.py`** — signed webhooks with retry, the
  `fetch`/`publish` bookends, and the recovery paths for when either delivery
  or upload fails.

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
# POSTGRES_PASSWORD, PUBLIC_BASE_URL, S3_BUCKET, S3_REGION, API_KEYS, and:
#   DOCKER_GID=$(getent group docker | cut -d: -f3)
# See Authentication and Deployment below for what API_KEYS and the S3_*
# variables actually control -- both are required, not optional, in production.

docker compose up -d --build

# A brand-new database needs nothing further: init_db() runs
# `alembic upgrade head` automatically at API startup. Deploying onto a
# database this service already created before Alembic was adopted needs one
# manual step first -- see Deployment.

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

## Authentication

Every route requires a credential except `GET /health` (load-balancer probe)
and `GET /` (service banner).

**`X-API-Key: <secret>`** — server-to-server, full access. This is the
credential Dharini's backend holds, and the *only* one accepted by
`POST /v1/runs`, `POST /v1/runs/{id}/redeliver` and
`POST /v1/runs/{id}/reupload`.

**`Authorization: Bearer <Dharini JWT>`** — the browser's own session token,
verified **locally**, never by a round trip to Dharini: an auth check that
phones home fails exactly when Dharini is down, and adds that latency to
every progress poll. It is read-only, and scoped to `owner_id == token.sub`
— a run belonging to someone else comes back `404`, not `403`. A `403` would
confirm the run exists, turning id enumeration into a working membership
oracle.

### Why submission is service-only

`POST /v1/runs` takes a `callback_url`, and the worker that eventually calls
it sits inside the VPC, with a clear path to internal services and the cloud
metadata endpoint (`169.254.169.254`). `validate_callback_url` exists
specifically to stop a client-chosen URL from turning that into an SSRF
pivot. A browser is not a trusted network position for that decision: if a
Bearer token could submit a run, anyone holding a stolen or forged session
token could choose the callback URL themselves, and `validate_callback_url`
would be evaluating a request from precisely the position it was built to
keep out. So submission, redelivery and reupload stay API-key-only regardless
of how strong the JWT verification is — this is a design boundary, not a gap
to close later.

### Configuring it

`API_KEYS` is comma-separated `label:secret` pairs
(`dharini:sk_live_…,ops:sk_live_…`), compared with `hmac.compare_digest` —
never `==`, which leaks a secret one byte at a time under timing analysis.
The label is what a request log names the caller, and what makes rotation
possible: add the new pair, deploy, migrate the caller over, remove the old
one. **Empty `API_KEYS` refuses to start** unless `ALLOW_UNAUTHENTICATED=true`
is set explicitly — the same "insecure state must be chosen, never reached by
forgetting to set something" rule as `REQUIRE_PINNED_IMAGES`. Locally, either
set `ALLOW_UNAUTHENTICATED=true` or just pick a key — `tests/conftest.py`
does the latter, setting `API_KEYS=test:test-key` so the suite always has a
fixed credential to send.

Bearer verification prefers RS256 via `DHARINI_JWT_JWKS_URL`, so this service
holds only a public key and cannot itself mint a token; `DHARINI_JWT_SECRET`
(HS256) is the weaker fallback of a single shared secret that both verifies
and issues. **Bearer auth is off entirely** unless one of those two is
configured — a half-configured verifier that falls through to accepting
unsigned tokens is worse than no browser access at all. `DHARINI_JWT_ISS` and
`DHARINI_JWT_AUD` are required alongside either one; without them a token
minted by any issuer for any audience would be accepted.

`CORS_ALLOWED_ORIGINS` replaces the old `allow_origins=["*"]`, which — with
`allow_credentials=True` — is exactly the configuration that lets any page
read an authenticated response. An empty list disables CORS outright, the
right value if no browser calls this service directly.

---

## API

Interactive docs at `/docs`. Both credential types are declared as OpenAPI
security schemes, so the page has an **Authorize** button for trying a real
`X-API-Key` or Bearer token against a live endpoint, and any client Dharini
generates from the schema already knows both exist.

### Discover pipelines

```bash
curl localhost:8000/v1/pipelines -H 'X-API-Key: sk_live_…'
```

Returns each pipeline's `stages` (`fetch` and `publish` bookend every
pipeline's own list — see [Architecture](#architecture)), its `extra_inputs`
(secondary files like an ARTIC primer scheme) and its `params_schema` (JSON
Schema). **Dharini renders its parameter form from this** — which is why a
new pipeline needs no frontend change.

### Submit a run

The file already exists in S3 by the time you call this: Dharini uploads it
there directly, with its own credentials, and sends the object key. This
service issues no presigned URL and never streams the bytes a second time.

```bash
curl -X POST localhost:8000/v1/runs \
  -H 'X-API-Key: sk_live_…' \
  -H 'Content-Type: application/json' \
  -d '{
        "pipeline_id": "ont-lsdv",
        "external_id": "dharini-job-8831",
        "owner_id": "u17",
        "input_key": "uploads/u17/2f8c/reads.fastq.gz",
        "extra_input_keys": {"primer_bed": "uploads/u17/2f8c/scheme.bed"},
        "params": {"variant": {"tool": "clair3"}, "threads": 4},
        "callback_url": "https://dharini.example.org/api/snp/webhook"
      }'
```

```json
{
  "run_id": "…", "external_id": "dharini-job-8831",
  "pipeline_id": "ont-lsdv", "pipeline_version": "1.1.0",
  "status": "QUEUED",
  "stages": ["fetch", "prepare", "qc", "align", "primer", "variant",
             "consensus", "annotate", "report", "publish"],
  "queue_position": 0, "estimated_start_s": 0
}
```

Two things worth knowing:

- **`external_id` is chosen by the caller before submitting, and is unique.**
  Resubmitting the same one is idempotent — the response is `200` with the
  *original* run, not a second hour of Medaka compute. This is what makes a
  client's timeout-and-retry safe, and it is also how the eventual webhook
  can be correlated if the `202` itself was lost.
- **Every input key is validated before anything is queued**: well-formed,
  under the `uploads/` prefix, an accepted extension, and confirmed with a
  `HeadObject` call to actually exist and sit within `MAX_INPUT_BYTES` — all
  in milliseconds. A typo'd key is a `422` immediately, not a run that
  queues, waits behind three others, and dies twenty minutes later.

Only a service credential may call this — see
[Authentication](#authentication) above for why.

### Check status

```bash
curl localhost:8000/v1/runs/<run_id> -H 'X-API-Key: sk_live_…'
```

Includes `progress` (e.g. `"5/10 · variant"`), per-stage timings and peak
memory, artifact metadata, metrics, and webhook delivery state. A Bearer
token may call this too, scoped to its own runs.

### Watch it run

`GET /v1/runs/{id}` is the wrong shape to poll every few seconds — it drags
`params`, artifacts and delivery history along for the ride. This endpoint is
built for that, and is the one a browser calls directly with the Dharini
session token it already holds:

```bash
curl localhost:8000/v1/runs/<run_id>/progress \
  -H 'Authorization: Bearer <dharini-session-jwt>'
```

```json
{
  "run_id": "…", "external_id": "dharini-job-8831",
  "status": "RUNNING",
  "stage": "variant", "stage_index": 5, "stage_total": 10,
  "stage_started_at": "2026-08-19T18:41:02Z",
  "stage_elapsed_s": 754,
  "stage_typical_s": 1180,
  "heartbeat_age_s": 1.8,
  "eta_s": 1640, "eta_at": "2026-08-19T19:21:22Z",
  "queue_position": null, "estimated_start_s": null,
  "stages": [
    {"name": "fetch", "status": "SUCCEEDED", "duration_s": 25.1},
    {"name": "prepare", "status": "SUCCEEDED", "duration_s": 8.3},
    {"name": "variant", "status": "RUNNING", "duration_s": null}
  ]
}
```

**There is deliberately no sub-stage percentage.** Clair3, Medaka and
minimap2 emit nothing machine-readable mid-stage, so "62% through variant
calling" would be a number this service made up. What it reports instead is
honest:

- **which stage**, and how long it has been running (`stage_elapsed_s`);
- **whether the worker is alive** (`heartbeat_age_s`) — the stats sampler
  that already runs for peak-memory tracking writes a heartbeat every
  `HEARTBEAT_INTERVAL_S`; a UI should treat anything past roughly 3× that as
  "no signal", not "failed" — the run may still be fine and the sampler
  merely stalled;
- **how long that stage usually takes** (`stage_typical_s`) — the **median**
  `duration_s` of the same stage over the pipeline's last `ETA_HISTORY_RUNS`
  successful runs. Median, not mean, so one pathological forty-minute
  alignment doesn't drag everyone else's estimate with it.

`eta_s` sums the remaining time in the current stage with the typical
duration of every stage still ahead. **Both `stage_typical_s` and `eta_s` are
`null` until there is history to derive them from.** The first run of a new
pipeline reports elapsed time and nothing else — inventing a number would be
worse than admitting there isn't one yet, and a frontend should render
*"Variant calling · 12m elapsed"*, not a progress bar that means nothing.

While a run is `QUEUED`, `queue_position` and `estimated_start_s` are
populated instead of a stage. `GET /v1/queue` below is the same estimate,
computed for a run that has not been submitted yet.

### Queue depth

```bash
curl localhost:8000/v1/queue -H 'X-API-Key: sk_live_…'
```

```json
{
  "worker_concurrency": 1,
  "running": [{"run_id": "…", "pipeline_id": "ont-lsdv",
               "stage": "variant", "eta_s": 1640}],
  "queued": 3,
  "estimated_wait_s": 5200
}
```

`running` names other users' run ids and is **omitted entirely** (not merely
`null`) under a Bearer token — queue depth and expected wait aren't
sensitive, another user's runs are. The same `estimated_wait_s` figure comes
back as `estimated_start_s` in `POST /v1/runs`'s `202`, and while a run is
`QUEUED`, in its progress payload — so a frontend can say *"3 ahead of you ·
about 1h 25m before this starts"* instead of an unqualified "queued".

### Other endpoints

| Endpoint | Credential | Purpose |
| --- | --- | --- |
| `GET /v1/runs?pipeline_id=&status=&limit=` | either | list runs (service key: everyone's; Bearer: own only) |
| `GET /v1/runs/{id}/artifacts/{filename}` | either | download a result file — kept for operational debugging; Dharini mints its own presigned S3 GET for an end user |
| `POST /v1/runs/{id}/redeliver` | service only | re-send the webhook manually |
| `POST /v1/runs/{id}/reupload` | service only | re-run artifact publication, after an S3 outage longer than the retry schedule |
| `GET /health` | open | liveness, disk, schema revision, storage reachability |

There is no longer a multipart submission path or a legacy `/upload`,
`/status/{job_id}`, `/download/...` route — `POST /v1/runs` with a JSON body
is the only way in, for every pipeline.

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
  "run": { "id": "…", "external_id": "dharini-job-8831", "owner_id": "u17",
           "pipeline_id": "ont-lsdv", "pipeline_version": "1.1.0",
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
                   "content_type": "text/plain",
                   "bucket": "dharini-prod", "key": "runs/<run_id>/annotated.vcf" } ],
  "artifacts_uploaded": true,
  "artifacts_error": null,
  "output": { "bucket": "dharini-prod", "prefix": "runs/<run_id>/" }
}
```

Parsed variants are inline so you can store queryable rows immediately.
**No file bytes are ever in this body, and artifacts are no longer a URL —**
`artifacts[].bucket` / `.key` name where the object lives, and Dharini mints
its own presigned GET for the eventual user download rather than trusting a
link this service hands out. This also outlives local retention: the S3
object a key points at is still there long after the six-hour local copy
under `$RUNS_ROOT` is swept.

`external_id` and `owner_id` on `run` are new — a consumer can correlate a
delivery without having stored `run_id`, which matters exactly when the
original `202` was lost.

`artifacts_uploaded` is `false` and `artifacts_error` is set when the run
computed successfully but the S3 upload exhausted its retry budget (see
[Deployment](#deployment)). The webhook still fires in that case — the run's
science is real even when the files are momentarily stranded — and
`POST /v1/runs/{id}/reupload` is the recovery. Conversely, **the webhook is
withheld entirely until the upload either succeeds or exhausts**: a delivery
carrying keys that 404 is worse than a late delivery.

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

- `GET /v1/runs?status=SUCCEEDED -H 'X-API-Key: sk_live_…'` — reconcile anything missed
- `POST /v1/runs/{id}/redeliver` — re-send one

Artifacts are **never** purged by retention while a delivery is still
outstanding, nor while a run's S3 upload is still mid-retry — see
[Storage](#storage) below for the exact rule; deleting the only copy of a
successful result out from under a retrying upload would turn a delay into
data loss.

### What Dharini's side of this contract has to do

Not enforced by this service, but the design does not hold without it:

1. **Generate `external_id` before submitting**, not after. It is what makes
   a retry after a lost `202` idempotent; generating it from the response
   defeats the purpose.
2. **Hold `X-API-Key` server-side only.** It is a full-access credential —
   never ship it to a browser.
3. **Reject an `X-Dharini-Timestamp` older than ~5 minutes**, as the NestJS
   example above does. The signature covers the timestamp, but that only
   stops a *tampered* replay — freshness is what stops a *recorded* one from
   being replayed verbatim.
4. **Mint the presigned GET users actually download from.** This service
   serves artifacts only for operational debugging (`/artifacts/{filename}`,
   `X-API-Key` or the owning user's own Bearer token) — it is not the
   frontend's result server.
5. **Run a reconciliation sweep.** Any run submitted more than roughly an
   hour ago with no terminal status is worth a `GET /v1/runs/{id}` — this is
   what makes an `ABANDONED` delivery recoverable without a human noticing.
6. **Handle `artifacts_uploaded: false`.** The run succeeded and the results
   are stranded; `POST /v1/runs/{id}/reupload` is the fix, not a resubmission
   of the run.

---

## Deployment

### IAM

Attached to the EC2 instance role. No IAM user, no access key, no secret on
disk — `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` are deliberately absent
from `.env.example`; if the process cannot obtain credentials from the role,
that is a startup failure, not a fallback to something weaker.

```jsonc
{ "Effect": "Allow", "Action": "s3:GetObject",
  "Resource": "arn:aws:s3:::dharini/uploads/*" },
{ "Effect": "Allow", "Action": "s3:PutObject",
  "Resource": "arn:aws:s3:::dharini/runs/*" }
```

(`dharini` above is the example `S3_BUCKET`; substitute your own.) That is
the **whole** policy — no `s3:ListBucket`, no `s3:DeleteObject`, no
`s3:GetObject` on `runs/*`, no wildcards. The result: this host can read the
one object it was told about, by exact key, and create new objects under a
run-scoped prefix. It cannot enumerate the bucket, cannot read back another
run's results, and cannot destroy anything — a compromise of this service is
one object read, not the bucket.

The missing `s3:ListBucket` has a visible consequence: without it, S3 answers
a HEAD or GET on a key that doesn't exist with `403 AccessDenied` rather than
`404 NoSuchKey`, so `app/storage.py` treats both the same way — a run
submitted with a typo'd key gets a clean `422`, not a `500` that looks like a
service failure.

### Bucket versioning is not optional

**Turn on versioning.** `PutObject` on an existing key *is* an overwrite, and
IAM has no way to express "create but never replace" — the policy above
genuinely permits an artifact-upload retry to clobber a prior version, and
without versioning that clobber is unrecoverable. Versioning is what makes
the "cannot destroy anything" claim above actually true, and it's also what
makes an upload retry safe rather than merely convenient.

### Lifecycle rules

- `uploads/` — expire after **7 days**. An input is consumed within minutes
  of submission; keeping it around longer serves only resubmission.
- `runs/` — transition to Glacier Instant Retrieval at **90 days**. The
  short-term copy already lives on local disk (`RETENTION_HOURS`, see
  [Storage](#storage) below).

**Both rules must carry a noncurrent-version-expiration clause.** Versioning
plus retry churn otherwise grows storage without bound: every retried
`PutObject` leaves a noncurrent version behind, and a lifecycle rule that
only expires the *current* version never reclaims it. Verify this by reading
the bucket's lifecycle configuration back after applying it — passing the
happy path proves nothing about the clause being present.

### Database migrations

`init_db()` runs `alembic upgrade head` automatically at API startup, so a
**fresh** database needs no manual step. A database this service already
created before Alembic was adopted (with SQLAlchemy's `create_all()`) needs
one step, once, before the first deploy of this version:

```bash
make stamp     # records "head" WITHOUT running any DDL
```

Skip it and Alembic tries to `CREATE TABLE` over tables that already exist,
and fails loudly rather than silently — but it is still a deploy-day surprise
worth avoiding by doing this first. From then on:

```bash
make migrate            # bring a database to head by hand
make migration m="…"    # generate a new revision after changing app/models.py
```

`GET /health` reports `schema_revision`, precisely so "the migration didn't
run" and "the code is newer than the schema" don't look identical from
outside.

### `/health`

```bash
curl localhost:8000/health
```

```json
{
  "status": "ok", "pipelines": 2, "worker_concurrency": 1,
  "schema_revision": "0002",
  "storage": {"backend": "s3", "bucket": "dharini", "reachable": true},
  "disk": {"free_gb": 812.4, "total_gb": 999.9, "used_percent": 18.8}
}
```

Always `200` while the process itself is up. Storage being unreachable is
reported in `storage.reachable` rather than failing the check: taking every
instance out of rotation because S3 blipped turns a degradation into an
outage, and this is what an operator actually reads to tell "the process is
dead" apart from "the process is fine but can't reach S3" apart from "the
schema is behind the code" — three failure modes that otherwise look
identical from outside, at 2am, on a run that has been computing for forty
minutes.

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
publishes survive, and even those are now local scratch, not the durable
copy. Once `publish` uploads them to S3, local disk under `$RUNS_ROOT` is
swept after `RETENTION_HOURS` (default **6** — down from 72 before artifacts
moved to S3; the 320 GB volume is no longer the constraint it used to be,
because S3 lifecycle rules own the long term, see
[Deployment](#deployment)).

**A run submitted through this flow whose artifacts have not reached S3
(`input_key` set and `artifacts_uploaded = False`) is never purged**, for the
same reason a run with an undelivered webhook is never purged: deleting the
only copy of a successful result because its upload was still retrying would
turn a delay into data loss. The `input_key` scoping matters for any run that
predates this feature: it has no `input_key` and `artifacts_uploaded` will
always be `False`, and without the scoping it would be exempt from retention
forever — every historical run's files sitting on disk permanently instead of
following the ordinary window they were created under.

**Run records are kept indefinitely**, since the history is the point of a
surveillance platform.

### Debugging a failed run

```bash
curl localhost:8000/v1/runs/<run_id> -H 'X-API-Key: sk_live_…' | jq '.error_stage, .error_message'
ls $RUNS_ROOT/<run_id>/logs/          # full stdout+stderr per stage
```

A failure inside `fetch` or `publish` shows up here exactly like a failure in
any pipeline-declared stage — `error_stage` names it, and there is a matching
log file. Most `fetch` failures never get this far, though: a missing or
oversized input key is caught by `HeadObject` at submission time (see
[Submit a run](#submit-a-run)) and is a `422`/`413` in milliseconds, not a
run that queues and dies twenty minutes later.

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

**On Linux**, add yourself to the `docker` group once — the dev container talks
to the daemon through the mounted socket, and without it every target fails with
*permission denied while trying to connect to the Docker daemon socket*:

```bash
sudo usermod -aG docker $USER && newgrp docker
```

That is the whole Linux setup. The `Makefile` reads the socket's group id
itself, so there is nothing to look up or configure.

Files written by a run are owned by root, because the tool containers run as
root — that is true on any Linux host and is why `runs/` and `fixtures/` are
cleaned by a container rather than by you:

```bash
make clean       # removes runs/* and fixtures/ — no sudo
```

**No `.env` is needed for local work.** The `dev` container deliberately does
not read one: the test suite points itself at SQLite in a temporary directory,
and `make smoke` sets `RUNS_ROOT` and `REFS_ROOT` to paths inside the checkout.
`.env.example` is for deploying to a server — it points `RUNS_ROOT` at
`/srv/dharini`, which does not exist on your machine. Copy it when you deploy,
not when you clone.

**The suite never talks to S3.** `tests/conftest.py` sets
`STORAGE_BACKEND=local`, which makes every test — and `make smoke` — run
against `LocalStore`, a filesystem-backed implementation of the exact same
`ObjectStore` interface `S3Store` implements. `S3Store` itself is exercised
against a real S3 API by `tests/test_storage_s3.py`, which is skipped
whenever `S3_ENDPOINT_URL` is unset. If you're touching `app/storage.py`
itself, run it for real:

```bash
docker compose --profile dev up -d minio
docker compose run --rm --no-deps \
  -e S3_ENDPOINT_URL=http://minio:9000 \
  -e AWS_ACCESS_KEY_ID=dharini -e AWS_SECRET_ACCESS_KEY=dharini-dev-secret \
  dev python -m pytest tests/test_storage_s3.py -v
```

`minio` has no host port mapping — it's reachable by service name only from
containers on the same compose network, which is what makes
`http://minio:9000` resolve inside `dev` but not from your own shell. The
access key pair matches `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` in
`docker-compose.yml`; `S3Store` takes no credentials of its own and reads
them the same way it would read an instance role in production, through
boto3's own credential chain. Nobody else needs any of this; it's the one
integration point `LocalStore` cannot stand in for.

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

### Where everything goes

There is one rule: **the dev container mounts this checkout and nothing else.**
A path like `~/data/reads.fastq.gz` does not exist inside it. Everything you
want a pipeline to read has to live under the repository directory.

```text
sequencing/
├── fixtures/          ← PUT YOUR INPUT FILES HERE   (git-ignored)
│     reads.fastq.gz         ONT reads for ont-lsdv
│     query.fasta            assembled genome for snippy-fasta
│     scheme.bed             ARTIC primer scheme, if you use trimming
│
├── data/              ← reference genome + annotation  (REFS_ROOT locally)
│     lsdv_reference_sequence.fasta
│     lsdv_reference_sequence.gff3
│
└── runs/              ← OUTPUT LANDS HERE            (git-ignored)
      <run-id>/input/    your file, under a normalised name
      <run-id>/work/     scratch, deleted unless you pass --keep
      <run-id>/output/   the results: VCFs, consensus, QC report
      <run-id>/logs/     one log per stage
```

**There is no config file to edit.** Parameters are passed on the command line
and validated against the pipeline's JSON Schema, which is also what Dharini
renders its form from — so there is one definition, not a file that drifts from
it. To see every parameter, its type and its default:

```bash
make params                          # ont-lsdv
make params PIPELINE=snippy-fasta
```

To use a different reference genome, drop it in `data/` and pass its filename:
`--param reference=my_reference.fasta`. It is a filename, not a path — it is
resolved inside the references directory so a pipeline cannot read arbitrary
files off the host.

### Run a pipeline on your own file

This is the loop to live in. `tools/run_local.py` builds the **same**
`RunContext` the Celery worker builds, applies the **same** schema validation
and defaults the API applies, and executes the **same** stages in the **same**
tool containers. It skips only Postgres, Redis, the queue, HTTP and S3 — the
parts you are not debugging. Your input file is copied straight into the run
directory, the same place `fetch` would have put it, so a pipeline never
knows the difference. A green run here means the pipeline works, not that a
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

`PIPELINE=` chooses which one runs, and defaults to `ont-lsdv`. **The two are
independent** — separate packages, separate manifests, separate images, and a
run of one executes none of the other's code. So you can work on, break and fix
one without touching the other, which is the same property that makes adding a
third a backend-only change.

The contract tests are parametrised the same way, so a failure names the
pipeline responsible rather than just the suite:

```text
tests/test_all_pipelines.py::test_pipeline_satisfies_the_contract[ont-lsdv]
tests/test_all_pipelines.py::test_pipeline_satisfies_the_contract[snippy-fasta]
```

`--param` takes dotted keys and JSON values, so `--param threads=8` is a number
and `--param primer.enabled=true` is a boolean. `--extra key=path` supplies a
secondary input. `--keep` retains `work/`, which the worker normally purges.
`--run-id NAME` gives the run a name you choose instead of a timestamp.

`make params PIPELINE=<id>` lists every parameter that pipeline accepts, with
its type and default. There is no server to start for it.

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

### ⚠ Apple Silicon cannot run `snippy-fasta`

`ont-lsdv` runs end to end on both architectures. `snippy-fasta` does not run on
an M-series Mac at all, and cannot be made to. Both escape routes are closed:

**Emulation fails.** The tool images are `linux/amd64` — native on a Linux
x86-64 box, emulated on a Mac. Most tools survive that; the vcflib binaries in
the Snippy image do not. `vcfstreamsort` and `vcfuniq` die with SIGILL
(exit 132), because the emulator does not implement every instruction they were
compiled to use. Snippy gets as far as `freebayes-parallel` and dies in the pipe
after it, so bwa, samclip, samtools and freebayes all demonstrably work — it is
specifically vcflib.

**A native arm64 image is not buildable.** snippy 4.6.0 pins
`vcflib >=1.0.0_rc3,<=1.0.2` and `tabixpp 1.1.0`, and bioconda publishes no
`linux-aarch64` build of either, so the environment does not solve. Newer vcflib
builds for aarch64 exist (1.0.9+) but snippy 4.6.0 will not accept them, and
changing snippy's version to suit a laptop would mean local results no longer
match what production computes — the exact thing pinning exists to prevent.

**So CI runs it instead.** `.github/workflows/tests.yml` has a `snippy-fasta`
job on an amd64 runner that executes the pipeline end to end and asserts it
finds the three planted variants. Exit status alone is not enough there: Snippy
can complete having called nothing, and a wrong reference or an empty alignment
both look like success.

If you are changing `snippy-fasta`, push the branch and read that job. It is the
only place the pipeline actually runs.

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

Tests marked `docker` run a real container to prove the runner's safety
properties, or (for `tests/test_storage_s3.py`) a real S3-compatible API to
prove `S3Store` works and not just `LocalStore`; both skip when their
dependency is unavailable. `make test-fast` skips them explicitly.
`.github/workflows/tests.yml` sets `REQUIRE_DOCKER=1` so that on CI — where a
daemon is always present — the runner tests' skip becomes a failure instead.
The S3 tests have no `minio` service in CI today, so they still skip there;
`REQUIRE_S3=1` is what would turn that into a failure once one is added, and
is the flag to set locally when you actually want to know `S3Store` works
rather than assume it.

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

1. **The API break is total, with no compatibility shim.** `POST /v1/runs` no
   longer accepts multipart at all, and every route now requires a credential.
   A Dharini deployment still speaking the old contract fails outright rather
   than degrading, so this version and the corresponding Dharini deploy have
   to ship together — see G9 in the design spec.
2. **`S3Store` is exercised locally and manually, not in CI.**
   `tests/test_storage_s3.py` runs against the `minio` dev-profile service and
   is skipped whenever `S3_ENDPOINT_URL` is unset — which is always true in
   CI today, since no `minio` service is wired up there. The backend that
   actually handles production traffic is therefore the one with the least
   automated coverage; a real multi-GB `ont-lsdv` upload (`keep_alignment:
   true`) on staging is the check that stands in for it before a deploy.
3. **No real LSDV primer scheme exists in this repository.** Primer trimming is
   implemented and verified against a *synthesised* scheme, and a scheme whose
   contigs do not match the reference is now rejected outright. But no run has
   ever used a genuine LSDV amplicon design, because nobody has supplied one.
   Until then, leave `primer.enabled` false.
4. **`snippy-fasta` is only ever exercised on amd64.** Its reference-path bug is
   fixed, and CI now runs it end to end and asserts the variant count. But no
   Apple Silicon machine can run it — see the note above — so a developer on a
   Mac changing that pipeline is relying entirely on CI to tell them whether it
   works.
5. **Variant calls have only been checked against synthetic reads.** All three
   ONT callers report exactly the planted substitutions, which proves the
   invocations, the plumbing and the resource limits. It proves nothing about
   accuracy on real basecalled data — in particular the Medaka model must match
   the basecaller that produced the reads, and a mismatch degrades calls
   silently.
6. **The worker mounts the Docker socket**, which is root-equivalent access to
   the host. Acceptable only because no client input ever reaches a shell — do
   not add a code path that changes that.
