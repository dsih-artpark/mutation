# Dharini integration — implementation brief

Hand this to whoever (or whatever) implements the Dharini side. It is written
to be pasted to an agent as-is.

Every field name below was verified against the running pipeline service, not
transcribed from a design document.

---

## Context

Dharini (NestJS) is the frontend and the start of the flow. The pipeline
service behind it has been rewritten: it no longer accepts file uploads. A file
now goes to S3 first, and Dharini submits the **object key**.

This is a **breaking change with no compatibility shim.** `POST /upload`,
`GET /status/{job_id}`, `GET /download/...` and `GET /visualization/{run_id}`
have been deleted from the pipeline service. Both sides must deploy together.

**The single most important rule: the pipeline service never uploads to S3 on
the input side and never issues a presigned URL.** Dharini owns all S3 write
credentials for `uploads/`, and Dharini mints every download link. The pipeline
service holds a credential that can only `GetObject` under `uploads/` and
`PutObject` under `runs/` — no `ListBucket`, no `DeleteObject`, no read of
`runs/*`. Do not design anything that assumes otherwise.

---

## What Dharini must implement

### 1. Upload

Browser picks a FASTA/FASTQ. Dharini mints a presigned `PUT` for a key **it**
chooses, under the `uploads/` prefix, and the browser PUTs directly to S3.

```
uploads/<user-id>/<upload-id>/<filename>
```

The pipeline service validates that every submitted key starts with `uploads/`
and rejects `..`, leading `/`, empty segments, control characters and keys over
1024 bytes. Keep the layout inside that prefix.

Bytes must never pass through the NestJS process. A 1.2 GB FASTQ streamed
through Node is a timeout, not a feature.

### 2. Submit the run

```http
POST {PIPELINE_URL}/v1/runs
Content-Type: application/json
X-API-Key: {PIPELINE_API_KEY}          # see "Auth is currently OFF" below
```

```jsonc
{
  "pipeline_id":      "ont-lsdv",              // from GET /v1/pipelines
  "external_id":      "dharini-job-8831",      // REQUIRED. See below.
  "owner_id":         "u17",                   // which Dharini user owns this
  "input_key":        "uploads/u17/2f8c/reads.fastq.gz",
  "input_filename":   "Sample_42_reads.fastq.gz",   // optional, display only
  "extra_input_keys": { "primer_bed": "uploads/u17/2f8c/scheme.bed" },
  "params":           { "variant": {"tool": "clair3"}, "threads": 4 },
  "callback_url":     "https://dharini.example.org/api/pipeline/webhook"
}
```

`202` on success:

```jsonc
{
  "run_id": "2b6c721b-…", "external_id": "dharini-job-8831",
  "pipeline_id": "ont-lsdv", "pipeline_version": "1.1.0",
  "status": "QUEUED",
  "stages": ["fetch","prepare","qc","align","primer","variant",
             "consensus","annotate","report","publish"],
  "queue_position": 0, "estimated_start_s": 0
}
```

**`external_id` is not optional and must be generated BEFORE the request is
sent.** It carries a unique constraint. Resubmitting the same one returns `200`
with the *original* run instead of starting a second — which is what makes a
timeout-and-retry safe, since an ONT run is roughly half an hour of compute.
It is also how a run is correlated when the `202` response itself is lost:
Dharini already knows the id, so the eventual webhook can be matched even
though the `run_id` was never received. Persist the `external_id` → job row
**before** calling, not after.

Other responses: `404` unknown `pipeline_id`; `413` input over the size limit;
`422` anything else (bad params, rejected callback URL, malformed key, missing
object). `422` bodies carry a `detail` explaining which check failed.

### 3. Render the parameter form from the service

```http
GET {PIPELINE_URL}/v1/pipelines
```

Each pipeline returns `params_schema` (JSON Schema, Draft 2020-12), `stages`,
`accepted_extensions` and `extra_inputs`. **Render the form from this, do not
hardcode fields.** Adding a pipeline is meant to be a backend-only change, and
hardcoding here is what breaks that property.

