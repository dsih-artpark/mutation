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
      │                     ├─POST /v1/runs ────────────────────────►│  validate
      │                     │                  │◄─HeadObject──────────┤  (§ Submit a run)
      │                     │◄──202 {run_id}───┤                     │
      │                     │                  │◄─GetObject───────────┤  fetch
      │                     │                  │                     │  … stages …
      ├──poll──────────────►├─GET …/progress ──────────────────────►│  (every 3-5s)
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
- **`app/auth.py`** — credential verification. **Currently disabled** — see
  [Access control](#access-control).
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

## Quick start (single host)

```bash
sudo mkdir -p /srv/dharini/runs /srv/dharini/references && sudo chown -R $USER /srv/dharini
mkdir -p /srv/dharini/service && cd /srv/dharini/service
IMAGE=dsihartpark/dharini-mutation-pipeline; VERSION=1.0.0      # a tag from Docker Hub
for f in compose.yaml deploy.sh; do docker run --rm $IMAGE:$VERSION cat /app/deploy/$f > $f; done
docker run --rm $IMAGE:$VERSION cat /app/.env.example > .env && chmod +x deploy.sh
$EDITOR .env                           # fill in the required variables
./deploy.sh $VERSION
```

Full instructions, including upgrades and rollback: [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).
On AWS, start with [docs/deploy-aws-ec2.md](docs/deploy-aws-ec2.md). To work on
the code instead, see [Local development](#local-development).

---

## Access control

**Authentication is on by default.** The service refuses to start unless
`API_KEYS` is set (see [Turning it on](#turning-it-on)) or you opt out with
`ALLOW_UNAUTHENTICATED=true`. The opt-out is for local development, and for
deployments where the network is the only access control:

> ### ⚠ The security group is the only access control
>
> Inbound `8000` must be restricted to **Dharini's security group** (or its
> Elastic IP) — never `0.0.0.0/0`. With auth off, an open port is an open API:
> anyone reaching it can submit runs, read every run's results, and choose the
> `callback_url` the worker will later call. See
> [Security group](docs/deploy-aws-ec2.md#4-security-group) under Deployment for the exact rules.

What is *not* weakened by this: **payload validation is identical either way.**
The pipeline id must exist, `params` must satisfy that pipeline's JSON Schema,
`input_key` must be well-formed, under `uploads/`, of an accepted extension and
confirmed present by `HeadObject`, and `callback_url` must survive
`validate_callback_url`'s allowlist and address checks. None of that is an auth
check and none of it is skipped. What is missing is any answer to *who is
calling* — which is precisely why the network has to answer it instead.

### Turning it on

The code is written, tested and already wired into every route; nothing needs
implementing. Two lines of `.env`:

```bash
API_KEYS=dharini:sk_live_…,ops:sk_live_…    # comma-separated label:secret
ALLOW_UNAUTHENTICATED=false
docker compose up -d                         # restart to pick it up
```

Then have Dharini send `X-API-Key: sk_live_…` on every call. Secrets are
compared with `hmac.compare_digest` — never `==`, which leaks a secret one byte
at a time under timing analysis. The label is what a request log names the
caller and what makes rotation possible: add the new pair, deploy, migrate the
caller over, remove the old one.

**Empty `API_KEYS` with `ALLOW_UNAUTHENTICATED=false` refuses to start.** The
insecure state has to be chosen explicitly, never reached by forgetting to set
something — the same rule as `REQUIRE_PINNED_IMAGES`.

### The browser path, when you get to it

`Authorization: Bearer <Dharini JWT>` is implemented too: read-only, scoped to
`owner_id == token.sub`, and verified **locally** rather than by a round trip to
Dharini — an auth check that phones home fails exactly when Dharini is down, and
would add that latency to every progress poll. A run belonging to someone else
comes back `404`, not `403`; a `403` would confirm the run exists and turn id
enumeration into a working membership oracle.

It stays **off** until `DHARINI_JWT_JWKS_URL` (RS256, preferred — this service
then holds only a public key and a compromise here cannot mint tokens) or
`DHARINI_JWT_SECRET` (HS256) is set, alongside `DHARINI_JWT_ISS`,
`DHARINI_JWT_AUD` and `CORS_ALLOWED_ORIGINS`. A half-configured verifier that
falls through to accepting unsigned tokens would be worse than no browser access
at all. Until then, **the browser polls through Dharini**, which is what the
diagram above shows.

### Why submission will stay service-only

Even once Bearer tokens are on, `POST /v1/runs` will not accept one. It takes a
`callback_url`, and the worker that eventually calls it sits inside the VPC with
a clear path to internal services and the metadata endpoint
(`169.254.169.254`). `validate_callback_url` exists to stop a client-chosen URL
from becoming an SSRF pivot, and a browser is not a trusted position for that
decision: anyone holding a stolen session token could choose the callback URL
themselves. Submission, redelivery and reupload are a design boundary, not a gap
to close later.

---

## API

Interactive docs at `/docs`. The examples below send no credential, matching
the shipped configuration — see [Access control](#access-control). Both
credential types are still declared as OpenAPI security schemes, so a client
Dharini generates from the schema already knows they exist and the `/docs` page
already has an **Authorize** button; the day you set `API_KEYS`, adding
`-H 'X-API-Key: …'` is the only change to everything below.

### Discover pipelines

```bash
curl localhost:8000/v1/pipelines
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
  -H 'Content-Type: application/json' \
  -d '{
        "pipeline_id": "ont-lsdv",
        "external_id": "dharini-job-8831",
        "owner_id": "u17",
        "input_key": "app-data/genomic-sequencing/uploads/u17/2f8c/reads.fastq.gz",
        "extra_input_keys": {
          "primer_bed": "app-data/genomic-sequencing/uploads/u17/2f8c/scheme.bed"},
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
  under the configured `…/uploads/` prefix, an accepted extension, and confirmed with a
  `HeadObject` call to actually exist and sit within `MAX_INPUT_BYTES` — all
  in milliseconds. A typo'd key is a `422` immediately, not a run that
  queues, waits behind three others, and dies twenty minutes later.

Once `API_KEYS` is set, this route accepts **only** a service credential, never
a browser token — see [Access control](#access-control) for why that is a
design boundary rather than a gap.

### Check status

```bash
curl localhost:8000/v1/runs/<run_id>
```

Includes `progress` (e.g. `"5/10 · variant"`), per-stage timings and peak
memory, artifact metadata, metrics, and webhook delivery state.

### Watch it run

`GET /v1/runs/{id}` is the wrong shape to poll every few seconds — it drags
`params`, artifacts and delivery history along for the ride. This endpoint is
built for that. Dharini proxies a browser's poll to it today, and it is the
route a browser would call directly once Bearer auth is on:

```bash
curl localhost:8000/v1/runs/<run_id>/progress
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
curl localhost:8000/v1/queue
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

`running` names other users' run ids, so it is **omitted entirely** (not merely
`null`) under a Bearer token — queue depth and expected wait aren't sensitive,
another user's runs are. Do not forward it to a browser verbatim. The same `estimated_wait_s` figure comes
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
| `GET /v1/system` | either | bucket, prefixes, pipeline versions, resolved image digests, limits |
| `GET /health` | open | liveness, disk, schema revision, storage reachability — nothing that names the bucket |

The **Credential** column describes the rules once `API_KEYS` is set. With auth
off, "either" and "service only" both mean *no credential required*.

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
                   "bucket": "your-bucket-name",
                   "key": "app-data/genomic-sequencing/runs/<run_id>/annotated.vcf" } ],
  "artifacts_uploaded": true,
  "artifacts_error": null,
  "output": { "bucket": "your-bucket-name",
              "prefix": "app-data/genomic-sequencing/runs/<run_id>/" }
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
[Deployment](docs/deploy-aws-ec2.md)). The webhook still fires in that case — the run's
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

- `GET /v1/runs?status=SUCCEEDED` — reconcile anything missed
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
2. **Never call this service from a browser.** Not because of a credential —
   there isn't one yet — but because the callback URL, `owner_id` and
   `input_key` in a submission are all trust-sensitive, and a browser cannot be
   trusted to choose them. Proxy progress polls through Dharini. When
   `X-API-Key` is switched on it is a full-access credential and belongs
   server-side only.
3. **Reject an `X-Dharini-Timestamp` older than ~5 minutes**, as the NestJS
   example above does. The signature covers the timestamp, but that only
   stops a *tampered* replay — freshness is what stops a *recorded* one from
   being replayed verbatim.
4. **Mint the presigned GET users actually download from.** This service
   serves artifacts only for operational debugging
   (`/artifacts/{filename}`) — it is not the frontend's result server, and
   streaming a 3 GB BAM back through it wastes the bandwidth the direct-to-S3
   design exists to save.
5. **Run a reconciliation sweep.** Any run submitted more than roughly an
   hour ago with no terminal status is worth a `GET /v1/runs/{id}` — this is
   what makes an `ABANDONED` delivery recoverable without a human noticing.
6. **Handle `artifacts_uploaded: false`.** The run succeeded and the results
   are stranded; `POST /v1/runs/{id}/reupload` is the fix, not a resubmission
   of the run.

---

## Deployment

The service runs from a **published container image**; nothing is built or
cloned on the server. `deploy.sh <version>` pulls the image and the tool images,
restarts the stack, waits for `/health`, and rolls back if it does not come up.

- **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)** — install, configure, upgrade, roll back, and run migrations on any Docker host.
- **[docs/deploy-aws-ec2.md](docs/deploy-aws-ec2.md)** — the AWS setup: instance sizing, S3 bucket and policy, IAM, security group.

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

