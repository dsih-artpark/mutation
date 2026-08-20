# Object Storage, Authentication and Run Visibility

**Date:** 2026-08-19
**Status:** Draft — awaiting review
**Implements:** Phase 3 of `2026-08-04-multi-pipeline-architecture-design.md`,
plus three areas that spec does not cover: the output side, authentication, and
intra-run progress.

Inputs arrive from S3 and results are published to S3. The service holds no AWS
credentials on disk, cannot enumerate the bucket, cannot read back another run's
results, and cannot destroy anything. Every endpoint requires a credential. A
run reports where it is while it is running.

---

## 1. Problem

**Bytes move twice.** Dharini uploads a FASTQ through the browser, and today that
file is streamed a second time through `POST /v1/runs` as multipart form data. A
1 GB FASTQ holds a uvicorn connection for minutes, consumes local disk twice
over, and makes `MAX_UPLOAD_SIZE=5GB` a timeout problem rather than a limit.

**Results live on one disk.** `RunArtifact` records a bare filename, the webhook
hands Dharini a `PUBLIC_BASE_URL` link, and this host becomes a publicly
reachable file server whose 72-hour sweep is the only thing between a 320 GB
volume and a full one.

**There is no authentication at all.** Anyone who can reach port 8000 can submit
runs, read every run, and download any artifact. `CORSMiddleware` is configured
`allow_origins=["*"]`. Tolerable on localhost; indefensible on EC2.

**A running job is opaque.** `GET /v1/runs/{id}` reports `5/9 · variant` and then
nothing for twenty minutes. There is no liveness signal, no elapsed-in-stage, and
no estimate — so a healthy Clair3 stage and a wedged worker look identical.

**A queued job is worse.** `queue_position` is computed once at submission and
never updates. With `WORKER_CONCURRENCY=1`, the second of two submissions waits
half an hour with no way to learn that.

**No idempotency key.** A client timeout and retry starts a second run — on a
single box, an hour of duplicated Medaka compute.

Phases 0–1 of the parent plan have landed. Phase 2 has not, and does not block
this work.

---

## 2. Goals

- Bytes move once, directly between S3 and the box that needs them.
- No AWS access key exists on this host, in `.env`, or in any image.
- A compromise of this service cannot read another user's data or destroy any.
- Results are durable independent of this instance's disk and lifetime.
- Eight minutes of successful compute is never discarded because S3 blipped.
- Every endpoint requires a credential; the browser may read, only Dharini's
  server may write.
- A user watching a run always knows: which stage, how long it has been there,
  whether it is alive, and roughly when it will finish.
- A pipeline author's code does not change, and does not learn what S3 is.
- Every test except the Docker-marked integration tests, and
  `tools/run_local.py`, run with no S3 — not a real one, not a mocking library.

### Non-goals

- Phase 2 (`ctx.asset()`, scoped `REFS_ROOT`). Independent of storage.
- Multi-region, cross-account, or customer-managed buckets.
- Presigned URL issuance by this service (S1).
- Object deletion by this service (S3).
- Sub-stage progress percentages (S16).
- Rate limiting and quota enforcement. Dharini's edge owns that.

---

## 3. Decisions

Numbered `S*` to sit alongside the parent spec's `D*`.

