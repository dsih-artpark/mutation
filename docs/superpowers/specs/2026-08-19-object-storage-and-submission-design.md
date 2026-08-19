# Object Storage and the Submission Flow

**Date:** 2026-08-19
**Status:** Draft — awaiting review
**Implements:** Phase 3 of `2026-08-04-multi-pipeline-architecture-design.md`,
plus the output side, which that spec does not cover.

Inputs arrive from S3 and results are published to S3. The pipeline service
holds no AWS credentials on disk, cannot enumerate the bucket, cannot read back
another run's results, and cannot destroy anything.

---

## 1. Problem

Dharini uploads a FASTA or FASTQ through the browser, and today that file is
streamed a second time through `POST /v1/runs` as multipart form data. A 1 GB
FASTQ holds a uvicorn connection for minutes, consumes local disk twice over,
and makes `MAX_UPLOAD_SIZE=5GB` a timeout problem rather than a limit.

Results have the mirror problem. `RunArtifact` records a bare filename, the
webhook hands Dharini a `PUBLIC_BASE_URL` link, and this host becomes a
publicly-reachable file server whose 72-hour retention sweep is the only thing
standing between a 320 GB disk and a full one. Every result a scientist may want
next year lives on one EC2 instance's local volume.

There is also no idempotency key. A client timeout and retry starts a second
run, and on a single box that is an hour of duplicated Medaka compute.

The parent spec decided the input half of this in §9 and left it unbuilt.
Phases 0–1 have landed; Phase 2 has not, and does not block this work.

---

## 2. Goals

- Bytes move once, directly between S3 and the box that needs them.
- No AWS access key exists on this host, in `.env`, or in any image.
- A compromise of this service cannot read another user's data, and cannot
  destroy any data.
- Results are durable independent of this instance's disk and lifetime.
- Eight minutes of successful compute is never discarded because S3 blipped.
- A pipeline author's code does not change, and does not learn what S3 is.
- Every test except the Docker-marked integration tests, and `tools/run_local.py`,
  run with no S3 — not a real one, not a mocking library.

### Non-goals

- Phase 2 (`ctx.asset()`, scoped `REFS_ROOT`). Independent of storage.
- Multi-region, cross-account, or customer-managed buckets.
- Presigned URL issuance by this service. See S1.
- Deleting objects from this service. See S3.

---

## 3. Decisions

Numbered `S*` to sit alongside the parent spec's `D*`.

| # | Decision | Rationale |
|---|---|---|
| **S1** | **Dharini uploads to S3 with its own credentials and sends the object key.** This service issues no presigned URLs and has no `POST /v1/uploads`. | Supersedes D6; explicitly sanctioned by §9.2. Dharini already owns the browser upload flow, so the endpoint would be a second path to the same place. Critically, issuing a presigned PUT requires the signer to *hold* `s3:PutObject` on `uploads/*` — so D6 would have made input access read-write. Dropping it is what makes read-only input real rather than nominal. |
| **S2** | **The worker writes artifacts straight to S3.** No proxy hop back through Dharini. | The alternative moves every byte twice and puts a NestJS process in the path of a 3 GB BAM. It also buys nothing: whatever can compromise this worker can equally call Dharini's upload endpoint. The property actually wanted is a prefix-scoped, write-only IAM policy. |
| **S3** | **Object deletion is an S3 lifecycle rule, never an API call.** | Supersedes §10.5. `s3:DeleteObject` on the instance role would forfeit the no-destroy guarantee for a job AWS does for free, enforces itself, and cannot be triggered by a bug in our code. |
| **S4** | **Credentials come from the EC2 instance role.** No static keys anywhere. | Rules out Lightsail, which has no instance-profile support, and Fargate, which cannot host the sibling-container execution model. Plain EC2 keeps `docker compose up` and path parity working unchanged. |
| **S5** | **Bucket versioning is on.** | `PutObject` on an existing key *is* an overwrite; IAM cannot express "create but do not replace". Versioning makes the no-destroy property true and makes upload retries safe. |
| **S6** | **The client never invents a key.** Inputs are validated against a configured bucket and prefix; outputs are always `runs/<run_id>/`. | §9.2's principle. `external_id` already gives Dharini correlation, so a client-supplied output prefix would be a validated attack surface buying nothing. |
| **S7** | **Storage is bookended around the Celery task**, not threaded through `RunContext`. | `app/storage.py` becomes the only module holding a boto3 client, mirroring `runtime/runner.py` being the only thing that starts a process. `Pipeline`, `RunContext` and `ctx.artifact()` do not change, so every existing and future pipeline works over S3 with no edits and `run_local` needs no stub. |
| **S8** | **Input download failure fails the run; artifact upload failure does not.** | Asymmetric because the costs are. Re-downloading is cheap; re-running Medaka is not. Upload gets the retry machinery `WebhookDelivery` already has, and the same rule: a run that computed correctly is `SUCCEEDED` even when a later step could not deliver it. |
| **S9** | **The webhook fires only after artifacts are in S3.** | The payload carries S3 keys. A webhook that arrives before the objects exist hands Dharini keys that 404, which is worse than a late webhook. |
| **S10** | **No file bytes in the webhook.** Parsed variant rows stay inline; everything else is a key. | Already the existing design's position (`WEBHOOK_MAX_INLINE_VARIANTS`). A multi-megabyte body that must survive six retries is not a delivery mechanism. |
| **S11** | **Alembic, adopted now.** | Supersedes §13's single hand-written migration. That was scoped to three columns; this phase adds roughly twelve. `models.py` describes run history as the point of a surveillance platform, and history you intend to keep needs migrations. |
| **S12** | **`ExtraInput` is pulled forward from Phase 4.** | The JSON body needs `extra_input_keys`, and validating one requires each extra's accepted extensions and on-disk filename. Keeping `primer_bed` hardcoded means writing the endpoint and its validation twice. |
| **S13** | **`STORAGE_BACKEND=s3\|local`.** `LocalStore` implements the same Protocol over the filesystem. | The fast suite and `run_local` touch no S3 at all, and need no mocking library. Only the Docker-marked integration tests need a real endpoint. |