Which is also how you find out whether the instance is right — see
[1. The instance](docs/deploy-aws-ec2.md#1-instance) for the starting point and the two families
to avoid.

### Storage

`work/` is deleted as soon as a run ends — only files a pipeline explicitly
publishes survive, and even those are now local scratch, not the durable
copy. Once `publish` uploads them to S3, local disk under `$RUNS_ROOT` is
swept after `RETENTION_HOURS` (default **6** — down from 72 before artifacts
moved to S3; the 320 GB volume is no longer the constraint it used to be,
because S3 lifecycle rules own the long term, see
[Deployment](docs/deploy-aws-ec2.md)).

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
curl localhost:8000/v1/runs/<run_id> | jq '.error_stage, .error_message'
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

**No `.env` is needed for local work, and once you have one it is ignored.**
`.env.example` is for deploying to a server — it points `RUNS_ROOT` at
`/srv/dharini` and names a real bucket, prefix and credentials, none of which
should decide whether your tests pass. Copy it when you deploy, not when you
clone.

Two things enforce that, and it takes both. `docker-compose.yml` withholds
`env_file: .env` from the `dev` service — but that only controls the
container's *environment*, and `app/config.py` calls `load_dotenv()`, which
reads the *file*, off the bind-mounted checkout. So the dev service also sets
`DHARINI_IGNORE_DOTENV=1`, which is what actually stops it.

The suite then points itself at SQLite in a temporary directory, `make smoke`
sets `RUNS_ROOT` and `REFS_ROOT` to paths inside the checkout, and the dev
container picks `STORAGE_BACKEND=local` with `ALLOW_UNAUTHENTICATED=true` —
because `app/config.py` otherwise defaults to S3 and refuses to import without
a bucket and an API key, neither of which local work has.

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
`docker-compose.yml`. Nobody else needs any of this; it's the one integration
point `LocalStore` cannot stand in for.

You can also point a local checkout at a **real** S3 bucket, which is the other
thing `.env` credentials buy: put `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
`S3_BUCKET` and `S3_REGION` in a local `.env` and `docker compose up` behaves
exactly as it does on the server. Use a scratch bucket, not the production one —
`runs/` is written to, and the policy deliberately cannot delete what it
writes.

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

### `snippy-fasta` and AVX-512

For months this section said Apple Silicon could not run `snippy-fasta` and
could not be made to. That was wrong, and the way it was wrong is worth keeping.

**The symptom.** `vcfstreamsort` and `vcfuniq` died with SIGILL (exit 132)
partway through Snippy, right after `freebayes-parallel`. On a Mac that looks
exactly like an emulation limit, and it was diagnosed as one.

**The actual cause.** Those binaries were compiled with **AVX-512** —
EVEX-encoded, using `vpternlogd` and `%k1` mask registers. They raise SIGILL on
any CPU without it. Apple Silicon's x86 emulator has none, but neither does a
hosted CI runner, and neither does **AMD EPYC Milan**, which is the `m6a`
instance this service runs on. One cause, three "different" failures.

**Why the binaries were built that way.** They were not the version anyone
intended. `snippy=4.6.0` declares only `vcflib >=1.0.0_rc3` — no upper bound —
so conda resolved **vcflib 1.0.10**, whose published build carries AVX-512.
Every published **1.0.2** build is plain SSE2 with none, and 1.0.2 satisfies
snippy's constraint. Pinning it is the whole fix.

The old text also claimed snippy 4.6.0 pins `vcflib <=1.0.2` and that this made
an arm64 image unbuildable. It does not pin that; the constraint above is what
it actually declares.

**Verified.** With the pin, `snippy-fasta` runs end to end **on Apple Silicon**
and reports all three planted variants:

```text
snp_count      3
variant_count  3
by_effect      {'missense_variant': 2, 'synonymous_variant': 1}
SUCCEEDED in 16.3s
```

So a developer on a Mac can now run and change this pipeline directly, and CI
is a check rather than the only place it executes.

**The lesson is the one already in this file** — *pin every version, including
transitive ones.* This is the second time an unpinned transitive dependency has
broken a tool here. The first resolved `python-kaleido` to a major version that
removed a module NanoPlot imports. Both were invisible until something died at
runtime, and both were one pin away.

### How the dev container is wired

It sits behind a compose profile, so `docker compose up` never starts it. It
mounts this checkout **at its own host path** and mounts the Docker socket, for
the reason described under [Path parity](#-path-parity): the pipeline and the
container-backed tests start sibling containers with bind mounts, and the daemon
resolves a bind-mount source on the host. Parity is what makes one path string
valid on both sides. The `Makefile` passes that path as `DHARINI_REPO`.

`make images-push` is the one target that does **not** run in the container —
registry credentials never cross the boundary. See
[Build, test, publish to Docker Hub](#5-build-test-publish-to-docker-hub).

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
contract, the rules and a worked example. This is the shape of the work.

**A pipeline is one new directory under `app/pipelines/`** — a package
implementing `Pipeline`, plus a `manifest.yaml` declaring the container images
it needs. `app/pipelines/__init__.py` discovers every subpackage with `pkgutil`
at startup and each one registers itself, so **no existing file changes**.

```text
app/pipelines/your_pipeline/
├── __init__.py        registers the pipeline with the registry
├── pipeline.py        id, version, stages, params_schema, extra_inputs, run(ctx)
├── specs.py           one StageSpec per tool: image, cpus, memory_gb, timeout_s
├── manifest.yaml      the images this scope declares
└── docker/            Dockerfiles, only if you need a custom image
```

### What you do *not* have to touch

This is the part worth internalising before you start, because most of the
instinct to edit these is wrong:

| | Why not |
| --- | --- |
| The API | `POST /v1/runs` takes `pipeline_id` and dispatches through the registry. It has no per-pipeline knowledge to add. |
| The database | Runs, stages, artifacts and metrics are pipeline-agnostic. No migration. |
| **Dharini** | It builds its parameter form from `params_schema` in `GET /v1/pipelines`. Declare a parameter and the form grows a field, with no frontend deploy. |
| `fetch` / `publish` | The platform prepends and appends them. Your `stages` list names only your own. |
| S3 | `ctx.artifact()` is the whole interface. A pipeline never learns object storage exists. |

### The loop

```bash
make params PIPELINE=your-pipeline           # does the schema say what you meant?
make check                                   # contract + manifest validation (~1s)
make smoke PIPELINE=your-pipeline INPUT=fixtures/your-input --keep
make test                                    # contract tests are parametrised per pipeline
```

`make smoke` builds the **same** `RunContext` the Celery worker builds, applies
the same schema validation and runs the same stages in the same tool
containers. It skips only Postgres, Redis, the queue, HTTP and S3 — the parts
you are not debugging. A green smoke run means the pipeline works, not that a
test harness is happy.

### Shipping it

```bash
# only if you added a custom image -- see the next section
make images-push && make images-lock

git commit -am "feat(pipelines): add your-pipeline" && git push
```

Open a pull request. After it merges, CI publishes a new application image
(`sha-<commit>`, or a release tag), which contains your pipeline and the lock
file. Then, on the server:

```bash
./deploy.sh <version>
curl -s localhost:8000/v1/pipelines | jq '.[].id'   # the new id is listed
```

Dharini needs no deploy. It reads the new pipeline, its stages and its
parameter schema from that endpoint.

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

### 5. Build, test, publish to Docker Hub

Images live under the **`dsihartpark`** organisation on
[Docker Hub](https://hub.docker.com/u/dsihartpark). A contract test fails the
build if a manifest declares a repository under any other namespace, so a
personal account cannot reach production by accident.

**The normal path is CI, and you should prefer it.**
`.github/workflows/images.yml`, run by a maintainer (Actions → images → Run
workflow) on the pull-request branch after a change to `shared/**`,
`app/pipelines/*/manifest.yaml`, `app/pipelines/*/docker/**` or
`tools/images.py`. It builds, pushes, regenerates `images.lock.json` and commits
the lock back to the branch. It authenticates from the `DOCKERHUB_USERNAME` and
`DOCKERHUB_TOKEN` secrets of the `images` environment, which waits for a
maintainer's approval on every run — an **access token**, created under
*Docker Hub → Account Settings → Personal access tokens* with Read & Write
scope, never an account password.

So in practice: dispatch the `images` workflow on your pull-request branch
(Actions → images → Run workflow) so the refreshed lock lands in the PR, then
merge. It is deliberately not triggered by the pull request itself, since that
would hand the registry credentials to forks.

**Pushing by hand**, when you need the image before a merge:

```bash
docker login -u <your-dockerhub-user>           # once, on the host

make images-build SCOPE=<your-pipeline-scope>   # or omit SCOPE for everything
make smoke INPUT=fixtures/reads.fastq.gz        # prove it actually runs first
make images-push                                # docker push, on the host
make images-lock                                # resolve digests → images.lock.json
git add images.lock.json && git commit -m "chore(images): add your-tool"
```

Three things about that sequence:

- **`make images-push` is the one target that does not run inside the dev
  container.** `docker login` stores credentials through a helper backed by the
  OS keychain, so a container mounting only the Docker socket authenticates as
  nobody and the push fails with `insufficient_scope: authorization failed` —
  which reads like an account problem rather than a missing credential. Walking
  the manifests still happens in the container; only `docker push` runs on the
  host.
- **Push before you lock.** `images-lock` resolves each tag to the digest the
  *registry* reports. Locking an image that was never pushed records your local
  build id, and every other machine then fails to pull it.
- **Commit `images.lock.json`.** An image that is built but not pushed, or
  pushed but not locked, works on your laptop and nowhere else — and that
  failure surfaces in production rather than in CI.

**Then deploy it.** Digests are pinned, so a new image does not reach the server
until a version that contains the new lock is deployed. `deploy.sh` pulls the
tool images at the digests that version pins:

```bash
./deploy.sh <version>
```

With `REQUIRE_PINNED_IMAGES=true`, a manifest entry with no matching lock entry
refuses to start rather than silently resolving to a mutable tag. That is the
intended behaviour: a result you cannot trace to the image that produced it is
not a result.

---

## Known gaps

1. **Authentication is off, so the security group is the only access
   control.** `ALLOW_UNAUTHENTICATED=true` is a deliberate choice to get the
   service live, and the code to turn it off is written, tested and wired into
   every route. Until it is on, an inbound rule wider than Dharini's security
   group means an open API — see [Access control](#access-control).
2. **The API break is total, with no compatibility shim.** `POST /v1/runs` no
   longer accepts multipart at all, and the legacy `/upload`, `/status/{id}`
   and `/download/…` routes are gone. A Dharini deployment still speaking the
   old contract fails outright rather than degrading, so this version and the
   corresponding Dharini deploy have to ship together — see G9 in the design
   spec.
3. **Nothing deletes from S3.** No lifecycle rule, no versioning, and the
   service holds no `s3:DeleteObject` by design, so `uploads/` and `runs/`
   grow until something outside this service reclaims them. A cost item, not
   a correctness one — local disk is swept on `RETENTION_HOURS` regardless.
   See [Versioning and lifecycle](docs/deploy-aws-ec2.md#s3-versioning-and-lifecycle).
4. **`S3Store` is exercised locally and manually, not in CI.**
   `tests/test_storage_s3.py` runs against the `minio` dev-profile service and
   is skipped whenever `S3_ENDPOINT_URL` is unset — which is always true in
   CI today, since no `minio` service is wired up there. The backend that
   actually handles production traffic is therefore the one with the least
   automated coverage; a real multi-GB `ont-lsdv` upload (`keep_alignment:
   true`) on staging is the check that stands in for it before a deploy.
5. **No real LSDV primer scheme exists in this repository.** Primer trimming is
   implemented and verified against a *synthesised* scheme, and a scheme whose
   contigs do not match the reference is now rejected outright. But no run has
   ever used a genuine LSDV amplicon design, because nobody has supplied one.
   Until then, leave `primer.enabled` false.
6. **`snippy-fasta`'s fix reaches a host only after the next deploy.** The
   AVX-512 crash is diagnosed, fixed by pinning `vcflib=1.0.2`, and verified
   end to end — see
   [`snippy-fasta` and AVX-512](#snippy-fasta-and-avx-512). The rebuilt image
   (`4.6.0-2`) is published by the `images` job of the next deploy, which also
   refreshes `images.lock.json`. Until that runs, a deployed host still has
   `4.6.0-1` and still fails on any CPU without AVX-512 — the resolver warns
   loudly and falls back to the tag rather than silently running the old
   image.
7. **Variant calls have only been checked against synthetic reads.** All three
   ONT callers report exactly the planted substitutions, which proves the
   invocations, the plumbing and the resource limits. It proves nothing about
   accuracy on real basecalled data — in particular the Medaka model must match
   the basecaller that produced the reads, and a mismatch degrades calls
   silently.
8. **The worker mounts the Docker socket**, which is root-equivalent access to
   the host. Acceptable only because no client input ever reaches a shell — do
   not add a code path that changes that.

---

## License

Released under the [Apache License 2.0](LICENSE); see [NOTICE](NOTICE). The tools
this service orchestrates run in their own containers under their own licences,
and **Medaka is not OSI-approved** (optional, never the default). See
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

If you fork, publish tool images under your own Docker Hub namespace and change
the `dsihartpark/` prefix in the manifests and in `tests/test_images.py`.