| # | Decision | Rationale |
|---|---|---|
| **S1** | **Dharini uploads to S3 with its own credentials and sends the object key.** No `POST /v1/uploads`, no presigned URLs issued here. | Supersedes D6; sanctioned by §9.2. Issuing a presigned PUT requires the signer to *hold* `s3:PutObject` on `uploads/*`, so D6 would have made input access read-write. Dropping it is what makes read-only input real rather than nominal. |
| **S2** | **The worker writes artifacts straight to S3.** No proxy hop through Dharini. | The alternative moves every byte twice and puts NestJS in the path of a 3 GB BAM. It also buys nothing: whatever compromises this worker can equally call Dharini's upload endpoint. |
| **S3** | **Object deletion is an S3 lifecycle rule, never an API call.** | Supersedes §10.5. `s3:DeleteObject` on the instance role forfeits the no-destroy guarantee for a job AWS does for free and no bug of ours can trigger. |
| **S4** | **Credentials come from the EC2 instance role.** No static keys. | Rules out Lightsail (no instance profiles) and Fargate (cannot host sibling-container execution). Plain EC2 keeps `docker compose up` and path parity unchanged. |
| **S5** | **Bucket versioning is on.** | `PutObject` on an existing key *is* an overwrite and IAM cannot express "create but do not replace". Versioning makes the no-destroy property true and makes upload retries safe. |
| **S6** | **The client never invents a key.** Inputs validated against a configured bucket and prefix; outputs always `runs/<run_id>/`. | §9.2's principle. `external_id` already provides correlation, so a client-supplied output prefix would be attack surface buying nothing. |
| **S7** | **Storage is bookended around the Celery task**, not threaded through `RunContext`. | `app/storage.py` becomes the only module holding a boto3 client, mirroring `runtime/runner.py` being the only thing that starts a process. `Pipeline`, `RunContext` and `ctx.artifact()` do not change, so every pipeline works over S3 with no edits and `run_local` needs no stub. |
| **S8** | **Input download failure fails the run; artifact upload failure does not.** | Asymmetric because the costs are. Re-downloading is cheap; re-running Clair3 is not. Upload gets the retry machinery `WebhookDelivery` already has, and the same rule: a run that computed correctly is `SUCCEEDED` even when a later step cannot deliver it. |
| **S9** | **The completion webhook fires only after artifacts are in S3.** | The payload carries S3 keys. A webhook arriving before the objects exist hands Dharini keys that 404. |
| **S10** | **No file bytes in any webhook.** Parsed variant rows stay inline; everything else is a key. | Already the existing position (`WEBHOOK_MAX_INLINE_VARIANTS`). A multi-megabyte body that must survive six retries is not a delivery mechanism. |
| **S11** | **Alembic, adopted now.** | Supersedes §13's single hand-written migration, which was scoped to three columns; this phase adds fifteen. Run history is described in `models.py` as the point of a surveillance platform, and history you intend to keep needs migrations. |
| **S12** | **`ExtraInput` is pulled forward from Phase 4.** | The JSON body needs `extra_input_keys`, and validating one requires each extra's extensions and on-disk filename. Keeping `primer_bed` hardcoded means writing the endpoint twice. |
| **S13** | **`STORAGE_BACKEND=s3\|local`.** `LocalStore` implements the same Protocol over the filesystem. | The fast suite and `run_local` touch no S3 at all and need no mocking library. Only the Docker-marked integration tests need a real endpoint. |
| **S14** | **Two credentials with different powers**: `X-API-Key` (Dharini's server, full access) and `Authorization: Bearer <Dharini JWT>` (browser, read-only, owner-scoped). | One consumer needs to submit and administer; many browsers need to watch. A single credential type would either put a service key in the browser or force every read through NestJS. |
| **S15** | **Submission, redelivery and reupload are service-key only.** A browser token can never write. | `params`, `input_key` and above all `callback_url` are trust-sensitive. Letting a browser choose the callback URL re-opens the SSRF hole `validate_callback_url` exists to close, wearing a valid user's token. |
| **S16** | **JWTs are verified locally, never introspected over the network.** | An auth check that requires a round trip to Dharini fails when Dharini is down, and adds that latency to every progress poll. RS256 + JWKS is preferred over HS256 so this service holds only a public key and cannot mint tokens. |
| **S17** | **A run whose owner does not match returns 404, not 403.** | A 403 confirms the run exists. Enumeration of run ids should yield nothing. |
| **S18** | **Progress is reported by polling a dedicated endpoint, not by SSE, and never as a sub-stage percentage.** | Clair3, Medaka and minimap2 emit no machine-readable progress, so "62% through variant calling" would be fabricated. The truthful signal is stage + elapsed + a historical typical duration. SSE would hold a connection per watcher and NestJS would have to re-fan-out anyway. |
| **S19** | **ETAs come from the median of past runs of the same pipeline and stage, and are `null` when there is no history.** | `run_stages` already records `duration_s` for every stage of every run — the estimate is a query, not a model. A first run of a new pipeline reports elapsed time only; inventing a number would be worse than admitting ignorance. |
| **S20** | **Liveness is a heartbeat on the stage row, written by the stats sampler that already runs.** | `StageRunner` samples container stats every `STATS_SAMPLE_INTERVAL_S` for peak memory. Piggybacking a timestamp costs one throttled UPDATE and turns "silent for twenty minutes" into a distinguishable healthy-vs-wedged signal. |

---

## 4. Deltas from the approved spec

Recorded explicitly so the parent document is not silently contradicted.

| Parent ref | Approved | This phase | Decision |
|---|---|---|---|
| D6, §9.2 | Service issues presigned PUT via `POST /v1/uploads` | Dharini uploads; sends `input_key`. No such endpoint. | S1 |
| §9.1 | `ObjectStore` = `presign_put, head, download, delete` | `head, download, upload` | S1, S3 |
| §10.5 | `cleanup_expired_runs` deletes S3 objects | Lifecycle rules on the bucket | S3 |
| §11 | Artifacts served from `PUBLIC_BASE_URL` | Artifacts in S3; webhook carries `{bucket, key}` | S2 — new |
| §13 | One hand-written migration, Alembic later | Alembic now | S11 |
| §14 | Phase 2 precedes Phase 3 | Phase 3 without Phase 2 | Sequencing only |
| — | No authentication anywhere | Two credential types on every route | S14 — new |
| — | Progress is `n/m · stage` at completion granularity | Dedicated poll endpoint, heartbeat, ETA | S18–S20 — new |

Adopted unchanged from the parent: D15 (`external_id`), §9.2's validation ladder
and `S3_ALLOWED_PREFIX`, §10.1 (`ExtraInput`), §10.3 (`fetch` + preflight),
§11's three-mechanism response flow.

---

## 5. Flow

```
   BROWSER            DHARINI (NestJS)        S3                PIPELINE SERVICE
      │                     │                  │                    │
 ①    ├──"upload"──────────►│                  │                    │
      │◄──presigned PUT─────┤                  │                    │
 ②    ├──────── PUT 1.2 GB ────────────────────►│                    │
      │                     │  uploads/u17/2f8c/reads.fastq.gz      │
 ③    │                     ├─POST /v1/runs  X-API-Key────────────►│
      │                     │                  │◄─HeadObject────────┤
      │                     │◄──202 {run_id}───┤                    │
 ④    │                     │                  │◄─GetObject─────────┤  fetch    25s
 ⑤    │                     │                  │                    │  stages   28m
 ⑤ᵇ   ├─GET /v1/runs/{id}/progress  Bearer <JWT>────────────────────►│  (poll 3-5s)
 ⑥    │                     │                  │◄─PutObject×7───────┤  publish  40s
 ⑦    │                     │◄──run.completed (signed)──────────────┤
 ⑧    │                     │◄─presign GET─────┤                    │
      │◄──────── download ───────────────────► │                    │
```

Note ⑤ᵇ: the browser polls this service directly with the Dharini session token
it already holds. Submission (③) never leaves Dharini's server (S15).

---

## 6. Authentication and authorisation

Every route requires a credential except `GET /health` (load-balancer probe) and
`GET /` (service banner).

### 6.1 Service key

```
X-API-Key: sk_live_…
```

`API_KEYS` is a comma-separated list of `label:secret` pairs — `dharini:sk_live_…,ops:sk_live_…`
— so a request log names the caller and a key can be rotated by adding the new
one, deploying, migrating callers, then removing the old. Comparison uses
`hmac.compare_digest`, never `==`; a timing-variable comparison leaks the key one
byte at a time.

The resolved label is attached to the request and recorded on the run.

### 6.2 User token

```
Authorization: Bearer <JWT issued by Dharini>
```

Verified locally (S16). Configuration selects the algorithm:

| Mode | Config | Property |
|---|---|---|
| **RS256** *(preferred)* | `DHARINI_JWT_JWKS_URL` | This service holds only a public key. Compromising it cannot mint tokens. |
| HS256 | `DHARINI_JWT_SECRET` | One shared secret that both verifies and issues. Acceptable, weaker. |

Checked on every request: signature, `exp` (with `DHARINI_JWT_LEEWAY_S` clock
skew), `iss` == `DHARINI_JWT_ISS`, `aud` == `DHARINI_JWT_AUD`. `sub` becomes the
owner id. JWKS responses are cached in-process and refetched when an unknown
`kid` appears — so key rotation on Dharini's side needs no deploy here.

Bearer authentication is **disabled entirely** when neither `DHARINI_JWT_JWKS_URL`
nor `DHARINI_JWT_SECRET` is configured. A half-configured verifier that accepts
unsigned tokens is worse than no browser access.

### 6.3 What each credential may do

| Endpoint | `X-API-Key` | `Bearer` |
|---|---|---|
| `POST /v1/runs` | ✓ | ✗ 403 (S15) |
| `POST /v1/runs/{id}/redeliver` · `/reupload` | ✓ | ✗ 403 |
| `GET /v1/runs` | ✓ all | ✓ own only |
| `GET /v1/runs/{id}` | ✓ | ✓ own only |
| `GET /v1/runs/{id}/progress` | ✓ | ✓ own only |
| `GET /v1/runs/{id}/artifacts/{file}` | ✓ | ✓ own only |
| `GET /v1/pipelines` | ✓ | ✓ |
| `GET /v1/queue` | ✓ full | ✓ depth and wait only |
| `GET /health` · `GET /` | open | open |

"Own only" means `Run.owner_id == jwt.sub`. A mismatch is **404** (S17).
`GET /v1/queue` under a Bearer token returns counts and estimated wait but no
other user's run ids or pipeline mix.

### 6.4 Ownership

`Run.owner_id` is set at submission from the request body's `owner_id` field —
Dharini's server knows which of its users the run is for and tells us. It is
nullable: a run submitted by `ops` with no owner is visible to service keys only,
which is the correct behaviour for an operational re-run.

### 6.5 CORS

`allow_origins=["*"]` is removed. `CORS_ALLOWED_ORIGINS` is an explicit
comma-separated list, `allow_credentials=True`, and only the methods the browser
actually uses (`GET`, `OPTIONS`). An empty list disables CORS entirely, which is
the correct production value if no browser calls this service directly.

---

## 7. Submission contract

`POST /v1/runs`, `Content-Type: application/json`, `X-API-Key` required.
Multipart is removed.

```jsonc
{
  "pipeline_id":      "ont-lsdv",
  "external_id":      "dharini-job-8831",
  "owner_id":         "u17",
  "input_key":        "uploads/u17/2f8c/reads.fastq.gz",
  "input_filename":   "Sample_42_reads.fastq.gz",      // optional, display only
  "extra_input_keys": { "primer_bed": "uploads/u17/2f8c/scheme.bed" },
  "params":           { "variant": {"tool": "clair3"}, "qc": {"tool": "fastp"} },
  "callback_url":     "https://dharini.artpark.in/api/snp/webhook"
}
```

`params` is untouched by this work — same `params_schema`, same jsonschema
validation, same `GET /v1/pipelines` form rendering. Per-pipeline tool selection
already works through it and needs nothing new.

`input_filename` is metadata. The file on disk is normalised to
`pipeline.input_filename`, so a hostile name never reaches a tool command line.
When omitted, the key's basename is used for the extension check.

### 7.1 Validation, before anything is queued

Ordered cheapest-first, so the common mistakes cost no network call.

| # | Check | Failure |
|---|---|---|
| 0 | credential present and valid | 401 |
| 1 | `external_id` already present | **200** with the existing run — idempotent, not an error |
| 2 | pipeline exists | 404 |
| 3 | `params` against `params_schema` | 422 |
| 4 | `callback_url` — existing `validate_callback_url` SSRF check | 422 |
| 5 | every key well-formed and under `S3_ALLOWED_PREFIX` | 422 |
| 6 | every required `extra_inputs` entry present; no unknown keys | 422 |
| 7 | extension ∈ `accepted_extensions` / `extra_inputs[k].extensions` | 422 |
| 8 | `HeadObject` on each key — exists | 422, `{"message": "input object not found", "key": …}` |
| 9 | `HeadObject` size ≤ `MAX_INPUT_BYTES` | 413 |

Check 8 returns 422 rather than 404: the route exists and the request is
well-formed but unprocessable, and a 404 on a POST implies a missing endpoint.

Without step 8, a typo'd key is a run that queues, waits behind three others, and
dies twenty minutes later. With it, it is an error in milliseconds while the user
is still looking at the upload screen — the same reasoning that already puts
`params` validation ahead of enqueueing.

### 7.2 Responses

| Code | Meaning |
|---|---|
| 202 | new run accepted — `RunAccepted`, now including `external_id` and `estimated_start_s` |
| 200 | `external_id` already seen — the existing `RunAccepted` |
| 401 | missing or invalid credential |
| 403 | Bearer token on a write endpoint (S15) |
| 404 | unknown `pipeline_id` |
| 413 | an input exceeds `MAX_INPUT_BYTES` |
| 422 | any other validation failure |

`RunAccepted.stages` reports the effective list — `fetch` first, `publish` last —
so the denominator a client sees matches what will actually run.

---

## 8. Key layout, and why prefix checking is sufficient

```
s3://<S3_BUCKET>/
    uploads/…                     GetObject only.   Written by Dharini.
    runs/<run_id>/
        annotated.vcf
        consensus.fasta
        visualization.html
        run.json                  PutObject only.   Written by this service.
```

S3 keys are opaque strings, not filesystem paths: S3 does not normalise, so
`uploads/../runs/x` addresses a literally different object than `runs/x` and
cannot escape a prefix. `key.startswith(S3_ALLOWED_PREFIX)` is therefore the real
control. `..`, control characters, backslashes, `//` and keys over 1024 bytes are
rejected anyway — it costs nothing and keeps the rule legible to someone who has
not internalised that S3 is not a filesystem.

No local path is ever derived from a key. Destinations come from
`pipeline.input_filename` and `extra_inputs[k].filename`, both of which the
contract test already forbids from containing a path separator. Path traversal
via a crafted key is structurally impossible rather than defended against.

### 8.1 IAM

Attached to the EC2 instance role. No user, no access key, no secret.

```jsonc
{ "Effect": "Allow", "Action": "s3:GetObject",
  "Resource": "arn:aws:s3:::dharini/uploads/*" },
{ "Effect": "Allow", "Action": "s3:PutObject",
  "Resource": "arn:aws:s3:::dharini/runs/*" }
```

No `ListBucket`, no `DeleteObject`, no `GetObject` on `runs/*`, no wildcards.

Bucket versioning on (S5). Lifecycle rules expire `uploads/` after 7 days — an
input is consumed within minutes and kept only for resubmission — and transition
`runs/` to Glacier Instant Retrieval at 90 days. Both rules must carry a
**noncurrent version expiration** clause; without it, versioning turns retry
churn into unbounded storage growth.

The result: this host can read the one object it was handed and create new
objects under a run-scoped prefix. It cannot enumerate the bucket, cannot read
back another run's results, and cannot destroy anything. There is no key on the
filesystem to leak.

---

## 9. Storage interface

`app/storage.py` — the only module in the repository holding a boto3 client.

```python
@dataclass(frozen=True)
class ObjectInfo:
    key: str
    size: int
    etag: str
    content_type: str

class ObjectStore(Protocol):
    def head(self, key: str) -> ObjectInfo | None: ...
    def download(self, key: str, dest: Path, max_bytes: int) -> int: ...
    def upload(self, path: Path, key: str, content_type: str) -> None: ...
```

`S3Store` (boto3) and `LocalStore` (filesystem), selected by `STORAGE_BACKEND`.
boto3's managed transfer handles multipart automatically in both directions, so a
3 GB BAM needs no special case.

Errors are a small hierarchy — `KeyRejected`, `ObjectNotFound`, `ObjectTooLarge`,
`TransferFailed` — so the API maps them to status codes and the worker maps them
to retry-or-fail without either catching a bare `botocore.ClientError`.

`download()` enforces `max_bytes` **during** the write, not from the `HeadObject`
size, for the same reason `_save_upload` does today: the declared size is not
necessarily the delivered size.

The client is built once per process with `botocore.config.Config(retries=
{"mode": "adaptive", "max_attempts": 3})`, so transient throttling is absorbed
below our own retry layer rather than consuming one of its six attempts.

---

## 10. The `fetch` bookend

The platform prepends `fetch` and appends `publish` to every pipeline's declared
stages. `Pipeline.effective_stages` returns `("fetch", *self.stages, "publish")`
and is what `describe()`, `RunAccepted.stages` and `RunContext.declared_stages`
all use. Both names are reserved; `tests/contract.py` already rejects `fetch` and
gains `publish`.

This is not cosmetic. A 90-second download and a four-minute BAM upload are
otherwise invisible, and a run reporting `0/8` for ninety seconds looks hung.

Inside `fetch`, before any transfer, **preflight** (§10.3 of the parent): every
image the pipeline resolved is present, every asset it declares is on disk. A
missing image fails the run in under a second with an actionable message instead
of dying at stage five with a confusing Docker error. `PreflightFailed` subclasses
`StageError`, so it flows through the existing failure, recording and webhook
paths with no new code.

A failure anywhere in `fetch` fails the run (S8): `error_stage="fetch"`, and
Dharini resubmits at the cost of one download.

---

## 11. The `publish` bookend

`upload_run_artifacts(run_id)` — a Celery task, so it is independently retryable
and independently observable.

```
for artifact in run.artifacts where uploaded_at is null:
    store.upload(output/<filename>, f"{S3_OUTPUT_PREFIX}{run_id}/{filename}", content_type)
    artifact.s3_key, artifact.uploaded_at = …
write run.json to {S3_OUTPUT_PREFIX}<run_id>/run.json
run.artifacts_uploaded = True
→ _enqueue_delivery(run_id)
```

`uploaded_at` living on the artifact row is what makes retry cheap: if four of
five files landed and the BAM timed out, the retry skips the four. A separate
`ArtifactUpload` table would buy nothing — unlike webhooks, there is exactly one
destination per run.

On failure: `upload_attempts += 1`, `next_upload_attempt_at` set, rescheduled with
`S3_UPLOAD_BACKOFF_S`. The run stays `SUCCEEDED` throughout (S8) and **the webhook
is withheld** (S9).

On exhaustion: `artifacts_uploaded` stays false, `upload_error` is recorded, and
the webhook fires anyway carrying `artifacts_uploaded: false`. Dharini learns the
run computed and the results are stranded, rather than waiting forever.

**A run with no artifacts still goes through `publish`.** `execute_run` calls
`upload_run_artifacts` unconditionally — including for a run that failed at
`fetch` or mid-pipeline. With nothing to upload it writes `run.json`, sets
`artifacts_uploaded = True` and enqueues the delivery immediately. Making the
webhook conditional on there being artifacts is how a failed run silently
notifies nobody; the `publish` stage is therefore `SUCCEEDED` on a `FAILED` run,
which is accurate — the run failed, the reporting did not.

`run.json` is the same document as the webhook payload. It costs nothing, makes
the S3 prefix self-describing, and is the recovery path if a webhook is lost
permanently.

Two existing safety nets gain siblings, following the patterns already in
`tasks.py`:

- **`sweep_pending_uploads`** beside `sweep_pending_deliveries` — re-arms uploads
  whose retry task was lost to a Redis flush or a worker killed mid-countdown.
- **`POST /v1/runs/{id}/reupload`** beside `/redeliver` — manual recovery for an
  outage longer than the retry schedule.

---

## 12. Webhook payload

Unchanged except where noted. `run`, `metrics`, `stages` and `variants` keep their
existing shapes, so Dharini's handler barely moves.

```jsonc
{
  "event": "run.completed",
  "delivery_id": "…", "attempt": 1,
  "run": { "id": "…", "external_id": "dharini-job-8831",
           "owner_id": "u17", … },                             // + external_id, owner_id
  "metrics": { … },                                             // unchanged
  "stages":  [ … ],                                             // unchanged
  "variants": [ … ], "variants_truncated": false,               // unchanged
  "artifacts": [
    { "kind": "vcf", "label": "Annotated variants",
      "filename": "annotated.vcf",
      "bucket": "dharini", "key": "runs/<run_id>/annotated.vcf",
      "size_bytes": 48210, "content_type": "text/plain" }       // url → bucket+key
  ],
  "artifacts_uploaded": true,                                   // new
  "artifacts_error": null,                                      // new
  "output": { "bucket": "dharini", "prefix": "runs/<run_id>/" } // new
}
```

Signing, headers, backoff, the SSRF allowlist and `sweep_pending_deliveries` are
untouched.

---

## 13. Progress, heartbeat and ETA

### 13.1 The endpoint

`GET /v1/runs/{id}/progress` — small, fixed-size, built for a 3–5 second poll.
`GET /v1/runs/{id}` remains the full record; it drags `params`, artifacts and
deliveries with it and is the wrong shape to poll.

```jsonc
{
  "run_id": "…", "external_id": "dharini-job-8831",
  "status": "RUNNING",
  "stage": "variant", "stage_index": 5, "stage_total": 9,
  "stage_started_at": "2026-08-19T18:41:02Z",
  "stage_elapsed_s": 754,
  "stage_typical_s": 1180,          // median of past runs; null with no history
  "heartbeat_age_s": 1.8,           // null when not running
  "eta_s": 1640, "eta_at": "2026-08-19T19:21:22Z",
  "queue_position": null,           // set only while QUEUED
  "estimated_start_s": null,
  "stages": [ {"name": "fetch", "status": "SUCCEEDED", "duration_s": 25.1},
              {"name": "qc",    "status": "SUCCEEDED", "duration_s": 118.4}, … ]
}
```

The `stages` array is nine short objects — under a kilobyte — and is what lets the
UI render a step indicator without a second request.

### 13.2 Heartbeat

`RunStage.last_heartbeat_at`, written by the stats sampler `StageRunner` already
runs every `STATS_SAMPLE_INTERVAL_S` for peak memory (S20), throttled to one
UPDATE per `HEARTBEAT_INTERVAL_S` (default 10 s). At `WORKER_CONCURRENCY=1` that
is 0.1 writes per second.

`heartbeat_age_s` is what distinguishes a healthy twenty-minute Clair3 stage from
a wedged worker. A UI should treat `> 3 × HEARTBEAT_INTERVAL_S` as "no signal",
not as failure — the run may still be fine and the sampler merely stalled.

### 13.3 Estimates

`stage_typical_s` is the **median** `duration_s` over `SUCCEEDED` rows in
`run_stages` with the same stage name, joined to runs of the same `pipeline_id`,
limited to the most recent `ETA_HISTORY_RUNS` (default 50). Median, not mean: one
pathological forty-minute alignment should not move the estimate for everyone.

`eta_s` = `max(0, typical_current − elapsed_current)` + Σ `typical` of the stages
not yet started.

**Both are `null` when there is no history** (S19). A first run of a new pipeline
reports elapsed time and nothing else. Inventing a number would be worse than
admitting ignorance, and the UI copy should read *"Variant calling · 12m
elapsed"* rather than showing a progress bar that means nothing.

Medians are cached in-process for `ETA_CACHE_TTL_S` (default 60 s); they move
slowly and the query is a group-by across two tables.

---

## 14. Queue visibility

`queue_position` in the `202` is a snapshot and never updates. Two additions.

`GET /v1/queue`:

```jsonc
{
  "worker_concurrency": 1,
  "running":  [ {"run_id": "…", "pipeline_id": "ont-lsdv",
                 "stage": "variant", "eta_s": 1640} ],   // service key only
  "queued": 3,
  "estimated_wait_s": 5200      // for a run submitted right now
}
```

Under a Bearer token the `running` array is omitted — depth and wait are not
sensitive, another user's run ids are.

`estimated_wait_s` = remaining ETA of the running run + Σ typical total duration
of each queued run's pipeline, divided by `WORKER_CONCURRENCY`. `null` when any
of those pipelines has no history.

The same figure appears as `estimated_start_s` in the submission `202` and in the
progress endpoint while a run is `QUEUED`, so the frontend can say *"3 ahead of
you · about 1h 25m before this starts"* rather than an unqualified "queued".

---

## 15. Data model

```python
Run:
    external_id            String(128)  unique, indexed, not null
    owner_id               String(128)  indexed, nullable
    submitted_by           String(64)   nullable        # API key label
    input_key              Text
    extra_input_keys       JSON         default {}
    output_bucket          String(255)  nullable
    artifacts_uploaded     bool         default False
    upload_attempts        int          default 0
    upload_error           Text         nullable
    next_upload_attempt_at DateTime     nullable, indexed

RunStage:
    last_heartbeat_at      DateTime     nullable
    # + index (name, status) for the ETA median query

RunArtifact:
    s3_key                 Text         nullable
    uploaded_at            DateTime     nullable
```

`output_bucket` is recorded per run even though it comes from configuration:
`S3_BUCKET` may change, and a six-month-old run's objects stay where they were
written. A webhook payload rebuilt at redelivery time must name the bucket the
files are actually in, not today's.

`input_key` and `extra_input_keys` become real columns, replacing the current
practice of stashing `_input_name` and `_extra_inputs` inside the `params` JSON
column and stripping reserved keys later (§10.5). `input_filename` and
`input_bytes` already exist and keep their meaning.

### 15.1 Migration

`init_db()`'s `create_all` creates missing tables; it will not add a column to an
existing one. Adopting Alembic (S11):

1. `alembic init`, configured to read `DATABASE_URL` from `app.config`.
2. Autogenerate a **baseline** revision from the current models, stamped on
   existing databases with `alembic stamp` so no DDL runs against them.
3. One revision adding the columns above, backfilling `external_id` for historical
   runs and creating its unique index.
4. `init_db()` becomes `alembic upgrade head`, run once at API startup.

**`external_id` is `NOT NULL` with a unique index, and historical rows have none.**
The revision backfills them with the run's own id before applying the constraint —
every existing run then has a unique, stable `external_id` equal to its `run_id`,
which is correct in the only sense that matters: nothing will ever resubmit it.

---

## 16. Retention

`cleanup_expired_runs` gains one guard, mirroring the one already there for
undelivered webhooks: **a run whose artifacts are not in S3 is never purged.**
Deleting local files for a run whose upload was abandoned would destroy the only
copy of a successful result.

Once artifacts are uploaded, local disk is disposable, so `RETENTION_HOURS` drops
from 72 to **6**. The 320 GB volume stops being the constraint it is today, and
S3 lifecycle rules (S3) own long-term retention.

---

## 17. Configuration

| Variable | Default | Notes |
|---|---|---|
| `STORAGE_BACKEND` | `s3` | `local` for tests and `run_local` |
| `S3_BUCKET` | — | required when backend is `s3` |
| `S3_REGION` | — | |
| `S3_ALLOWED_PREFIX` | `uploads/` | client-supplied keys must sit under it |
| `S3_OUTPUT_PREFIX` | `runs/` | server-generated; never client-supplied |
| `S3_ENDPOINT_URL` | *empty* | MinIO in dev; empty in production |
| `S3_SSE` | `AES256` | or `aws:kms` |
| `S3_KMS_KEY_ID` | *empty* | required when `S3_SSE=aws:kms` |
| `LOCAL_STORAGE_ROOT` | `$RUNS_ROOT/_objects` | `LocalStore` only |
| `MAX_INPUT_BYTES` | 5 GB | replaces `MAX_UPLOAD_SIZE` |
| `S3_UPLOAD_MAX_ATTEMPTS` | 6 | |
| `S3_UPLOAD_BACKOFF_S` | `[10, 60, 300, 1800, 7200, 21600]` | matches the webhook schedule |
| `API_KEYS` | *empty* | `label:secret` pairs. **Empty refuses to start unless `ALLOW_UNAUTHENTICATED=true`.** |
| `ALLOW_UNAUTHENTICATED` | `false` | local development only |
| `DHARINI_JWT_JWKS_URL` | *empty* | RS256, preferred |
| `DHARINI_JWT_SECRET` | *empty* | HS256 alternative |
| `DHARINI_JWT_ISS` / `_AUD` | *empty* | both required when Bearer auth is enabled |
| `DHARINI_JWT_LEEWAY_S` | `30` | clock skew |
| `CORS_ALLOWED_ORIGINS` | *empty* | explicit list; empty disables CORS |
| `HEARTBEAT_INTERVAL_S` | `10` | |
| `ETA_HISTORY_RUNS` | `50` | |
| `ETA_CACHE_TTL_S` | `60` | |
| `RETENTION_HOURS` | **6** | was 72 |

`AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` are deliberately absent. If the
process cannot obtain credentials from the instance role, that is a startup
failure, not a fallback.

**Startup refuses to run unauthenticated.** Empty `API_KEYS` without
`ALLOW_UNAUTHENTICATED=true` is a hard startup error, following the existing
`REQUIRE_PINNED_IMAGES` pattern: the insecure state must be chosen explicitly,
never reached by forgetting to set something.

`/health` gains a storage probe (backend, bucket, credentials resolvable) —
credentials failing at 02:00 on a run computing for forty minutes should not be
the first sign of a misconfiguration.

---

## 18. Removals

| Removed | Replacement |
|---|---|
| `app/api/legacy.py` entirely, its router registration, its `__init__` export, `tests/test_api.py:259-272` | `/v1/runs`, `/v1/pipelines`, S3 keys |
| `_save_upload`, `_stored_name` in `api/runs.py` | `storage.download` + `pipeline.input_filename` |
| `python-multipart` | no multipart endpoint remains |
| `MAX_UPLOAD_SIZE` | `MAX_INPUT_BYTES` |
| hardcoded `primer_bed` in the endpoint signature | `extra_inputs` / `extra_input_keys` (S12) |
| `allow_origins=["*"]` | `CORS_ALLOWED_ORIGINS` |

`generate_visualization` is called by the snippy-fasta pipeline itself
(`pipeline.py:132`), so deleting the legacy router does not affect it.
`GET /v1/runs/{id}/artifacts/{filename}` and `PUBLIC_BASE_URL` survive for
operational debugging but leave the webhook payload; Dharini stops depending on
them.

---

## 19. Testing

| Layer | Mechanism |
|---|---|
| Unit — everything below the API | `LocalStore`, no S3 at all (S13) |
| API — validation ladder | `LocalStore`, plus a fault-injecting subclass for miss / oversize / transfer-failure |
| Auth | Generated RSA keypair and HS256 secret in-process; no network |
| Integration | MinIO as a `dev`-profile compose service behind `S3_ENDPOINT_URL` |
| Contract | `publish` added to `RESERVED_STAGES`; `extra_inputs[*].filename` has no path separator |

New cases, each corresponding to a way this can go wrong:

- key outside `S3_ALLOWED_PREFIX`, containing `..`, over 1024 bytes → 422
- `HeadObject` miss → 422; oversize → 413; oversize found *during* download → FAILED
- duplicate `external_id` → 200 with the original run, **and no second Celery task**
- upload failure → run stays `SUCCEEDED`, webhook **not** sent
- upload retry skips artifacts with `uploaded_at` set
- upload exhaustion → webhook sent with `artifacts_uploaded: false`
- retention refuses a run with `artifacts_uploaded = False`
- `effective_stages` denominator matches the stages that actually execute
- **no credential → 401 on every `/v1` route; `/health` stays open**
- **Bearer on `POST /v1/runs` → 403 (S15)**
- **Bearer reading another owner's run → 404, not 403 (S17)**
- **expired, wrong-`iss`, wrong-`aud`, wrong-key and `alg:none` tokens → 401**
- **empty `API_KEYS` without `ALLOW_UNAUTHENTICATED` → startup error**
- progress ETA is `null` with no history and non-null with history
- heartbeat age rises when the sampler stops
- queue depth and `estimated_wait_s` track the queue
- Alembic: baseline autogenerate against a restored dump produces an empty diff

---

## 20. Build order

Dependency-ordered. Each step is independently testable and committable; the
breaking API change lands late, on machinery that already works.

| # | Step | Verifiable by |
|---|---|---|
| 1 | `app/storage.py`: `ObjectStore`, `S3Store`, `LocalStore`, key validation | unit tests; no other file touched |
| 2 | Config settings, dependencies, MinIO dev service, `/health` probe | `make test`, `curl /health` |
| 3 | `app/auth.py`: API key + JWT verification + CORS; applied to existing routes | auth tests; 401 on every `/v1` route |
| 4 | Alembic: init, baseline, stamp | empty autogenerate diff |
| 5 | `ExtraInput`, `Pipeline.extra_inputs`, `effective_stages`, contract update | contract test over both pipelines |
| 6 | Model columns + one revision | migration up and down on a copy |
| 7 | Ownership enforcement on read endpoints | 404-not-403 tests |
| 8 | `fetch` bookend + preflight | worker consumes S3 inputs; API still multipart |
| 9 | `publish` bookend, `upload_run_artifacts`, sweeper, `/reupload`, retention guard | upload retry tests |
| 10 | Heartbeat, ETA medians, `GET /v1/runs/{id}/progress`, `GET /v1/queue` | progress tests |
| 11 | Webhook payload change | `tests/test_webhooks.py` |
| 12 | `POST /v1/runs` JSON, delete legacy router, rewrite `tests/test_api.py` | full suite |
| 13 | `readme.md`, `docs/PIPELINE_AUTHORING.md`, `.env.example` | review |

Steps 1–11 are individually deployable. Step 12 is the cutover and must ship with
Dharini.

---

## 21. Dharini's side of the contract

Not this repository's work, but the design is not complete without it.

1. Upload to `s3://<bucket>/uploads/…` with its own credentials. This service
   never sees them and issues no presigned PUT (S1).
2. Hold the `X-API-Key` **server-side only**. It is a full-access credential.
3. Send `owner_id` on every submission so browser reads can be scoped (§6.4).
4. Generate a unique `external_id` **before** submitting, so a retry after a lost
   `202` is idempotent and correlation survives (D15).
5. Issue browser tokens with `iss`, `aud`, `exp` and `sub` matching this service's
   configuration. **RS256 preferred** so this service holds only a public key.
6. Verify `X-Dharini-Signature` — HMAC-SHA256 over `<timestamp>.<body>` — and
   **reject an `X-Dharini-Timestamp` older than ~5 minutes.** The signature covers
   the timestamp, but replay protection only exists if the receiver enforces
   freshness (§11 of the parent).
7. Treat `X-Dharini-Delivery` as the idempotency key; retries are expected.
8. Mint presigned GETs for users. This service serves no result files.
9. Run a reconciliation sweep — say every 10 minutes: any run submitted over an
   hour ago with no terminal status → `GET /v1/runs/{id}`. This is what makes an
   `ABANDONED` delivery recoverable.
10. Handle `artifacts_uploaded: false` — the run succeeded and results are
    stranded. `POST /v1/runs/{id}/reupload` is the recovery.

---

## 22. Risks and verification gates

Continuing the parent spec's numbering. Gates are blocking.

| ID | Risk | Gate |
|---|---|---|
| **G9** | The API break is total — no compatibility shim — so a lagging Dharini deploy strands every submission. | Both sides deployed together. One real end-to-end submission on staging, upload through webhook, before production. |
| **G10** | An IAM policy permitting the happy path can still be far wider than intended, and nothing in normal operation reveals it. | **Negative test from the instance:** `ListBucket` denied, `GetObject` on `runs/*` denied, `DeleteObject` denied. Passing the happy path is not the check. |
| **G11** | An Alembic baseline not matching the deployed schema corrupts on first upgrade. | Autogenerate against a restored production dump must produce an **empty** diff before the baseline is stamped. |
| **G12** | Multi-GB artifact upload is the least-exercised path and most expensive to get wrong. | One real `ont-lsdv` run with `keep_alignment: true`. Confirm the BAM lands, `run.json` is written, and the webhook fires **after** both. |
| **G13** | Versioning plus retry churn grows storage without bound if lifecycle rules cover only current versions. | Lifecycle configuration must include a **noncurrent version expiration** clause, verified by reading the bucket configuration back. |
| **G14** | `RETENTION_HOURS` 72 → 6 makes a bug in the upload guard data loss rather than inconvenience. | The test asserting `artifacts_uploaded = False` is never purged must exist and pass **before** the default is lowered. |
| **G15** | JWT verification is the classic place to get security wrong — `alg:none`, algorithm confusion, unverified signatures, missing `aud`. | Explicit negative tests for `alg:none`, an RS256 token verified against the HS256 secret, expired, wrong `iss`, wrong `aud`, and a token signed by an unrelated key. All must 401. |
| **G16** | Ownership checks are easy to add to the obvious endpoint and forget on the others. | A parametrised test asserting **every** `/v1/runs*` read route enforces owner scoping, driven by the router's own route table so a new route added later fails the test until handled. |
| **G17** | The heartbeat writes to Postgres from the stats-sampling thread; a session leak there degrades the worker slowly and invisibly. | Sampler uses its own short-lived session per write, and a test asserts pool size is stable across a simulated long stage. |

---

## 23. Outcome

| Property | Before | After |
|---|---|---|
| Bytes per input | 2 (browser→S3, S3→API→disk) | 1 |
| AWS credentials on host | n/a | none — instance role |
| Blast radius of host compromise | n/a | one object read; no enumeration, no destruction |
| Result durability | one EC2 volume, 72 h | S3, lifecycle-governed |
| Authentication | none | service key + owner-scoped user token |
| CORS | `*` | explicit allowlist, or off |
| Duplicate submit on retry | second run | idempotent |
| Visibility while running | `5/9 · variant`, then silence | stage, elapsed, liveness, ETA |
| Visibility while queued | a stale number | live position and estimated start |
| Cost of a transient S3 failure after compute | n/a | one retry, not one re-run |
| Pipeline author changes | — | none |

---

## 24. As built

Recorded after implementation so this document does not quietly disagree with
the code. Everything in §1–§23 landed as specified except the following.

### Corrections to the design

| Ref | Specified | Built | Why |
|---|---|---|---|
| §16 | "a run whose artifacts are not in S3 is never purged" | Scoped to runs with a non-null `input_key` | Every run predating this change has `artifacts_uploaded` false and will never be published. The rule as written exempted all of them from retention permanently — a disk leak that would have surfaced weeks after deploy, not at it. A pre-existing run keeps the contract it was created under. |
| §11 | `run.json` is the same document as the webhook payload | Same, but with `artifacts_uploaded` forced true and the delivery fields removed | The manifest is written before the run row commits, so reading the flag off that row made every manifest claim false while sitting beside the artifacts it had just published. It is only written after all artifacts land, so the value is true by construction. |
| §9 | `S3_SSE` always sent | Omitted when empty | AWS has encrypted new objects with SSE-S3 since January 2023, so the header is redundant there, and an S3-compatible store without KMS rejects it outright. Production still sets `AES256`. |

### Defects found during implementation

Each was found by a test, not by review. Listed because each is a class of
mistake worth watching for in the next phase.

1. **`boto3.exceptions.S3UploadFailedError` inherits from neither `ClientError` nor `BotoCoreError`.** `upload_file` wraps failures in it, so catching the two botocore bases let a failed upload escape `storage.upload()` un-wrapped — the worker's `except StorageError` retry path would never have seen it, and the task would have crashed instead of backing off. The entire retry design in §11 was inert against its most likely trigger. Found only by running `S3Store` against a real S3 API.
2. **A single non-ASCII byte in `X-API-Key` returned 500, not 401.** `hmac.compare_digest` refuses two `str` arguments unless both are ASCII, and Starlette decodes header bytes as latin-1. Any client could trigger it with one header and no credentials. Compared as bytes now.
3. **`PyJWKClient` was constructed without a timeout**, which means urllib's: none. A JWKS host that accepts a connection and stalls would pin a worker thread per request until the pool was gone — the component that authenticates browsers taking down the whole API.
4. **Storage errors were not attributed to a stage.** `ObjectNotFound` reached the generic handler, producing a `FAILED` run with `error_stage` null, which tells an operator nothing. Now `InputUnavailable`, a `StageError`.
5. **`.vcf` was published as `text/x-vcard`.** Python's `mimetypes` table predates every format here and maps Variant Call Format to a vCard contact card. That value is stored as object metadata and decides what a browser does on download.
6. **Naive/aware datetime subtraction.** SQLite drops `tzinfo` on a `DateTime(timezone=True)` round trip and Postgres does not, so subtracting a stored timestamp from a live `utcnow()` raised `TypeError` on the test backend only.

### Added beyond the spec

- **`tests/test_storage_s3.py`** — `S3Store` against a real S3 API. `LocalStore` was carrying the whole suite, so the only backend that runs in production had no test. It found defect 1.
- **`/health` storage probe and `schema_revision`** (§17 called for the probe; the revision was not specified). Without the latter, "the migration did not run" and "the code is old" are indistinguishable from outside.
- **Genomics content types** in `runtime/context.py`, matched suffix-wise from the right so `snps.vcf.gz` is gzip rather than plain text.

### Gates still outstanding

**G10** (IAM negative test from the instance), **G11** against a real production
dump — the mechanism is verified on a populated Postgres, but not against your
data — **G12** (a multi-GB `ont-lsdv` run), **G13** (lifecycle noncurrent-version
expiry), and **G9** (deploying with the frontend). None can be closed from here.