---

## 4. Deltas from the approved spec

Recorded explicitly so the parent document is not silently contradicted.

| Parent ref | Approved | This phase | Decision |
|---|---|---|---|
| D6, §9.2 | Service issues presigned PUT via `POST /v1/uploads` | Dharini uploads; sends `input_key`. No such endpoint. | S1 |
| §9.1 | `ObjectStore` = `presign_put, head, download, delete` | `head, download, upload` | S1, S3 |
| §10.5 | `cleanup_expired_runs` deletes S3 objects | Lifecycle rules on the bucket | S3 |
| §11 | Artifacts served from `PUBLIC_BASE_URL` | Artifacts in S3; webhook carries `{bucket, key}` | S2 — new, §9 covered inputs only |
| §13 | One hand-written migration, Alembic later | Alembic now | S11 |
| §14 | Phase 2 precedes Phase 3 | Phase 3 without Phase 2 | Sequencing only; nothing here depends on `ctx.asset()` |

Adopted unchanged from the parent: D15 (`external_id`), §9.2's validation
ladder and `S3_ALLOWED_PREFIX`, §10.1 (`ExtraInput`), §10.3 (`fetch` +
preflight), §11's three-mechanism response flow.

---

## 5. Flow

```
① Dharini (browser)  ──PUT──►  s3://dharini/uploads/<...>/reads.fastq.gz
                                  Dharini's credentials, Dharini's key layout

② Dharini ──POST /v1/runs (JSON)──►  pipeline service
                                       validate → 202 {run_id}  |  200 (idempotent)
                                                                |  4xx in milliseconds

③ worker  fetch    GetObject uploads/…  ─►  $RUNS_ROOT/<run_id>/input/reads.fastq.gz
④ worker  <stages> pipeline.run(ctx)        ← unchanged, no S3 awareness
⑤ worker  publish  PutObject  ─►  s3://dharini/runs/<run_id>/{annotated.vcf, …, run.json}

⑥ worker  POST callback_url   {run, external_id, metrics, variants,
                                artifacts:[{bucket, key, …}]}

⑦ Dharini mints presigned GETs for the user
```

---

## 6. Submission contract

`POST /v1/runs`, `Content-Type: application/json`. Multipart is removed.

```jsonc
{
  "pipeline_id":      "ont-lsdv",
  "external_id":      "dharini-job-8831",
  "input_key":        "uploads/u17/2f8c/reads.fastq.gz",
  "input_filename":   "Sample_42_reads.fastq.gz",      // optional, display only
  "extra_input_keys": { "primer_bed": "uploads/u17/2f8c/scheme.bed" },
  "params":           { "variant": {"tool": "clair3"}, "qc": {"tool": "fastp"} },
  "callback_url":     "https://dharini.example.org/api/snp/webhook"
}
```