`extra_inputs` describes secondary files (e.g. ont-lsdv's ARTIC `primer_bed`),
each with `extensions`, `required` and a `title`/`description`. Render a file
picker per entry and send the resulting keys in `extra_input_keys`.

### 4. Status and progress

Two endpoints, different shapes, different purposes.

```http
GET {PIPELINE_URL}/v1/runs/{run_id}            # full record
GET {PIPELINE_URL}/v1/runs/{run_id}/progress   # small, built for polling
```

`/progress` is the one to poll every 3–5 seconds:

```jsonc
{
  "run_id": "…", "external_id": "dharini-job-8831",
  "status": "RUNNING",
  "stage": "variant", "stage_index": 5, "stage_total": 10,
  "stage_started_at": "2026-08-20T18:41:02Z",
  "stage_elapsed_s": 754,
  "stage_typical_s": 1180,      // median of past runs; null with no history
  "heartbeat_age_s": 1.8,       // null when not running
  "eta_s": 1640, "eta_at": "2026-08-20T19:21:22Z",
  "queue_position": null,       // set only while QUEUED
  "estimated_start_s": null,
  "stages": [ {"name": "fetch", "status": "SUCCEEDED", "duration_s": 25.1}, … ]
}
```

`GET /v1/runs/{run_id}` additionally carries `params`, `metrics`, per-stage
peak memory, artifacts and webhook delivery state. Use it for a detail page,
not for a poll loop.

**Expose your own `GET /status/:runId` in Dharini** backed by these — the
frontend should talk to Dharini, not to the pipeline service directly, unless
you deliberately enable browser tokens (see below).

**Do not draw a percentage progress bar.** Clair3, Medaka and minimap2 emit
nothing machine-readable, so there is no honest sub-stage percentage. The
service deliberately refuses to invent one. The truthful UI is:

> Variant calling · 12m elapsed · typically ~20m

When `stage_typical_s` is `null` (a pipeline with no run history yet), show
elapsed time only. When `heartbeat_age_s` exceeds ~30s, show "no signal" — not
"failed"; the run is probably fine and the sampler stalled.

### 5. Queue

```http
GET {PIPELINE_URL}/v1/queue
```

```jsonc
{ "worker_concurrency": 1, "queued": 3, "estimated_wait_s": 5200,
  "running": [ {"run_id": "…", "pipeline_id": "ont-lsdv",
                "stage": "variant", "eta_s": 1640} ] }
```

`worker_concurrency` is **1**. A second submission genuinely waits for the
first to finish — roughly half an hour for ONT. Surface this, or users will
read a queued run as a broken one. `estimated_start_s` in the submit response
and in `/progress` is the same figure.

### 6. Receive the webhook

```http
POST {your callback_url}
X-Dharini-Event:     run.completed | run.failed
X-Dharini-Delivery:  <uuid>            ← idempotency key
X-Dharini-Timestamp: <unix seconds>
X-Dharini-Signature: sha256=<hmac>
```

```jsonc
{
  "event": "run.completed", "delivery_id": "…", "attempt": 1,
  "timestamp": "2026-08-20T19:21:22Z",
  "run": { "id": "…", "external_id": "dharini-job-8831", "owner_id": "u17",
           "pipeline_id": "ont-lsdv", "pipeline_version": "1.1.0",
           "status": "SUCCEEDED", "duration_s": 412.7, "params": {…},
           "input_filename": "…", "input_bytes": 1234,
           "created_at": "…", "started_at": "…", "finished_at": "…",
           "error": null },
  "metrics":  { "variant_count": 37, "snp_count": 31, … },
  "stages":   [ {"name":"align","status":"SUCCEEDED","duration_s":84.2,
                 "peak_memory_mb":2140}, … ],
  "variants": [ {"chrom":"LSDV","pos":1042,"ref":"A","alt":"G",
                 "effect":"missense_variant","impact":"MODERATE",
                 "gene":"LSDV008","hgvs_p":"p.Lys34Glu",
                 "depth":120,"allele_freq":0.98}, … ],
  "variants_truncated": false, "variants_total": 37,
  "artifacts": [ {"kind":"vcf","label":"Annotated variants",
                  "filename":"annotated.vcf",
                  "bucket":"dharini","key":"runs/<run_id>/annotated.vcf",
                  "size_bytes":48210,"content_type":"text/plain"} ],
  "artifacts_uploaded": true,
  "artifacts_error": null,
  "output": { "bucket": "dharini", "prefix": "runs/<run_id>/" }
}
```

Four things the receiver **must** do:

1. **Verify the signature.** HMAC-SHA256 over the exact bytes
   `"{X-Dharini-Timestamp}." + rawBody`, keyed with the shared `WEBHOOK_SECRET`,
   compared in constant time. In NestJS this means capturing the **raw body**
   before JSON parsing — a re-serialised body will not match.
2. **Reject a timestamp older than ~5 minutes.** The signature covers the
   timestamp, but replay protection does not exist unless the receiver enforces
   freshness. This is the step most often skipped.
3. **Treat `X-Dharini-Delivery` as an idempotency key.** Retries are expected —
   the schedule spans about nine hours — and the same delivery id may arrive
   more than once.
4. **Handle `artifacts_uploaded: false`.** The run computed successfully but its
   artifacts are stranded on the worker. Show the metrics and variants (they are
   in the payload), mark the files unavailable, and call
   `POST /v1/runs/{id}/reupload` to retry.

Respond `2xx` quickly. Do the heavy work asynchronously; a slow handler burns
the delivery timeout and triggers a retry.

Store `variants` immediately — they are parsed, queryable rows, and this is why
they are inline. Do **not** expect file bytes in the payload; a BAM can be
gigabytes and everything bulky is an S3 key.

### 7. Serve results

Mint a presigned `GET` for `{bucket, key}` when a user clicks. The pipeline
service serves no result files, and its local copies are deleted after 6 hours.
S3 is the durable store.

### 8. Reconciliation sweep

Every ~10 minutes: any run submitted over an hour ago with no terminal status →
`GET /v1/runs/{run_id}` and update. Roughly twenty lines.

This is not optional polish. After its retry budget a webhook delivery becomes
`ABANDONED`: the run succeeded and Dharini would otherwise never find out. This
sweep is what makes the system self-healing.

---

## Auth is currently OFF

The pipeline service is running with `ALLOW_UNAUTHENTICATED=true` and no
`API_KEYS`, so **send no credential for now**. Build the client so the header is
a single config value that is simply empty today:

```ts
headers: apiKey ? { 'X-API-Key': apiKey } : {}
```

When it is switched on, two credential types exist:

- `X-API-Key` — Dharini's **server** only, full access. Never ships to a browser.
- `Authorization: Bearer <Dharini JWT>` — a browser, **read-only**, and only for
  runs whose `owner_id` matches the token's `sub`. Requires the pipeline service
  to be configured with your JWKS URL (RS256 preferred), issuer and audience.

Submission stays server-only in both cases: `callback_url` is trust-sensitive,
and letting a browser choose it re-opens an SSRF hole.

Sending `owner_id` on every submission now costs nothing and is what makes
browser-scoped reads possible later without a backfill. **Do it from day one.**

⚠ **Because auth is off, the pipeline service is not reachable from the public
internet.** Its security group allows inbound `8000` only from Dharini's
security group. So every call must originate from Dharini's server — a browser
`fetch()` straight at `PIPELINE_URL` will not connect, and that is deliberate,
not a firewall to get opened. Proxy progress polls through a Dharini route.

---

## Endpoint migration

| Old | New |
|---|---|
| `POST /upload` (multipart) | `POST /v1/runs` (JSON, with `input_key`) |
| `GET /status/{job_id}` | `GET /v1/runs/{run_id}` or `/v1/runs/{run_id}/progress` |
| `GET /download/{id}/{file}` | presigned GET on `{bucket, key}` from the webhook |
| `GET /visualization/{run_id}` | the `visualization.html` artifact in S3 |
| `job_id` | `run_id` (and your own `external_id`) |
| status `PENDING/STARTED/SUCCESS/FAILURE` | `QUEUED/RUNNING/SUCCEEDED/FAILED/CANCELLED` |

---

## Configuration

| Name | Notes |
|---|---|
| `PIPELINE_URL` | base URL of the pipeline service |
| `PIPELINE_API_KEY` | leave EMPTY for now |
| `WEBHOOK_SECRET` | shared with the pipeline service; must match exactly |
| `S3_BUCKET` | one bucket, two prefixes: `uploads/` and `runs/` |
| AWS creds | Dharini needs `PutObject` on `uploads/*` and `GetObject` on `runs/*` |

The pipeline service's `WEBHOOK_ALLOWED_HOSTS` must include Dharini's hostname,
and the callback URL must resolve to a public address — a private-range address
is refused unless `WEBHOOK_ALLOW_PRIVATE_IPS=true` is set there.

---

## Do not

- Proxy result bytes through NestJS. Presign instead.
- Poll `GET /v1/runs/{id}` on a tight loop. Use `/progress`.
- Hardcode pipeline parameters. Render from `params_schema`.
- Generate `external_id` server-side after the call, or reuse one across runs.
- Parse or depend on the `runs/` key layout beyond what the webhook gives you.
- Expect any file content in the webhook body.
- Show a percentage progress bar.

---

## Definition of done

- [ ] Browser uploads directly to S3; no bytes through NestJS
- [ ] `external_id` generated and persisted **before** submitting
- [ ] `owner_id` sent on every submission
- [ ] Parameter form rendered from `GET /v1/pipelines`
- [ ] `/status/:runId` in Dharini, backed by `/v1/runs/{id}` + `/progress`
- [ ] Queue depth and estimated start shown to the user
- [ ] Webhook: raw-body HMAC verified, timestamp freshness enforced, delivery id deduped
- [ ] `artifacts_uploaded: false` handled with a `/reupload` path
- [ ] Downloads served by presigned GET
- [ ] Reconciliation sweep running on a timer
- [ ] Progress UI shows elapsed + typical, never a fabricated percentage