`params` is untouched by this work — same `params_schema`, same jsonschema
validation, same `GET /v1/pipelines` form rendering. Per-pipeline tool selection
already works through it and needs nothing new.

`input_filename` is metadata. The file on disk is still normalised to
`pipeline.input_filename`, so a hostile name never reaches a tool command line.
When omitted, the key's basename is used for the extension check.

### 6.1 Validation, before anything is queued

Ordered cheapest-first, so the common mistakes cost no network call.

| # | Check | Failure |
|---|---|---|
| 1 | `external_id` already present | **200** with the existing run — idempotent, not an error |
| 2 | pipeline exists | 404 |
| 3 | `params` against `params_schema` | 422 |
| 4 | `callback_url` — existing `validate_callback_url` SSRF check | 422 |
| 5 | every `input_key` well-formed and under `S3_ALLOWED_PREFIX` | 422 |
| 6 | every required `extra_inputs` entry present; no unknown keys | 422 |
| 7 | extension ∈ `accepted_extensions` / `extra_inputs[k].extensions` | 422 |
| 8 | `HeadObject` on each key — exists | 422, `{"message": "input object not found", "key": …}` |
| 9 | `HeadObject` size ≤ `MAX_INPUT_BYTES` | 413 |

Check 8 returns 422 rather than 404: the route exists and the request is
well-formed but unprocessable, and a 404 on a POST implies a missing endpoint.

This ladder is why the API is worth keeping synchronous. Without step 8, a
typo'd key is a run that queues, waits behind three others, and dies twenty
minutes later. With it, it is an instant error while the user is still looking
at the upload screen — the same reasoning that already puts `params` validation
ahead of enqueueing.

### 6.2 Responses

| Code | Meaning |
|---|---|
| 202 | new run accepted — `RunAccepted` body, now including `external_id` |
| 200 | `external_id` already seen — the existing `RunAccepted`, unchanged |
| 404 | unknown `pipeline_id` |
| 413 | an input exceeds `MAX_INPUT_BYTES` |
| 422 | any other validation failure |

`RunAccepted.stages` reports the effective list — `fetch` first, `publish` last
— so the denominator a client sees matches what will actually run.

---

## 7. Key layout, and why prefix checking is sufficient

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
cannot escape a prefix. `key.startswith(S3_ALLOWED_PREFIX)` is therefore the
real control. `..`, control characters, backslashes, `//` and keys over 1024
bytes are rejected anyway — it costs nothing and keeps the rule legible to
someone who has not internalised that S3 is not a filesystem.

No local path is ever derived from a key. Destinations come from
`pipeline.input_filename` and `extra_inputs[k].filename`, both of which the
contract test already forbids from containing a path separator. Path traversal
via a crafted key is structurally impossible rather than defended against.

### 7.1 IAM

Attached to the EC2 instance role. No user, no access key, no secret.

```jsonc
{ "Effect": "Allow", "Action": "s3:GetObject",
  "Resource": "arn:aws:s3:::dharini/uploads/*" },
{ "Effect": "Allow", "Action": "s3:PutObject",
  "Resource": "arn:aws:s3:::dharini/runs/*" }
```

No `ListBucket`, no `DeleteObject`, no `GetObject` on `runs/*`, no wildcards.
Bucket versioning on (S5). Lifecycle rules expire `uploads/` after 7 days — an
input is consumed within minutes and kept only for resubmission — and
transition `runs/` to Glacier Instant Retrieval at 90 days. Both rules must
carry a **noncurrent version expiration** clause; without it, versioning turns
retry churn into unbounded storage growth.

The result: this host can read the one object it was handed and create new
objects under a run-scoped prefix. It cannot enumerate the bucket, cannot read
back another run's results, and cannot destroy anything. There is no key on the
filesystem to leak.

---

## 8. Storage interface

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
boto3's managed transfer handles multipart automatically in both directions, so
a 3 GB BAM needs no special case.

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

## 9. The `fetch` bookend

The platform prepends `fetch` and appends `publish` to every pipeline's declared
stages. `Pipeline.effective_stages` returns `("fetch", *self.stages, "publish")`
and is what `describe()`, `RunAccepted.stages` and `RunContext.declared_stages`
all use. Both names are reserved; `tests/contract.py` already rejects `fetch`
and gains `publish`.

This is not cosmetic. A 90-second download and a four-minute BAM upload are
otherwise invisible, and a run that reports `0/8` for ninety seconds looks hung.

Inside `fetch`, before any transfer, **preflight** (§10.3): every image the
pipeline resolved is present, every asset it declares is on disk. A missing
image fails the run in under a second with an actionable message instead of
dying at stage five with a confusing Docker error. `PreflightFailed` subclasses
`StageError`, so it flows through the existing failure, recording and webhook
paths with no new code.

A failure anywhere in `fetch` fails the run (S8): `error_stage="fetch"`, and
Dharini resubmits at the cost of one download.

---

## 10. The `publish` bookend

`upload_run_artifacts(run_id)` — a Celery task, so it is independently
retryable and independently observable.

```
for artifact in run.artifacts where uploaded_at is null:
    store.upload(output/<filename>, f"{S3_OUTPUT_PREFIX}{run_id}/{filename}", artifact.content_type)
    artifact.s3_key, artifact.uploaded_at = …
write run.json to {S3_OUTPUT_PREFIX}<run_id>/run.json
run.artifacts_uploaded = True
→ _enqueue_delivery(run_id)
```

`uploaded_at` living on the artifact row is what makes retry cheap: if four of
five files landed and the BAM timed out, the retry skips the four. A separate
`ArtifactUpload` table would buy nothing over that — unlike webhooks, there is
exactly one destination per run.

On failure: `upload_attempts += 1`, `next_upload_attempt_at` set, rescheduled
with `S3_UPLOAD_BACKOFF_S`. The run stays `SUCCEEDED` throughout (S8) and **the
webhook is withheld** (S9).

On exhaustion: `artifacts_uploaded` stays false, `upload_error` is recorded, and
the webhook fires anyway carrying `artifacts_uploaded: false`. Dharini learns
that the run computed and the results are stranded, rather than waiting forever
for a delivery that will never come.

**A run with no artifacts still goes through `publish`.** `execute_run` calls
`upload_run_artifacts` unconditionally — including for a run that failed at
`fetch` or mid-pipeline. With nothing to upload it writes `run.json`, sets
`artifacts_uploaded = True` and enqueues the delivery immediately. Making the
webhook conditional on there being artifacts is how a failed run silently never
notifies anyone; the `publish` stage is therefore recorded as `SUCCEEDED` on a
`FAILED` run, which is accurate — the run failed, the reporting did not.

`run.json` is the same document as the webhook payload. It costs nothing, makes
the S3 prefix self-describing, and is the recovery path if a webhook is lost
permanently.

Two existing safety nets gain siblings, following the patterns already written
in `tasks.py`:

- **`sweep_pending_uploads`** beside `sweep_pending_deliveries` — re-arms
  uploads whose retry task was lost to a Redis flush or a worker killed
  mid-countdown.
- **`POST /v1/runs/{id}/reupload`** beside `/redeliver` — the manual recovery
  path for the case retries are worst at, an outage longer than the schedule.

---

## 11. Webhook payload

Unchanged except where noted. `run`, `metrics`, `stages` and `variants` keep
their existing shapes, so Dharini's handler barely moves.

```jsonc
{
  "event": "run.completed",
  "delivery_id": "…", "attempt": 1,
  "run": { "id": "…", "external_id": "dharini-job-8831", … },   // + external_id
  "metrics": { … },                                              // unchanged
  "stages":  [ … ],                                              // unchanged
  "variants": [ … ], "variants_truncated": false,                // unchanged
  "artifacts": [
    { "kind": "vcf", "label": "Annotated variants",
      "filename": "annotated.vcf",
      "bucket": "dharini", "key": "runs/<run_id>/annotated.vcf",
      "size_bytes": 48210, "content_type": "text/plain" }        // url → bucket+key
  ],
  "artifacts_uploaded": true,                                    // new
  "artifacts_error": null,                                       // new
  "output": { "bucket": "dharini", "prefix": "runs/<run_id>/" }  // new
}
```

Signing, headers, backoff, the SSRF allowlist and `sweep_pending_deliveries` are
all untouched.

---

## 12. Data model

```python
Run:
    external_id            String(128)  unique, indexed, nullable=False
    input_key              Text
    extra_input_keys       JSON         default {}
    output_bucket          String(255)  nullable
    artifacts_uploaded     bool         default False
    upload_attempts        int          default 0
    upload_error           Text         nullable
    next_upload_attempt_at DateTime     nullable, indexed

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

### 12.1 Migration

`init_db()`'s `create_all` creates missing tables; it will not add a column to
an existing one. Adopting Alembic (S11):

1. `alembic init`, configured to read `DATABASE_URL` from `app.config`.
2. Autogenerate a **baseline** revision from the current models, stamped on
   existing databases with `alembic stamp` so no DDL runs against them.
3. One revision adding the columns above, backfilling `external_id` for
   historical runs and creating its unique index.
4. `init_db()` becomes `alembic upgrade head`, run once at API startup.

**`external_id` is `NOT NULL` with a unique index, and historical rows have
none.** The revision backfills them with the run's own id before applying the
constraint — every existing run then has a unique, stable `external_id` equal to
its `run_id`, which is correct in the only sense that matters: nothing will ever
resubmit it.

---

## 13. Retention

`cleanup_expired_runs` gains one guard, mirroring the one already there for
undelivered webhooks: **a run whose artifacts are not in S3 is never purged.**
Deleting local files for a run whose upload was abandoned would destroy the only
copy of a successful result.

Once artifacts are uploaded, local disk is genuinely disposable, so
`RETENTION_HOURS` drops from 72 to **6**. The 320 GB volume stops being the
constraint it is today, and S3 lifecycle rules (S3) own long-term retention.

---

## 14. Configuration

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
| `RETENTION_HOURS` | **6** | was 72 |

`AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` are deliberately absent. If the
process cannot obtain credentials from the instance role, that is a startup
failure, not a fallback.

`/health` gains a storage probe (backend, bucket, credentials resolvable) —
credentials failing at 02:00 on a run that has been computing for forty minutes
should not be the first sign of a misconfiguration.

---

## 15. Removals

| Removed | Replacement |
|---|---|
| `app/api/legacy.py` entirely, its router registration, its `__init__` export, `tests/test_api.py:259-272` | `/v1/runs`, `/v1/pipelines`, S3 keys |
| `_save_upload`, `_stored_name` in `api/runs.py` | `storage.download` + `pipeline.input_filename` |
| `python-multipart` | no multipart endpoint remains |
| `MAX_UPLOAD_SIZE` | `MAX_INPUT_BYTES` |
| hardcoded `primer_bed` in the endpoint signature | `extra_inputs` / `extra_input_keys` (S12) |

`generate_visualization` is called by the snippy-fasta pipeline itself
(`pipeline.py:132`), so deleting the legacy router does not affect it.
`GET /v1/runs/{id}/artifacts/{filename}` and `PUBLIC_BASE_URL` survive for
operational debugging but leave the webhook payload; Dharini stops depending on
them.

---

## 16. Testing

| Layer | Mechanism |
|---|---|
| Unit — everything below the API | `LocalStore`, no S3 at all (S13) |
| API — validation ladder | `LocalStore`, plus a fault-injecting subclass for the miss / oversize / transfer-failure paths |
| Integration | MinIO as a `dev`-profile compose service behind `S3_ENDPOINT_URL` |
| Contract | `tests/contract.py` gains `publish` to `RESERVED_STAGES` and asserts every `extra_inputs[*].filename` has no path separator |

New cases, each corresponding to a way this can go wrong:

- key outside `S3_ALLOWED_PREFIX`, containing `..`, over 1024 bytes → 422
- `HeadObject` miss → 422; oversize → 413
- oversize discovered *during* download → run FAILED, not a full disk
- duplicate `external_id` → 200 with the original run, **and no second Celery task**
- upload failure → run stays `SUCCEEDED`, webhook **not** sent
- upload retry skips artifacts with `uploaded_at` set
- upload exhaustion → webhook sent with `artifacts_uploaded: false`
- retention refuses a run with `artifacts_uploaded = False`
- `effective_stages` denominator matches the stages that actually execute
- Alembic: baseline autogenerate against a restored dump produces an empty diff

---

## 17. Build order

Sequenced so each step is independently testable and the breaking API change
lands last, on machinery that already works.

| # | Step | Verifiable by |
|---|---|---|
| 1 | `app/storage.py`, `ObjectStore`, `S3Store`, `LocalStore`, key validation | unit tests, no other code touched |
| 2 | Config settings, MinIO dev service, `/health` probe | `make test`, `curl /health` |
| 3 | Alembic baseline + stamp | empty autogenerate diff against a dump |
| 4 | `Pipeline.extra_inputs` / `ExtraInput` / `effective_stages` | contract test over both pipelines |
| 5 | Model columns + revision | migration up and down on a copy |
| 6 | `fetch` bookend + preflight | worker consumes S3 inputs; API still multipart |
| 7 | `publish` bookend, `upload_run_artifacts`, sweeper, `/reupload`, retention guard | upload retry tests |
| 8 | Webhook payload change | `tests/test_webhooks.py` |
| 9 | `POST /v1/runs` JSON, delete legacy router, rewrite `tests/test_api.py` | full suite |
| 10 | `readme.md`, `docs/PIPELINE_AUTHORING.md`, `.env.example` | review |

Steps 1–8 are individually deployable. Step 9 is the cutover and must ship with
Dharini.

---

## 18. Dharini's side of the contract

Not this repository's work, but the design is not complete without it.

1. Upload to `s3://<bucket>/uploads/…` with its own credentials. This service
   never sees them and issues no presigned PUT (S1).
2. Generate a unique `external_id` **before** submitting, so a retry after a
   lost `202` is idempotent and correlation survives (D15).
3. Verify `X-Dharini-Signature` — HMAC-SHA256 over `<timestamp>.<body>` — and
   **reject an `X-Dharini-Timestamp` older than ~5 minutes.** The signature
   covers the timestamp, but replay protection only exists if the receiver
   enforces freshness (§11).
4. Treat `X-Dharini-Delivery` as the idempotency key; retries are expected.
5. Mint presigned GETs for users. This service serves no result files.
6. Run a reconciliation sweep — say every 10 minutes: any run submitted over an
   hour ago with no terminal status → `GET /v1/runs/{id}`. Roughly twenty lines, and it is what
   makes an `ABANDONED` delivery recoverable (§11).
7. Handle `artifacts_uploaded: false` — the run succeeded and the results are
   stranded on the worker. `POST /v1/runs/{id}/reupload` is the recovery.

---

## 19. Risks and verification gates

Continuing the parent spec's numbering. Gates are blocking.

| ID | Risk | Gate |
|---|---|---|
| **G9** | The API break is total — there is no compatibility shim, so a Dharini deploy that lags strands every submission. | Both sides deployed together. One real end-to-end submission on staging, upload through webhook, before production. |
| **G10** | An IAM policy that permits the happy path can still be far wider than intended, and nothing in normal operation reveals it. | **Negative test from the instance:** `ListBucket` denied, `GetObject` on `runs/*` denied, `DeleteObject` denied. Passing the happy path is not the check. |
| **G11** | An Alembic baseline that does not match the deployed schema corrupts on first upgrade. | Autogenerate against a restored production dump must produce an **empty** diff before the baseline is stamped. |
| **G12** | Multi-GB artifact upload is the least-exercised path and the most expensive to get wrong. | One real `ont-lsdv` run with `keep_alignment: true`. Confirm the BAM lands, `run.json` is written, and the webhook fires **after** both. |
| **G13** | Versioning plus retry churn grows storage without bound if lifecycle rules cover only current versions. | Lifecycle configuration must include a **noncurrent version expiration** clause. Verified by reading back the bucket's lifecycle configuration, not by intent. |
| **G14** | `RETENTION_HOURS` 72 → 6 deletes local files far sooner; a bug in the upload guard becomes data loss rather than an inconvenience. | The retention test asserting `artifacts_uploaded = False` is never purged must exist and pass **before** the default is lowered. |

---

## 20. Outcome

| Property | Before | After |
|---|---|---|
| Bytes per input | 2 (browser→S3, S3→API→disk) | 1 |
| AWS credentials on host | n/a | none — instance role |
| Blast radius of host compromise | n/a | one object read; no enumeration, no destruction |
| Result durability | one EC2 volume, 72 h | S3, lifecycle-governed |
| Duplicate submit on retry | second run | idempotent |
| Cost of a transient S3 failure after compute | n/a | one retry, not one re-run |
| Pipeline author changes | — | none |
