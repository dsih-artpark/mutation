# Object Storage, Authentication and Run Visibility — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move pipeline inputs and outputs onto S3, put a credential on every endpoint, and make a running job report where it is.

**Architecture:** Storage is bookended around the Celery task — `app/storage.py` is the only module holding a boto3 client, and `Pipeline`/`RunContext` never learn that S3 exists. Two credentials with different powers: an API key for Dharini's server (full access) and a locally-verified Dharini JWT for browsers (read-only, owner-scoped). Progress is polled from a dedicated small endpoint backed by a stage heartbeat and medians over historical `run_stages` rows.

**Tech Stack:** FastAPI, Celery, SQLAlchemy 2.0, Alembic, Postgres, boto3, PyJWT, pytest.

**Spec:** `docs/superpowers/specs/2026-08-19-object-storage-and-submission-design.md`

## Global Constraints

- Python 3.13. `from __future__ import annotations` at the top of every new module, matching the existing codebase.
- **Every test runs without network and without Docker** unless marked `@pytest.mark.docker`. `STORAGE_BACKEND=local` in `tests/conftest.py`.
- **No new `docker` invocation paths.** `app/runtime/runner.py` remains the only module that starts a process.
- **`app/storage.py` is the only module that may import `boto3` or `botocore`.** Enforced by a test.
- Comments explain *why*, never *what*. Match the existing density — see `app/config.py` and `app/delivery.py` for the house style.
- Secrets are compared with `hmac.compare_digest`, never `==`.
- Every commit message: lower-case type prefix, imperative subject under 72 chars, body explaining the reasoning. No `Co-Authored-By` trailer unless the repo already uses one (it does not).
- Run `docker compose run --rm dev pytest tests/ -q` before every commit. The `dev` service is the only supported way to run the suite.
- Exact config defaults are in spec §17 and must be copied verbatim.
- Reserved stage names: `fetch`, `publish`. Neither may appear in a pipeline's `stages`.

---

## File Structure

**Created**

| Path | Responsibility |
|---|---|
| `app/storage.py` | `ObjectStore` protocol, `S3Store`, `LocalStore`, key validation, error hierarchy. The only boto3 importer. |
| `app/auth.py` | API-key and JWT verification, `Principal`, FastAPI dependencies. |
| `app/progress.py` | ETA medians, queue depth, progress payload assembly. Pure query logic, no routing. |
| `app/api/queue.py` | `GET /v1/queue`. |
| `alembic.ini`, `alembic/env.py`, `alembic/versions/*.py` | Migrations. |
| `tests/test_storage.py`, `tests/test_auth.py`, `tests/test_progress.py`, `tests/test_uploads.py` | New coverage. |

**Modified**

| Path | Change |
|---|---|
| `app/config.py` | ~20 new settings; `MAX_UPLOAD_SIZE` → `MAX_INPUT_BYTES`; startup guard on `API_KEYS`. |
| `app/models.py` | New columns on `Run`, `RunStage`, `RunArtifact`. |
| `app/schemas.py` | `RunRequest`, `S3 artifact` shape, `ProgressOut`, `QueueOut`. |
| `app/api/runs.py` | JSON submission, ownership filtering, `/progress`, `/reupload`. |
| `app/tasks.py` | `fetch` bookend, `upload_run_artifacts`, `sweep_pending_uploads`, retention guard. |
| `app/delivery.py` | `build_payload` emits `bucket`/`key`, `owner_id`, `external_id`, upload state. |
| `app/pipelines/base.py` | `ExtraInput`, `extra_inputs`, `effective_stages`. |
| `app/runtime/runner.py` | Heartbeat callback on `_PeakMemorySampler`. |
| `app/runtime/context.py` | Heartbeat wiring; `declared_stages` receives effective stages. |
| `app/main.py` | Auth dependency, CORS allowlist, drop legacy router, `/health` storage probe. |
| `app/worker.py` | Beat entry for `sweep_pending_uploads`. |
| `tests/contract.py` | `publish` reserved; `extra_inputs` filename check. |

**Deleted**

`app/api/legacy.py`, its export in `app/api/__init__.py`, its registration in `app/main.py`, and `tests/test_api.py:259-272`.

---

## Task Dependency Graph

```
T1 config ──┬── T2 storage ──────────────┐
            ├── T3 auth ─────────┐       │
            └── T5 extra_inputs ─┤       │
                                 │       │
T4 alembic ──── T6 model columns ┴───────┴── T7 ownership
                                              │
                    T8 fetch ─── T9 publish ──┤
                                              ├── T10 progress/queue
                                              ├── T11 webhook payload
                                              └── T12 JSON submit + delete legacy
                                                        │
                                                   T13 docs
```

T2, T3 and T5 may run in parallel once T1 lands. Everything else is sequential.

---

## Task 1: Configuration and dependencies

**Files:**
- Modify: `app/config.py`
- Modify: `requirements.txt`
- Modify: `.env.example`
- Modify: `tests/conftest.py`
- Test: `tests/test_config_storage.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces: `app.config.STORAGE_BACKEND: str`, `S3_BUCKET: str`, `S3_REGION: str`, `S3_ALLOWED_PREFIX: str`, `S3_OUTPUT_PREFIX: str`, `S3_ENDPOINT_URL: str`, `S3_SSE: str`, `S3_KMS_KEY_ID: str`, `LOCAL_STORAGE_ROOT: Path`, `MAX_INPUT_BYTES: int`, `S3_UPLOAD_MAX_ATTEMPTS: int`, `S3_UPLOAD_BACKOFF_S: list[int]`, `API_KEYS: dict[str, str]` (label→secret), `ALLOW_UNAUTHENTICATED: bool`, `DHARINI_JWT_JWKS_URL: str`, `DHARINI_JWT_SECRET: str`, `DHARINI_JWT_ISS: str`, `DHARINI_JWT_AUD: str`, `DHARINI_JWT_LEEWAY_S: int`, `CORS_ALLOWED_ORIGINS: list[str]`, `HEARTBEAT_INTERVAL_S: int`, `ETA_HISTORY_RUNS: int`, `ETA_CACHE_TTL_S: int`, `RETENTION_HOURS: int` (default now 6).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_config_storage.py`:

```python
"""Configuration is read at import time, so each case reloads the module."""

from __future__ import annotations

import importlib

import pytest


def _reload(monkeypatch, **env):
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    import app.config
    return importlib.reload(app.config)


def test_api_keys_parse_into_label_secret_pairs(monkeypatch):
    config = _reload(monkeypatch, API_KEYS="dharini:sk_a,ops:sk_b")
    assert config.API_KEYS == {"dharini": "sk_a", "ops": "sk_b"}


def test_blank_api_keys_without_opt_in_is_a_startup_error(monkeypatch):
    with pytest.raises(RuntimeError, match="API_KEYS"):
        _reload(monkeypatch, API_KEYS="", ALLOW_UNAUTHENTICATED=None)


def test_blank_api_keys_are_allowed_when_explicitly_opted_in(monkeypatch):
    config = _reload(monkeypatch, API_KEYS="", ALLOW_UNAUTHENTICATED="true")
    assert config.API_KEYS == {}
    assert config.ALLOW_UNAUTHENTICATED is True


def test_a_key_without_a_label_is_rejected(monkeypatch):
    # "sk_a" alone gives no way to say which caller a request came from.
    with pytest.raises(RuntimeError, match="label:secret"):
        _reload(monkeypatch, API_KEYS="sk_a", ALLOW_UNAUTHENTICATED=None)


def test_s3_backend_requires_a_bucket(monkeypatch):
    with pytest.raises(RuntimeError, match="S3_BUCKET"):
        _reload(monkeypatch, STORAGE_BACKEND="s3", S3_BUCKET="",
                API_KEYS="d:sk", ALLOW_UNAUTHENTICATED=None)


def test_local_backend_needs_no_bucket(monkeypatch):
    config = _reload(monkeypatch, STORAGE_BACKEND="local", S3_BUCKET="",
                     API_KEYS="d:sk")
    assert config.STORAGE_BACKEND == "local"


def test_prefixes_are_normalised_to_end_with_a_slash(monkeypatch):
    # A prefix without a trailing slash makes "uploads" match "uploadsX/evil".
    config = _reload(monkeypatch, S3_ALLOWED_PREFIX="uploads",
                     S3_OUTPUT_PREFIX="runs", API_KEYS="d:sk")
    assert config.S3_ALLOWED_PREFIX == "uploads/"
    assert config.S3_OUTPUT_PREFIX == "runs/"


def test_kms_sse_requires_a_key_id(monkeypatch):
    with pytest.raises(RuntimeError, match="S3_KMS_KEY_ID"):
        _reload(monkeypatch, S3_SSE="aws:kms", S3_KMS_KEY_ID="", API_KEYS="d:sk")


def test_cors_origins_split_and_strip(monkeypatch):
    config = _reload(monkeypatch,
                     CORS_ALLOWED_ORIGINS="https://a.example, https://b.example",
                     API_KEYS="d:sk")
    assert config.CORS_ALLOWED_ORIGINS == ["https://a.example", "https://b.example"]


def test_retention_defaults_to_six_hours(monkeypatch):
    config = _reload(monkeypatch, RETENTION_HOURS=None, API_KEYS="d:sk")
    assert config.RETENTION_HOURS == 6
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `docker compose run --rm dev pytest tests/test_config_storage.py -q`
Expected: FAIL — `AttributeError: module 'app.config' has no attribute 'API_KEYS'`

- [ ] **Step 3: Add the settings to `app/config.py`**

Append these sections. Keep the existing file's comment style — say why, not what.

```python
# --------------------------------------------------------------------------
# Object storage
# --------------------------------------------------------------------------

STORAGE_BACKEND = os.getenv("STORAGE_BACKEND", "s3").strip().lower()
if STORAGE_BACKEND not in ("s3", "local"):
    raise RuntimeError(f"STORAGE_BACKEND must be 's3' or 'local', got {STORAGE_BACKEND!r}")

S3_BUCKET = os.getenv("S3_BUCKET", "").strip()
S3_REGION = os.getenv("S3_REGION", "").strip()
S3_ENDPOINT_URL = os.getenv("S3_ENDPOINT_URL", "").strip()

if STORAGE_BACKEND == "s3" and not S3_BUCKET:
    raise RuntimeError("STORAGE_BACKEND=s3 requires S3_BUCKET to be set.")


def _prefix(name: str, default: str) -> str:
    """Normalise a key prefix to end with '/'.

    Without the trailing slash, a `startswith` check on 'uploads' also accepts
    'uploadsX/anything' -- which is a different, attacker-chosen namespace.
    """
    value = os.getenv(name, default).strip().lstrip("/")
    return value if value.endswith("/") else value + "/"


S3_ALLOWED_PREFIX = _prefix("S3_ALLOWED_PREFIX", "uploads/")
S3_OUTPUT_PREFIX = _prefix("S3_OUTPUT_PREFIX", "runs/")

S3_SSE = os.getenv("S3_SSE", "AES256").strip()
S3_KMS_KEY_ID = os.getenv("S3_KMS_KEY_ID", "").strip()
if S3_SSE == "aws:kms" and not S3_KMS_KEY_ID:
    raise RuntimeError("S3_SSE=aws:kms requires S3_KMS_KEY_ID.")

LOCAL_STORAGE_ROOT = Path(
    os.getenv("LOCAL_STORAGE_ROOT", str(RUNS_ROOT / "_objects"))
).resolve()

# Replaces MAX_UPLOAD_SIZE. The service no longer accepts an upload; this is
# the ceiling on an object it is willing to fetch.
MAX_INPUT_BYTES = int(os.getenv("MAX_INPUT_BYTES", os.getenv("MAX_UPLOAD_SIZE", str(5 * 1024**3))))

S3_UPLOAD_MAX_ATTEMPTS = int(os.getenv("S3_UPLOAD_MAX_ATTEMPTS", "6"))

# Same shape and reasoning as WEBHOOK_BACKOFF_S: index = attempt - 1, spanning
# roughly nine hours so an S3 outage costs a delay rather than the results.
S3_UPLOAD_BACKOFF_S = [10, 60, 300, 1800, 7200, 21600]

# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------

ALLOW_UNAUTHENTICATED = os.getenv("ALLOW_UNAUTHENTICATED", "false").lower() in (
    "1", "true", "yes",
)


def _parse_api_keys(raw: str) -> dict[str, str]:
    """`label:secret,label:secret` -> {label: secret}.

    Labels exist so a request log names the caller, and so a key can be rotated
    by adding the new one, deploying, migrating callers, then removing the old.
    """
    keys: dict[str, str] = {}
    for pair in (p.strip() for p in raw.split(",") if p.strip()):
        label, sep, secret = pair.partition(":")
        if not sep or not label.strip() or not secret.strip():
            raise RuntimeError(
                f"API_KEYS entry {pair!r} is not in label:secret form. Without a "
                f"label there is no way to say which caller a request came from."
            )
        keys[label.strip()] = secret.strip()
    return keys


API_KEYS = _parse_api_keys(os.getenv("API_KEYS", ""))

# An unauthenticated service must be chosen, never reached by forgetting to set
# something -- the same reasoning as REQUIRE_PINNED_IMAGES above.
if not API_KEYS and not ALLOW_UNAUTHENTICATED:
    raise RuntimeError(
        "API_KEYS is empty and ALLOW_UNAUTHENTICATED is not set. Every /v1 route "
        "would be open to anyone who can reach this port. Set API_KEYS in "
        "production, or ALLOW_UNAUTHENTICATED=true for local development."
    )

DHARINI_JWT_JWKS_URL = os.getenv("DHARINI_JWT_JWKS_URL", "").strip()
DHARINI_JWT_SECRET = os.getenv("DHARINI_JWT_SECRET", "").strip()
DHARINI_JWT_ISS = os.getenv("DHARINI_JWT_ISS", "").strip()
DHARINI_JWT_AUD = os.getenv("DHARINI_JWT_AUD", "").strip()
DHARINI_JWT_LEEWAY_S = int(os.getenv("DHARINI_JWT_LEEWAY_S", "30"))

# Bearer auth is off unless a verification key is configured. A half-configured
# verifier that falls through to accepting unsigned tokens is worse than no
# browser access at all.
BEARER_AUTH_ENABLED = bool(DHARINI_JWT_JWKS_URL or DHARINI_JWT_SECRET)
if BEARER_AUTH_ENABLED and not (DHARINI_JWT_ISS and DHARINI_JWT_AUD):
    raise RuntimeError(
        "Bearer auth needs DHARINI_JWT_ISS and DHARINI_JWT_AUD. Without them a "
        "token minted by any issuer for any audience would be accepted."
    )

CORS_ALLOWED_ORIGINS = [
    o.strip() for o in os.getenv("CORS_ALLOWED_ORIGINS", "").split(",") if o.strip()
]

# --------------------------------------------------------------------------
# Progress reporting
# --------------------------------------------------------------------------

HEARTBEAT_INTERVAL_S = int(os.getenv("HEARTBEAT_INTERVAL_S", "10"))
ETA_HISTORY_RUNS = int(os.getenv("ETA_HISTORY_RUNS", "50"))
ETA_CACHE_TTL_S = int(os.getenv("ETA_CACHE_TTL_S", "60"))
```

Change the existing retention default from 72 to 6 and replace its comment:

```python
# Artifacts are on S3 once `publish` succeeds, so local disk is scratch. Six
# hours is enough to debug a run by hand; S3 lifecycle rules own the long term.
# A run whose artifacts have NOT reached S3 is never purged -- see tasks.py.
RETENTION_HOURS = int(os.getenv("RETENTION_HOURS", "6"))
```

Leave `MAX_UPLOAD_BYTES` defined as an alias of `MAX_INPUT_BYTES` for this task only; Task 12 removes it.

- [ ] **Step 4: Add dependencies**

In `requirements.txt`:

```
# S3 object storage. app/storage.py is the only module permitted to import it.
boto3==1.35.76

# Verifies Dharini's browser session tokens locally. [crypto] pulls the RSA
# backend needed for RS256 / JWKS; without it only HS256 works.
PyJWT[crypto]==2.10.1
```

- [ ] **Step 5: Point the test suite at the local backend**

In `tests/conftest.py`, alongside the existing `os.environ.setdefault` block:

```python
# The suite exercises the storage seam, not S3. LocalStore keeps it offline
# and dependency-free -- no moto, no MinIO, no credentials.
os.environ.setdefault("STORAGE_BACKEND", "local")
os.environ.setdefault("LOCAL_STORAGE_ROOT", os.path.join(_TMP, "objects"))
os.environ.setdefault("API_KEYS", "test:test-key")
os.environ.setdefault("DHARINI_JWT_SECRET", "test-jwt-secret")
os.environ.setdefault("DHARINI_JWT_ISS", "dharini-test")
os.environ.setdefault("DHARINI_JWT_AUD", "pipeline-test")
```

- [ ] **Step 6: Update `.env.example`**

Add every variable from spec §17 with the same explanatory comment style already used in that file. `API_KEYS` and `S3_BUCKET` get prominent "MUST SET IN PRODUCTION" notes.

- [ ] **Step 7: Run the tests**

Run: `docker compose run --rm dev pytest tests/test_config_storage.py tests/test_config_reference.py -q`
Expected: PASS

- [ ] **Step 8: Run the whole suite**

Run: `docker compose run --rm dev pytest tests/ -q`
Expected: PASS — nothing else reads the new settings yet.

- [ ] **Step 9: Commit**

```bash
git add app/config.py requirements.txt .env.example tests/conftest.py tests/test_config_storage.py
git commit -m "config: settings for object storage, auth and progress

Startup refuses to run with an empty API_KEYS unless ALLOW_UNAUTHENTICATED
is set explicitly, following REQUIRE_PINNED_IMAGES: the insecure state has
to be chosen, never reached by forgetting to set something.

Key prefixes are normalised to end with a slash. Without it a startswith
check on 'uploads' also accepts 'uploadsX/', which is a different namespace
chosen by whoever supplied the key.

RETENTION_HOURS drops 72 -> 6: once publish has put artifacts in S3, local
disk is scratch."
```

---

## Task 2: `app/storage.py`

Runs in parallel with Tasks 3 and 5.

**Files:**
- Create: `app/storage.py`
- Test: `tests/test_storage.py` (create)

**Interfaces:**
- Consumes: `app.config` settings from Task 1.
- Produces:
  - `ObjectInfo(key: str, size: int, etag: str, content_type: str)` — frozen dataclass
  - `StorageError`, `KeyRejected(StorageError, ValueError)`, `ObjectNotFound(StorageError)`, `ObjectTooLarge(StorageError)`, `TransferFailed(StorageError)`
  - `validate_input_key(key: str) -> str` — raises `KeyRejected`
  - `output_key(run_id: str, filename: str) -> str`
  - `ObjectStore` Protocol: `head(key) -> ObjectInfo | None`, `download(key, dest: Path, max_bytes: int) -> int`, `upload(path: Path, key: str, content_type: str) -> None`
  - `LocalStore(root: Path)`, `S3Store(bucket, region, endpoint_url, sse, kms_key_id)`
  - `get_store() -> ObjectStore` — process-level singleton chosen by `STORAGE_BACKEND`
  - `bucket_name() -> str` — `S3_BUCKET`, or `"local"` under `LocalStore`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_storage.py`:

```python
"""Storage seam: key validation, and both backends against one contract."""

from __future__ import annotations

import pytest

from app.storage import (
    KeyRejected,
    LocalStore,
    ObjectTooLarge,
    output_key,
    validate_input_key,
)


# ------------------------------------------------------------------ keys

@pytest.mark.parametrize("key", [
    "uploads/u17/reads.fastq.gz",
    "uploads/a.fasta",
])
def test_keys_under_the_allowed_prefix_are_accepted(key):
    assert validate_input_key(key) == key


@pytest.mark.parametrize("key,reason", [
    ("runs/abc/annotated.vcf", "outside the allowed prefix"),
    ("uploadsX/evil.fasta",    "prefix must match a whole path segment"),
    ("/uploads/a.fasta",       "leading slash"),
    ("",                       "empty"),
    ("uploads/../runs/a.vcf",  "traversal segment"),
    ("uploads//a.fasta",       "empty path segment"),
    ("uploads/a\x00.fasta",    "control character"),
    ("uploads/" + "x" * 1100,  "over the 1024-byte S3 key limit"),
])
def test_hostile_keys_are_rejected(key, reason):
    with pytest.raises(KeyRejected):
        validate_input_key(key)


def test_output_keys_are_built_from_the_run_id_not_the_client(monkeypatch):
    assert output_key("run-abc", "annotated.vcf") == "runs/run-abc/annotated.vcf"


def test_output_filenames_may_not_contain_a_separator():
    # Every caller passes an artifact basename; a separator means a bug upstream.
    with pytest.raises(KeyRejected):
        output_key("run-abc", "../../etc/passwd")


# ------------------------------------------------------- LocalStore contract

@pytest.fixture()
def store(tmp_path):
    return LocalStore(tmp_path / "objects")


def test_head_of_a_missing_object_is_none(store):
    assert store.head("uploads/nope.fasta") is None


def test_upload_then_head_reports_the_size(store, tmp_path):
    source = tmp_path / "a.fasta"
    source.write_bytes(b">x\nACGT\n")
    store.upload(source, "runs/r1/a.fasta", "text/plain")

    info = store.head("runs/r1/a.fasta")
    assert info.size == 8
    assert info.content_type == "text/plain"


def test_download_writes_the_bytes_and_returns_the_count(store, tmp_path):
    source = tmp_path / "a.fasta"
    source.write_bytes(b">x\nACGT\n")
    store.upload(source, "runs/r1/a.fasta", "text/plain")

    dest = tmp_path / "out" / "copy.fasta"
    written = store.download("runs/r1/a.fasta", dest, max_bytes=1024)

    assert written == 8
    assert dest.read_bytes() == b">x\nACGT\n"


def test_download_of_a_missing_object_raises(store, tmp_path):
    from app.storage import ObjectNotFound

    with pytest.raises(ObjectNotFound):
        store.download("uploads/nope.fasta", tmp_path / "x", max_bytes=1024)


def test_download_enforces_the_cap_during_the_write(store, tmp_path):
    """The cap is checked as bytes land, not from a declared size.

    A HeadObject size is metadata; this guarantee has to hold even when it
    understates what actually arrives.
    """
    source = tmp_path / "big.fasta"
    source.write_bytes(b"A" * 5000)
    store.upload(source, "runs/r1/big.fasta", "text/plain")

    dest = tmp_path / "out.fasta"
    with pytest.raises(ObjectTooLarge):
        store.download("runs/r1/big.fasta", dest, max_bytes=1000)

    # The partial file must not survive -- a truncated FASTQ that looks like a
    # real one is worse than no file.
    assert not dest.exists()


def test_local_store_refuses_to_escape_its_root(store, tmp_path):
    # Unlike S3, LocalStore keys ARE filesystem paths, so traversal is real here.
    with pytest.raises(KeyRejected):
        store.head("../../etc/passwd")


def test_only_storage_imports_boto3():
    """boto3 must not leak into the API, the worker or a pipeline."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "app"
    offenders = [
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if path.name != "storage.py" and "boto3" in path.read_text()
    ]
    assert offenders == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `docker compose run --rm dev pytest tests/test_storage.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.storage'`

- [ ] **Step 3: Implement `app/storage.py`**

```python
"""Object storage. The only module in this repository that imports boto3.

The same discipline as runtime/runner.py being the only thing that starts a
process: one place to audit, one place to get right.

A pipeline never sees this module. Storage is bookended around the Celery task
-- fetch before `pipeline.run(ctx)`, publish after -- so `Pipeline` and
`RunContext` do not learn that S3 exists and every pipeline works over object
storage with no edits.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .config import (
    LOCAL_STORAGE_ROOT,
    S3_ALLOWED_PREFIX,
    S3_BUCKET,
    S3_ENDPOINT_URL,
    S3_KMS_KEY_ID,
    S3_OUTPUT_PREFIX,
    S3_REGION,
    S3_SSE,
    STORAGE_BACKEND,
)

# S3's own limit. A longer key is rejected by the service anyway; catching it
# here turns a confusing ClientError into a 422 naming the field.
MAX_KEY_BYTES = 1024

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


# ==========================================================================
# Errors
# ==========================================================================

class StorageError(Exception):
    """Base for everything this module raises.

    A hierarchy rather than bare botocore exceptions, so the API can map to
    status codes and the worker can decide retry-or-fail without either one
    importing botocore.
    """


class KeyRejected(StorageError, ValueError):
    """The key is not one we are willing to address."""


class ObjectNotFound(StorageError):
    """No object at that key."""


class ObjectTooLarge(StorageError):
    """The object exceeds the caller's cap."""


class TransferFailed(StorageError):
    """The transfer started and did not complete."""


@dataclass(frozen=True)
class ObjectInfo:
    key: str
    size: int
    etag: str = ""
    content_type: str = "application/octet-stream"


# ==========================================================================
# Key validation
# ==========================================================================

def _reject_shape(key: str) -> None:
    if not key:
        raise KeyRejected("key is empty")
    if len(key.encode()) > MAX_KEY_BYTES:
        raise KeyRejected(f"key exceeds {MAX_KEY_BYTES} bytes")
    if key.startswith("/"):
        raise KeyRejected("key may not start with '/'")
    if "\\" in key:
        raise KeyRejected("key may not contain a backslash")
    if _CONTROL.search(key):
        raise KeyRejected("key contains a control character")

    segments = key.split("/")
    if any(segment == "" for segment in segments):
        raise KeyRejected("key contains an empty path segment")
    if any(segment in (".", "..") for segment in segments):
        raise KeyRejected("key contains a relative path segment")


def validate_input_key(key: str) -> str:
    """Accept a client-supplied key, or explain why not.

    S3 keys are opaque strings, not paths: the service does not normalise, so
    `uploads/../runs/x` addresses a literally different object than `runs/x`
    and cannot escape a prefix. The prefix check below is therefore the real
    control, and the shape checks are belt and braces that cost nothing.

    Note the prefix itself always ends in '/' (see config._prefix), so this
    matches a whole path segment -- 'uploadsX/evil' does not pass.
    """
    _reject_shape(key)
    if not key.startswith(S3_ALLOWED_PREFIX):
        raise KeyRejected(
            f"key must be under '{S3_ALLOWED_PREFIX}'; got '{key[:80]}'"
        )
    return key


def output_key(run_id: str, filename: str) -> str:
    """Where a published artifact goes. Never client-supplied.

    `filename` is an artifact basename produced by `ctx.artifact()`. A
    separator here means a bug upstream, not hostile input, but it would write
    outside the run's prefix so it is refused rather than sanitised.
    """
    if "/" in filename or "\\" in filename or filename in ("", ".", ".."):
        raise KeyRejected(f"artifact filename {filename!r} is not a bare basename")
    return f"{S3_OUTPUT_PREFIX}{run_id}/{filename}"


# ==========================================================================
# Interface
# ==========================================================================

class ObjectStore(Protocol):
    def head(self, key: str) -> ObjectInfo | None: ...
    def download(self, key: str, dest: Path, max_bytes: int) -> int: ...
    def upload(self, path: Path, key: str, content_type: str) -> None: ...


_COPY_CHUNK = 8 * 1024 * 1024


def _stream_to_disk(source, dest: Path, max_bytes: int, key: str) -> int:
    """Copy a file-like object to `dest`, refusing to exceed `max_bytes`.

    Checked as bytes land rather than from a declared size, for the same reason
    the old multipart handler did: metadata is not a guarantee. A partial file
    is removed -- a truncated FASTQ that still parses is worse than no file.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    try:
        with open(dest, "wb") as out:
            while chunk := source.read(_COPY_CHUNK):
                written += len(chunk)
                if written > max_bytes:
                    raise ObjectTooLarge(
                        f"'{key}' exceeds the {max_bytes / 10**9:.1f} GB limit"
                    )
                out.write(chunk)
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    return written


# ==========================================================================
# Local backend
# ==========================================================================

class LocalStore:
    """Filesystem-backed store for tests and `tools/run_local.py`.

    Exists so the suite exercises the storage seam with no S3, no mocking
    library and no credentials. Keys here really are paths, so unlike S3 this
    backend has to defend against traversal.
    """

    def __init__(self, root: Path):
        self.root = Path(root).resolve()

    def _path(self, key: str) -> Path:
        _reject_shape(key)
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root):
            raise KeyRejected(f"key escapes the store root: {key[:80]}")
        return path

    def head(self, key: str) -> ObjectInfo | None:
        path = self._path(key)
        if not path.is_file():
            return None
        import mimetypes

        content_type, _ = mimetypes.guess_type(path.name)
        return ObjectInfo(
            key=key,
            size=path.stat().st_size,
            etag="",
            content_type=content_type or "application/octet-stream",
        )

    def download(self, key: str, dest: Path, max_bytes: int) -> int:
        path = self._path(key)
        if not path.is_file():
            raise ObjectNotFound(key)
        with open(path, "rb") as source:
            return _stream_to_disk(source, Path(dest), max_bytes, key)

    def upload(self, path: Path, key: str, content_type: str) -> None:
        destination = self._path(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
        # Recorded beside the object so head() can return what upload() was
        # told, rather than re-guessing from the extension.
        destination.with_suffix(destination.suffix + ".content-type").write_text(content_type)


# ==========================================================================
# S3 backend
# ==========================================================================

class S3Store:
    """boto3-backed store. Credentials come from the EC2 instance role.

    There is deliberately no access-key configuration: if the process cannot
    obtain credentials from the role, that is a startup failure, not a fallback
    to something weaker.
    """

    def __init__(self, bucket: str, region: str, endpoint_url: str,
                 sse: str, kms_key_id: str):
        import boto3
        from botocore.config import Config

        self.bucket = bucket
        self.sse = sse
        self.kms_key_id = kms_key_id
        self._client = boto3.client(
            "s3",
            region_name=region or None,
            endpoint_url=endpoint_url or None,
            # Absorbs transient throttling below our own retry layer, so a 503
            # does not consume one of the six upload attempts.
            config=Config(retries={"mode": "adaptive", "max_attempts": 3}),
        )

    def head(self, key: str) -> ObjectInfo | None:
        from botocore.exceptions import ClientError

        try:
            response = self._client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") in (403, 404):
                # 403 as well as 404: with no s3:ListBucket, S3 reports a
                # missing object as AccessDenied rather than NoSuchKey.
                return None
            raise TransferFailed(f"head {key}: {exc}") from exc

        return ObjectInfo(
            key=key,
            size=int(response.get("ContentLength", 0)),
            etag=str(response.get("ETag", "")).strip('"'),
            content_type=response.get("ContentType", "application/octet-stream"),
        )

    def download(self, key: str, dest: Path, max_bytes: int) -> int:
        from botocore.exceptions import ClientError

        try:
            response = self._client.get_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            code = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code in (403, 404):
                raise ObjectNotFound(key) from exc
            raise TransferFailed(f"get {key}: {exc}") from exc

        # A streaming read rather than download_file: the cap has to be applied
        # as bytes land, and download_file gives no hook to stop mid-transfer.
        # Single-stream S3-to-EC2 in the same region runs at roughly 100 MB/s,
        # so a 1 GB FASTQ costs about ten seconds -- worth it for a guarantee
        # that holds regardless of what the object metadata claimed.
        with response["Body"] as body:
            return _stream_to_disk(body, Path(dest), max_bytes, key)

    def upload(self, path: Path, key: str, content_type: str) -> None:
        from botocore.exceptions import BotoCoreError, ClientError

        extra = {"ContentType": content_type, "ServerSideEncryption": self.sse}
        if self.sse == "aws:kms":
            extra["SSEKMSKeyId"] = self.kms_key_id

        try:
            # upload_file negotiates multipart on its own, which is what makes a
            # 3 GB BAM work with no special case here.
            self._client.upload_file(str(path), self.bucket, key, ExtraArgs=extra)
        except (ClientError, BotoCoreError) as exc:
            raise TransferFailed(f"put {key}: {exc}") from exc


# ==========================================================================
# Selection
# ==========================================================================

_store: ObjectStore | None = None


def get_store() -> ObjectStore:
    """The process-wide store. Built once; boto3 clients are expensive."""
    global _store
    if _store is None:
        _store = (
            S3Store(S3_BUCKET, S3_REGION, S3_ENDPOINT_URL, S3_SSE, S3_KMS_KEY_ID)
            if STORAGE_BACKEND == "s3"
            else LocalStore(LOCAL_STORAGE_ROOT)
        )
    return _store


def bucket_name() -> str:
    """Recorded on each run, so a payload rebuilt months later names the bucket
    the files are actually in rather than today's configured one."""
    return S3_BUCKET if STORAGE_BACKEND == "s3" else "local"
```

- [ ] **Step 4: Run the tests**

Run: `docker compose run --rm dev pytest tests/test_storage.py -q`
Expected: PASS, 20 tests.

- [ ] **Step 5: Run the whole suite**

Run: `docker compose run --rm dev pytest tests/ -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add app/storage.py tests/test_storage.py
git commit -m "feat(storage): the only module that talks to object storage

Mirrors runtime/runner.py being the only module that starts a process --
one place to audit. Pipelines never import this; storage is bookended
around the Celery task so RunContext does not learn that S3 exists.

download() streams rather than using download_file, because the size cap
has to be applied as bytes land: a HeadObject size is metadata, not a
guarantee, and a partial file is removed rather than left looking whole.

head() treats 403 as absence. Without s3:ListBucket -- which the instance
role deliberately lacks -- S3 reports a missing object as AccessDenied
rather than NoSuchKey, so a strict 404 check would report every missing
input as a server error."
```

---

## Task 3: `app/auth.py`

Runs in parallel with Tasks 2 and 5.

**Files:**
- Create: `app/auth.py`
- Modify: `app/main.py` (CORS, router dependencies)
- Test: `tests/test_auth.py` (create)

**Interfaces:**
- Consumes: `app.config` settings from Task 1.
- Produces:
  - `Principal(kind: str, label: str, owner_id: str | None)` — frozen dataclass; `kind` is `"service"` or `"user"`
  - `require_principal(request) -> Principal` — FastAPI dependency; any valid credential, else 401
  - `require_service(principal) -> Principal` — FastAPI dependency; 403 for a user token
  - `AuthError` — raised internally, converted to `HTTPException` by the dependencies

- [ ] **Step 1: Write the failing tests**

Create `tests/test_auth.py`:

```python
"""Credential verification.

Every negative case here is a way JWT verification is classically got wrong.
"""

from __future__ import annotations

import time

import jwt
import pytest
from fastapi import FastAPI, Depends
from fastapi.testclient import TestClient

from app.auth import Principal, require_principal, require_service

SECRET = "test-jwt-secret"
ISS = "dharini-test"
AUD = "pipeline-test"


def _token(**overrides) -> str:
    claims = {
        "sub": "u17", "iss": ISS, "aud": AUD,
        "iat": int(time.time()), "exp": int(time.time()) + 300,
    }
    claims.update(overrides)
    key = overrides.pop("_key", SECRET)
    return jwt.encode(claims, key, algorithm=overrides.pop("_alg", "HS256"))


@pytest.fixture()
def client():
    app = FastAPI()

    @app.get("/read")
    def read(principal: Principal = Depends(require_principal)):
        return {"kind": principal.kind, "label": principal.label,
                "owner_id": principal.owner_id}

    @app.get("/write")
    def write(principal: Principal = Depends(require_service)):
        return {"label": principal.label}

    return TestClient(app)


# ------------------------------------------------------------ service key

def test_a_valid_api_key_authenticates_as_a_service(client):
    response = client.get("/read", headers={"X-API-Key": "test-key"})
    assert response.status_code == 200
    assert response.json() == {"kind": "service", "label": "test", "owner_id": None}


def test_a_wrong_api_key_is_401(client):
    assert client.get("/read", headers={"X-API-Key": "nope"}).status_code == 401


def test_no_credential_is_401(client):
    assert client.get("/read").status_code == 401


# -------------------------------------------------------------- user token

def test_a_valid_bearer_token_authenticates_as_its_subject(client):
    response = client.get("/read", headers={"Authorization": f"Bearer {_token()}"})
    assert response.status_code == 200
    assert response.json() == {"kind": "user", "label": "u17", "owner_id": "u17"}


def test_a_bearer_token_may_not_write(client):
    """Submission is service-only: callback_url is trust-sensitive."""
    response = client.get("/write", headers={"Authorization": f"Bearer {_token()}"})
    assert response.status_code == 403


def test_a_service_key_may_write(client):
    assert client.get("/write", headers={"X-API-Key": "test-key"}).status_code == 200


@pytest.mark.parametrize("token_kwargs,why", [
    ({"exp": int(time.time()) - 10},        "expired"),
    ({"iss": "somebody-else"},              "wrong issuer"),
    ({"aud": "another-service"},            "wrong audience"),
    ({"_key": "a-different-secret"},        "signed by an unrelated key"),
])
def test_invalid_tokens_are_401(client, token_kwargs, why):
    response = client.get("/read",
                          headers={"Authorization": f"Bearer {_token(**token_kwargs)}"})
    assert response.status_code == 401, why


def test_an_unsigned_token_is_rejected(client):
    """alg:none is the oldest JWT attack. jwt.decode must be given an explicit
    algorithm list so the token cannot choose its own verification."""
    unsigned = jwt.encode({"sub": "u17", "iss": ISS, "aud": AUD,
                           "exp": int(time.time()) + 300},
                          key="", algorithm="none")
    response = client.get("/read", headers={"Authorization": f"Bearer {unsigned}"})
    assert response.status_code == 401


def test_a_token_missing_sub_is_rejected(client):
    """Without a subject there is no owner to scope reads to, so accepting it
    would silently grant access to every run."""
    bad = jwt.encode({"iss": ISS, "aud": AUD, "exp": int(time.time()) + 300},
                     SECRET, algorithm="HS256")
    assert client.get("/read", headers={"Authorization": f"Bearer {bad}"}).status_code == 401


def test_a_malformed_bearer_value_is_401(client):
    assert client.get("/read", headers={"Authorization": "Bearer not.a.jwt"}).status_code == 401
    assert client.get("/read", headers={"Authorization": "Basic abc"}).status_code == 401


# --------------------------------------------------------------- timing

def test_api_keys_are_compared_in_constant_time():
    """A `==` on a secret leaks it one byte at a time under timing analysis."""
    import inspect

    import app.auth

    source = inspect.getsource(app.auth)
    assert "compare_digest" in source


# ------------------------------------------------------------------ CORS

def test_the_app_does_not_allow_every_origin():
    import app.main

    for middleware in app.main.app.user_middleware:
        assert middleware.kwargs.get("allow_origins") != ["*"], (
            "allow_origins=['*'] with credentials is exactly the configuration "
            "that lets any page read authenticated responses."
        )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `docker compose run --rm dev pytest tests/test_auth.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.auth'`

- [ ] **Step 3: Implement `app/auth.py`**

```python
"""Who is calling, and what they are allowed to do.

Two credentials with different powers. Dharini's server holds an API key and
may submit and administer. A browser presents the Dharini session token it
already has, and may only read runs it owns.

Tokens are verified locally, never introspected over the network: an auth
check that needs a round trip to Dharini fails when Dharini is down, and adds
that latency to every progress poll.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass

import jwt
from fastapi import Depends, HTTPException, Request

from .config import (
    ALLOW_UNAUTHENTICATED,
    API_KEYS,
    BEARER_AUTH_ENABLED,
    DHARINI_JWT_AUD,
    DHARINI_JWT_ISS,
    DHARINI_JWT_JWKS_URL,
    DHARINI_JWT_LEEWAY_S,
    DHARINI_JWT_SECRET,
)

API_KEY_HEADER = "X-API-Key"

# Never taken from the token. Letting the token name its own algorithm is
# what makes alg:none and RS256/HS256 confusion possible.
_HS_ALGORITHMS = ["HS256"]
_RS_ALGORITHMS = ["RS256"]


@dataclass(frozen=True)
class Principal:
    kind: str            # "service" | "user"
    label: str           # API key label, or the token subject
    owner_id: str | None # None for a service key: it sees every run


ANONYMOUS = Principal(kind="service", label="anonymous", owner_id=None)


class AuthError(Exception):
    """Verification failed. Deliberately carries no detail to the client."""


# --------------------------------------------------------------------------
# Service key
# --------------------------------------------------------------------------

def verify_api_key(presented: str) -> Principal | None:
    """Match against every configured key in constant time.

    Every key is compared even after a match, so the time taken does not
    depend on which key matched or how far comparison got. `==` on a secret
    leaks it one byte at a time.
    """
    matched: str | None = None
    for label, secret in API_KEYS.items():
        if hmac.compare_digest(presented, secret):
            matched = label
    return Principal(kind="service", label=matched, owner_id=None) if matched else None


# --------------------------------------------------------------------------
# User token
# --------------------------------------------------------------------------

_jwks_client: "jwt.PyJWKClient | None" = None


def _signing_key(token: str):
    """Resolve the verification key.

    RS256 via JWKS is preferred: this service then holds only a public key, so
    compromising it cannot mint tokens for Dharini. PyJWKClient caches the key
    set and refetches when an unknown `kid` appears, so rotation on Dharini's
    side needs no deploy here.
    """
    global _jwks_client
    if DHARINI_JWT_JWKS_URL:
        if _jwks_client is None:
            _jwks_client = jwt.PyJWKClient(DHARINI_JWT_JWKS_URL, cache_keys=True)
        return _jwks_client.get_signing_key_from_jwt(token).key, _RS_ALGORITHMS
    return DHARINI_JWT_SECRET, _HS_ALGORITHMS


def verify_bearer(token: str) -> Principal:
    if not BEARER_AUTH_ENABLED:
        raise AuthError("bearer authentication is not configured")

    try:
        key, algorithms = _signing_key(token)
        claims = jwt.decode(
            token,
            key,
            algorithms=algorithms,          # explicit; never from the token
            audience=DHARINI_JWT_AUD,
            issuer=DHARINI_JWT_ISS,
            leeway=DHARINI_JWT_LEEWAY_S,
            options={"require": ["exp", "iss", "aud", "sub"]},
        )
    except Exception as exc:  # PyJWTError and JWKS transport alike
        raise AuthError(str(exc)) from exc

    subject = str(claims.get("sub") or "")
    if not subject:
        # Without a subject there is no owner to scope reads to, and an
        # unscoped user token would see every run in the database.
        raise AuthError("token has no subject")

    return Principal(kind="user", label=subject, owner_id=subject)


# --------------------------------------------------------------------------
# FastAPI dependencies
# --------------------------------------------------------------------------

def require_principal(request: Request) -> Principal:
    """Any valid credential. 401 otherwise.

    The failure carries no detail: distinguishing "unknown key" from "expired
    token" tells an attacker which half of the system to keep probing.
    """
    presented = request.headers.get(API_KEY_HEADER)
    if presented:
        principal = verify_api_key(presented)
        if principal:
            return principal
        raise HTTPException(401, "invalid credential")

    authorization = request.headers.get("Authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() == "bearer" and token:
        try:
            return verify_bearer(token)
        except AuthError:
            raise HTTPException(401, "invalid credential") from None

    if ALLOW_UNAUTHENTICATED and not API_KEYS:
        # Local development only; config.py refuses this combination unless it
        # was chosen explicitly.
        return ANONYMOUS

    raise HTTPException(401, "missing credential")


def require_service(principal: Principal = Depends(require_principal)) -> Principal:
    """A write. Service keys only.

    A browser token must never reach these: `params`, `input_key` and above all
    `callback_url` are trust-sensitive, and letting a browser choose the
    callback URL re-opens the SSRF hole validate_callback_url exists to close,
    this time wearing a valid user's token.
    """
    if principal.kind != "service":
        raise HTTPException(403, "this endpoint requires a service credential")
    return principal
```

- [ ] **Step 4: Wire it into `app/main.py`**

Replace the CORS middleware:

```python
from .config import CORS_ALLOWED_ORIGINS, RUNS_ROOT, WORKER_CONCURRENCY

# An explicit allowlist, and only the methods a browser actually uses. The
# previous allow_origins=["*"] with credentials is precisely the configuration
# that lets any page read an authenticated response.
if CORS_ALLOWED_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ALLOWED_ORIGINS,
        allow_credentials=True,
        allow_methods=["GET", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )
```

Apply the dependency to both `/v1` routers, leaving `/` and `/health` open:

```python
from .auth import require_principal

app.include_router(pipelines_router, dependencies=[Depends(require_principal)])
app.include_router(runs_router, dependencies=[Depends(require_principal)])
```

Import `Depends` from `fastapi`. Leave the legacy router registered for now — Task 12 removes it.

- [ ] **Step 5: Run the tests**

Run: `docker compose run --rm dev pytest tests/test_auth.py -q`
Expected: PASS, 15 tests.

- [ ] **Step 6: Fix the existing API tests**

`tests/test_api.py` now gets 401s. Add a shared authenticated client in `tests/conftest.py`:

```python
@pytest.fixture()
def auth_headers():
    """The API key tests/conftest.py configures. Every /v1 route needs one."""
    return {"X-API-Key": "test-key"}
```

Then set `client.headers.update({"X-API-Key": "test-key"})` on the `TestClient`
in `tests/test_api.py`'s `client` fixture, so existing tests need no other edit.

- [ ] **Step 7: Run the whole suite**

Run: `docker compose run --rm dev pytest tests/ -q`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add app/auth.py app/main.py tests/test_auth.py tests/conftest.py tests/test_api.py
git commit -m "feat(auth): a credential on every route, with two power levels

Dharini's server holds an API key and may write. A browser presents the
Dharini session token and may only read. Submission stays service-only
because callback_url is trust-sensitive: a browser choosing it would
reopen the SSRF hole validate_callback_url exists to close, wearing a
valid user's token.

Tokens are verified locally against JWKS rather than introspected over the
network -- an auth check that phones home fails when Dharini is down and
adds that latency to every progress poll.

jwt.decode is given an explicit algorithm list so a token cannot choose
its own verification, which is what alg:none and RS256/HS256 confusion
both depend on. Failures return one indistinguishable 401: saying which
half failed tells an attacker where to keep probing.

allow_origins=['*'] is replaced by an explicit allowlist. With credentials
enabled it is exactly the configuration that lets any page read an
authenticated response."
```

---

## Task 4: Alembic

**Files:**
- Create: `alembic.ini`, `alembic/env.py`, `alembic/script.py.mako`, `alembic/versions/0001_baseline.py`
- Modify: `app/db.py`, `requirements.txt`, `Makefile`
- Test: `tests/test_migrations.py` (create)

**Interfaces:**
- Consumes: `app.db.Base`, `app.config.DATABASE_URL`.
- Produces: `app.db.init_db()` now runs `alembic upgrade head`; `app.db.current_revision() -> str | None`.

- [ ] **Step 1: Add the dependency**

`requirements.txt`:

```
# Schema migrations. create_all() creates missing tables but never alters an
# existing one, so every column added from here on needs a revision.
alembic==1.14.0
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_migrations.py`:

```python
"""The migration chain must describe exactly what the models declare.

A baseline that drifts from the models corrupts the first upgrade that runs
against a real database, and nothing before that point reveals it.
"""

from __future__ import annotations

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

from app.db import Base


@pytest.fixture()
def migrated_engine(tmp_path):
    url = f"sqlite:///{tmp_path / 'migrate.db'}"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    return create_engine(url)


def test_the_chain_produces_exactly_what_the_models_declare(migrated_engine):
    import app.models  # noqa: F401  registers the mappers

    with migrated_engine.connect() as connection:
        context = MigrationContext.configure(connection)
        diff = compare_metadata(context, Base.metadata)

    assert diff == [], f"models and migrations disagree: {diff}"


def test_every_revision_downgrades(tmp_path):
    """A revision that cannot be reversed is a deploy with no way back."""
    url = f"sqlite:///{tmp_path / 'down.db'}"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    command.downgrade(config, "base")
```

- [ ] **Step 3: Run it to verify it fails**

Run: `docker compose run --rm dev pytest tests/test_migrations.py -q`
Expected: FAIL — `alembic.ini` does not exist.

- [ ] **Step 4: Initialise Alembic**

```bash
docker compose run --rm dev alembic init alembic
```

Then edit `alembic/env.py` so it reads the application's configuration rather
than a duplicated URL:

```python
from app.config import DATABASE_URL
from app.db import Base
import app.models  # noqa: F401  registers every mapper before autogenerate

target_metadata = Base.metadata

# A URL set on the Config (by the test suite) wins; otherwise take the
# application's, so `alembic upgrade head` on the host and init_db() inside the
# API can never disagree about which database they mean.
if not config.get_main_option("sqlalchemy.url", None):
    config.set_main_option("sqlalchemy.url", DATABASE_URL)
```

In `run_migrations_online`, pass `render_as_batch=True` to `context.configure`.
SQLite cannot `ALTER TABLE ... ALTER COLUMN`, and the test suite runs on
SQLite while production runs on Postgres; batch mode makes one revision work on
both.

- [ ] **Step 5: Generate the baseline**

```bash
docker compose run --rm dev alembic revision --autogenerate -m "baseline"
```

Rename the file to `alembic/versions/0001_baseline.py` and set
`revision = "0001"`, `down_revision = None`. Read the generated body and check
it creates `runs`, `run_stages`, `run_artifacts` and `webhook_deliveries` with
the columns currently in `app/models.py` — nothing more.

- [ ] **Step 6: Make `init_db()` run migrations**

In `app/db.py`, replace `init_db`:

```python
def init_db():
    """Bring the schema to head.

    Was `create_all`, which creates missing tables but never alters an existing
    one -- so a new column appeared on a fresh database and silently did not on
    a deployed one, and every query touching it failed at runtime.
    """
    from alembic import command
    from alembic.config import Config

    config = Config(str(BASE_DIR / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", DATABASE_URL)
    command.upgrade(config, "head")


def current_revision() -> str | None:
    """The revision this database is on. Surfaced by /health."""
    from alembic.migration import MigrationContext

    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()
```

Import `BASE_DIR` from `.config`.

- [ ] **Step 7: Add Makefile targets**

```makefile
migrate:  ## Bring the database to head
	docker compose run --rm dev alembic upgrade head

migration:  ## Generate a revision: make migration m="add x"
	docker compose run --rm dev alembic revision --autogenerate -m "$(m)"

stamp:  ## Mark an existing database as being at head WITHOUT running DDL
	docker compose run --rm dev alembic stamp head
```

- [ ] **Step 8: Run the tests**

Run: `docker compose run --rm dev pytest tests/test_migrations.py tests/ -q`
Expected: PASS

- [ ] **Step 9: Commit**

```bash
git add alembic.ini alembic/ app/db.py requirements.txt Makefile tests/test_migrations.py
git commit -m "build: adopt alembic, with the current schema as the baseline

create_all() creates missing tables but never alters an existing one, so
every column added from here on would appear on a fresh database and
silently not on a deployed one -- and every query touching it would fail
at runtime with no migration to blame.

env.py renders in batch mode: SQLite cannot ALTER COLUMN, and the suite
runs on SQLite while production runs on Postgres. One revision has to work
on both.

A test asserts compare_metadata() between the migration chain and the
models is empty. A baseline that drifts corrupts the first upgrade against
a real database, and nothing before that point reveals it.

DEPLOY NOTE: run 'make stamp' once on the existing database before the
first 'make migrate', so the baseline is recorded rather than re-run."
```

---

## Task 5: `ExtraInput` and effective stages

Runs in parallel with Tasks 2 and 3.

**Files:**
- Modify: `app/pipelines/base.py`, `app/pipelines/ont_lsdv/pipeline.py`, `app/api/runs.py`, `app/schemas.py`, `tests/contract.py`
- Test: `tests/test_registry.py` (extend)

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `app.pipelines.base.ExtraInput(filename: str, extensions: tuple[str, ...], required: bool = False, title: str = "", description: str = "")` — frozen dataclass
  - `Pipeline.extra_inputs: dict[str, ExtraInput]` — default `{}`
  - `Pipeline.effective_stages -> tuple[str, ...]` — `("fetch", *stages, "publish")`
  - `Pipeline.describe()` gains `extra_inputs` and reports `effective_stages` as `stages`
  - `PipelineOut` gains `extra_inputs: dict`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_registry.py`:

```python
def test_effective_stages_bookend_the_declared_ones():
    """The platform fetches before and publishes after every pipeline, and both
    are visible as stages so a 90-second download does not look like a hang."""
    from app.pipelines.base import Pipeline

    class _P(Pipeline):
        id = "x"
        stages = ["a", "b"]

        def run(self, ctx):
            raise NotImplementedError

    assert _P().effective_stages == ("fetch", "a", "b", "publish")


def test_describe_reports_the_effective_stages():
    """The denominator a client polls must match what actually runs."""
    from app.pipelines import registry

    described = registry.get("ont-lsdv").describe()
    assert described["stages"][0] == "fetch"
    assert described["stages"][-1] == "publish"


def test_ont_lsdv_declares_its_primer_bed_as_an_extra_input():
    """Replaces the hardcoded `primer_bed` parameter on the submit endpoint."""
    from app.pipelines import registry

    extras = registry.get("ont-lsdv").extra_inputs
    assert "primer_bed" in extras
    assert extras["primer_bed"].filename == "primers.bed"
    assert extras["primer_bed"].required is False
    assert ".bed" in extras["primer_bed"].extensions


def test_a_pipeline_declaring_a_reserved_stage_fails_the_contract():
    from tests.contract import _assert_stages
    from app.pipelines.base import Pipeline

    class _Clash(Pipeline):
        id = "clash"
        stages = ["publish"]

        def run(self, ctx):
            raise NotImplementedError

    with pytest.raises(AssertionError, match="reserved"):
        _assert_stages(_Clash())
```

- [ ] **Step 2: Run to verify failure**

Run: `docker compose run --rm dev pytest tests/test_registry.py -q`
Expected: FAIL — `AttributeError: type object '_P' has no attribute 'effective_stages'`

- [ ] **Step 3: Extend `app/pipelines/base.py`**

```python
from dataclasses import dataclass

# Prepended and appended by the platform to every pipeline's declared stages.
# Reserved: tests/contract.py rejects a pipeline that declares either.
FETCH_STAGE = "fetch"
PUBLISH_STAGE = "publish"


@dataclass(frozen=True)
class ExtraInput:
    """A secondary file a pipeline accepts alongside its primary input.

    Declared rather than hardcoded in the API, which is what removes
    `primer_bed` from the submit endpoint's signature: a pipeline needing a
    different secondary file becomes a backend-only change, and Dharini renders
    the extra file picker from `describe()` exactly as it renders the parameter
    form from `params_schema`.
    """

    filename: str
    """Normalised name on disk. Client filenames never reach a tool."""

    extensions: tuple[str, ...] = ()
    required: bool = False
    title: str = ""
    description: str = ""
```

On `Pipeline`:

```python
    extra_inputs: dict[str, ExtraInput] = {}
    """Secondary inputs, keyed by the name used in `extra_input_keys` and in
    `ctx.extra_input(key)`."""

    @property
    def effective_stages(self) -> tuple[str, ...]:
        """What actually runs, including the platform's own bookends.

        `stages` is what the author declared; this is what a client should see.
        A 90-second fetch and a four-minute publish are otherwise invisible, and
        a run reporting 0/8 for ninety seconds looks hung rather than busy.
        """
        return (FETCH_STAGE, *self.stages, PUBLISH_STAGE)
```

In `describe()`, replace `"stages": list(self.stages)` with
`"stages": list(self.effective_stages)` and add:

```python
            "extra_inputs": {
                key: {
                    "filename": extra.filename,
                    "extensions": list(extra.extensions),
                    "required": extra.required,
                    "title": extra.title,
                    "description": extra.description,
                }
                for key, extra in self.extra_inputs.items()
            },
```

- [ ] **Step 4: Declare `primer_bed` on ont-lsdv**

In `app/pipelines/ont_lsdv/pipeline.py`, on `OntLsdvPipeline`:

```python
    extra_inputs = {
        "primer_bed": ExtraInput(
            filename="primers.bed",
            extensions=(".bed",),
            required=False,
            title="ARTIC primer scheme",
            description=(
                "Required only when primer trimming is enabled. Contig names and "
                "coordinates must match the reference exactly."
            ),
        ),
    }
```

Import `ExtraInput` from `..base`.

- [ ] **Step 5: Update `PipelineOut` and the two `stages` readers**

`app/schemas.py`, on `PipelineOut`: add `extra_inputs: dict = {}`.

`app/api/runs.py`: in `create_run`, `stages=list(pipeline.effective_stages)`.
In `get_run`, `total = len(registry.get(run.pipeline_id).effective_stages) if ... else 0`.

- [ ] **Step 6: Update `tests/contract.py`**

```python
RESERVED_STAGES = {"fetch", "publish"}
```

and extend `_assert_filenames`:

```python
    for key, extra in pipeline.extra_inputs.items():
        assert extra.filename and "/" not in extra.filename and "\\" not in extra.filename, (
            f"{pipeline.id}: extra_inputs[{key!r}].filename {extra.filename!r} "
            f"may not contain a path separator"
        )
        assert extra.extensions, (
            f"{pipeline.id}: extra_inputs[{key!r}] declares no extensions, so the "
            f"API cannot reject the wrong file type before a run is queued"
        )
```

- [ ] **Step 7: Run the tests**

Run: `docker compose run --rm dev pytest tests/test_registry.py tests/test_all_pipelines.py tests/ -q`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add app/pipelines/base.py app/pipelines/ont_lsdv/pipeline.py app/schemas.py app/api/runs.py tests/contract.py tests/test_registry.py
git commit -m "feat(pipelines): declared extra inputs, and platform stage bookends

primer_bed was hardcoded in the submit endpoint's signature, so a pipeline
needing a different secondary file meant an API change. It is now declared
on the pipeline with its own filename and accepted extensions, and Dharini
renders the extra file picker from describe() the way it already renders
the parameter form from params_schema.

effective_stages surrounds the author's stages with fetch and publish.
Both do real work -- a 90-second download and a four-minute upload -- and
were invisible, so a run reported 0/8 for ninety seconds and looked hung.
The denominator a client polls now matches what actually runs."
```

---

## Task 6: Model columns and the migration

**Files:**
- Modify: `app/models.py`
- Create: `alembic/versions/0002_object_storage_auth_progress.py`
- Test: `tests/test_migrations.py` (extend)

**Interfaces:**
- Consumes: Task 4's Alembic setup.
- Produces, on `Run`: `external_id: str`, `owner_id: str | None`, `submitted_by: str | None`, `input_key: str | None`, `extra_input_keys: dict`, `output_bucket: str | None`, `artifacts_uploaded: bool`, `upload_attempts: int`, `upload_error: str | None`, `next_upload_attempt_at: datetime | None`. On `RunStage`: `last_heartbeat_at: datetime | None`. On `RunArtifact`: `s3_key: str | None`, `uploaded_at: datetime | None`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_migrations.py`:

```python
def test_the_backfill_gives_every_historical_run_a_unique_external_id(tmp_path):
    """external_id is NOT NULL and unique, and historical rows have none.

    Backfilled with the run's own id: unique by construction, stable, and
    correct in the only sense that matters -- nothing will ever resubmit it.
    """
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text

    url = f"sqlite:///{tmp_path / 'backfill.db'}"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)

    command.upgrade(config, "0001")
    engine = create_engine(url)
    with engine.begin() as connection:
        for run_id in ("run-a", "run-b"):
            connection.execute(text(
                "INSERT INTO runs (id, pipeline_id, pipeline_version, status, "
                "params, metrics, artifacts_purged, created_at) VALUES "
                f"('{run_id}', 'p', '1', 'SUCCEEDED', '{{}}', '{{}}', 0, "
                "'2026-01-01 00:00:00')"
            ))

    command.upgrade(config, "head")

    with engine.connect() as connection:
        rows = dict(connection.execute(text(
            "SELECT id, external_id FROM runs"
        )).all())

    assert rows == {"run-a": "run-a", "run-b": "run-b"}
```

- [ ] **Step 2: Run to verify failure**

Run: `docker compose run --rm dev pytest tests/test_migrations.py -q`
Expected: FAIL — no `external_id` column.

- [ ] **Step 3: Add the columns to `app/models.py`**

On `Run`, after `pipeline_version`:

```python
    # Chosen by Dharini before submitting. Carries the unique constraint, so a
    # client timeout and retry returns the original run instead of starting a
    # second one -- on a single box that is an hour of duplicated compute. It
    # also survives a lost 202: correlation works even when the caller never
    # learned the run_id.
    external_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)

    # Which of Dharini's users this run belongs to. A browser token may read
    # only its own; a service key sees everything. Nullable, because an
    # operational re-run submitted by `ops` has no user.
    owner_id: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)

    # The API key label that submitted it. Answers "who started this" months
    # later without trawling access logs.
    submitted_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
```

Replacing the `_input_name`/`_extra_inputs` convention:

```python
    input_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    extra_input_keys: Mapped[dict] = mapped_column(JSON, default=dict)
```

Upload state, mirroring `WebhookDelivery`'s fields:

```python
    # Recorded per run rather than read from config at display time: S3_BUCKET
    # may change, and a six-month-old run's objects stay where they were put.
    output_bucket: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # A run that computed correctly but whose artifacts could not be uploaded is
    # a SUCCEEDED run with artifacts_uploaded False -- never a failed run. The
    # same rule WebhookDelivery already follows, for the same reason: discarding
    # twenty minutes of Clair3 over a transient S3 error is the expensive
    # mistake.
    artifacts_uploaded: Mapped[bool] = mapped_column(default=False)
    upload_attempts: Mapped[int] = mapped_column(Integer, default=0)
    upload_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_upload_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
```

On `RunStage`:

```python
    # Written by the peak-memory sampler that already runs. Distinguishes a
    # healthy twenty-minute Clair3 stage from a wedged worker -- without it the
    # two are indistinguishable from outside.
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
```

and extend `__table_args__`:

```python
    __table_args__ = (
        Index("ix_run_stages_run_ordinal", "run_id", "ordinal"),
        # Drives the ETA median: "how long does `variant` usually take".
        Index("ix_run_stages_name_status", "name", "status"),
    )
```

On `RunArtifact`:

```python
    s3_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Per-artifact, so a retry after four of five files landed uploads one file
    # rather than five.
    uploaded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
```

- [ ] **Step 4: Generate and hand-edit the revision**

```bash
docker compose run --rm dev alembic revision --autogenerate -m "object storage, auth and progress"
```

Rename to `alembic/versions/0002_object_storage_auth_progress.py`, set
`revision = "0002"`, `down_revision = "0001"`. Autogenerate will add
`external_id` as `NOT NULL` directly, which fails on a table with rows. Replace
that part of `upgrade()` with a three-step sequence:

```python
def upgrade() -> None:
    # Added nullable, backfilled, then constrained. Adding it NOT NULL in one
    # step fails on any table that already has rows, which is every deployed
    # database.
    op.add_column("runs", sa.Column("external_id", sa.String(128), nullable=True))
    op.execute("UPDATE runs SET external_id = id WHERE external_id IS NULL")
    with op.batch_alter_table("runs") as batch:
        batch.alter_column("external_id", existing_type=sa.String(128), nullable=False)
        batch.create_unique_constraint("uq_runs_external_id", ["external_id"])
    op.create_index("ix_runs_external_id", "runs", ["external_id"])

    # ... the remaining add_column calls, all nullable or with a server_default
```

Every other new column is nullable or carries a `server_default` (`"0"` for
`upload_attempts`, `sa.false()` for `artifacts_uploaded`, `"{}"` for
`extra_input_keys`), so no further backfill is needed.

Write `downgrade()` to drop each column and index in reverse order.

- [ ] **Step 5: Run the tests**

Run: `docker compose run --rm dev pytest tests/test_migrations.py -q`
Expected: PASS — including `compare_metadata` returning empty.

- [ ] **Step 6: Fix run creation so the suite passes**

`external_id` is now `NOT NULL`. In `app/api/runs.py`'s `create_run`, pass
`external_id=run_id_from_body` — Task 12 adds it to the request body properly;
for now default it to a fresh uuid4 when absent so existing tests still create
runs. Add the same to any test helper that constructs a `Run` directly.

- [ ] **Step 7: Run the whole suite**

Run: `docker compose run --rm dev pytest tests/ -q`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add app/models.py alembic/versions/ app/api/runs.py tests/
git commit -m "feat(models): columns for object keys, ownership and upload state

external_id is added nullable, backfilled from the run's own id, then made
NOT NULL and unique. Adding it constrained in one step fails on any table
that already has rows, which is every deployed database; backfilling from
the id is unique by construction and correct in the only sense that
matters, since nothing will ever resubmit a historical run.

input_key and extra_input_keys become real columns, replacing the practice
of stashing _input_name and _extra_inputs inside the params JSON and
stripping reserved keys later.

Upload state mirrors WebhookDelivery's shape because it follows the same
rule: a run that computed correctly but could not deliver its results is
SUCCEEDED with a failed delivery, never a failed run."
```

---

## Task 7: Ownership enforcement on read endpoints

**Files:**
- Modify: `app/api/runs.py`, `app/api/pipelines.py`
- Test: `tests/test_auth.py` (extend)

**Interfaces:**
- Consumes: `Principal` (Task 3), `Run.owner_id` (Task 6).
- Produces: `app.api.runs._visible_run(session, run_id, principal) -> Run` — raises `HTTPException(404)` when absent *or* not owned.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_auth.py`:

```python
def test_a_user_may_read_their_own_run(client_with_runs):
    client, mine, _theirs = client_with_runs
    response = client.get(f"/v1/runs/{mine}", headers=_bearer("u17"))
    assert response.status_code == 200


def test_a_user_reading_someone_elses_run_gets_404_not_403(client_with_runs):
    """403 confirms the run exists. Enumerating ids should yield nothing."""
    client, _mine, theirs = client_with_runs
    response = client.get(f"/v1/runs/{theirs}", headers=_bearer("u17"))
    assert response.status_code == 404


def test_listing_runs_under_a_user_token_shows_only_their_own(client_with_runs):
    client, mine, theirs = client_with_runs
    ids = {r["id"] for r in client.get("/v1/runs", headers=_bearer("u17")).json()}
    assert mine in ids and theirs not in ids


def test_a_service_key_sees_every_run(client_with_runs):
    client, mine, theirs = client_with_runs
    ids = {r["id"] for r in client.get("/v1/runs",
                                       headers={"X-API-Key": "test-key"}).json()}
    assert {mine, theirs} <= ids


def test_a_user_may_not_download_someone_elses_artifact(client_with_runs):
    client, _mine, theirs = client_with_runs
    response = client.get(f"/v1/runs/{theirs}/artifacts/annotated.vcf",
                          headers=_bearer("u17"))
    assert response.status_code == 404


def test_every_run_read_route_enforces_owner_scoping():
    """Driven by the router's own table, so a route added later fails this test
    until it is handled. Ownership checks are easy to add to the obvious
    endpoint and forget on the next one."""
    from app.api.runs import router

    unscoped = []
    for route in router.routes:
        if "GET" not in getattr(route, "methods", set()):
            continue
        source = route.endpoint.__code__.co_names
        if "_visible_run" not in source and "_visible_runs" not in source:
            unscoped.append(route.path)

    assert unscoped == [], f"GET routes without owner scoping: {unscoped}"
```

Add fixtures `_bearer(sub)` (signs an HS256 token as in Task 3) and
`client_with_runs` (creates one run owned by `u17` and one by `u99`, returns
`(client, mine_id, theirs_id)`).

- [ ] **Step 2: Run to verify failure**

Run: `docker compose run --rm dev pytest tests/test_auth.py -q`
Expected: FAIL — cross-owner reads currently return 200.

- [ ] **Step 3: Implement the helper and apply it**

In `app/api/runs.py`:

```python
def _visible_run(session: Session, run_id: str, principal: Principal) -> Run:
    """Fetch a run the caller is allowed to see.

    A run owned by someone else is reported as absent, not forbidden. A 403
    confirms the run exists, which turns id enumeration into a working
    membership oracle.
    """
    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(404, f"unknown run: {run_id}")
    if principal.owner_id is not None and run.owner_id != principal.owner_id:
        raise HTTPException(404, f"unknown run: {run_id}")
    return run


def _visible_runs(query, principal: Principal):
    """Scope a list query to what the caller may see."""
    if principal.owner_id is not None:
        query = query.filter(Run.owner_id == principal.owner_id)
    return query
```

Add `principal: Principal = Depends(require_principal)` to `get_run`,
`list_runs`, `download_artifact` (and, in Task 10, `get_progress`), replacing
every `session.get(Run, run_id)` with `_visible_run(...)` and wrapping
`list_runs`' query in `_visible_runs(...)`.

Change `redeliver` and `reupload` to depend on `require_service` instead.

- [ ] **Step 4: Run the tests**

Run: `docker compose run --rm dev pytest tests/test_auth.py tests/ -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/api/runs.py tests/test_auth.py
git commit -m "feat(api): scope run reads to their owner

A run owned by someone else is reported as absent rather than forbidden.
403 confirms the run exists, which turns id enumeration into a working
membership oracle; 404 tells an attacker nothing either way.

A service key sees every run -- Dharini's server needs to -- so the filter
keys off principal.owner_id being set rather than off the run.

The route-table test walks the router itself, so a GET route added later
fails until it uses _visible_run. Ownership checks are easy to add to the
obvious endpoint and forget on the next one."
```

---

## Task 8: The `fetch` bookend and preflight

**Files:**
- Modify: `app/tasks.py`, `app/runtime/runner.py`, `app/runtime/context.py`
- Test: `tests/test_fetch.py` (create)

**Interfaces:**
- Consumes: `storage.get_store()`, `validate_input_key`, `Run.input_key`, `Run.extra_input_keys`, `Pipeline.effective_stages`, `Pipeline.extra_inputs`.
- Produces:
  - `app.runtime.runner.preflight(images: list[str]) -> None` — raises `PreflightFailed`; the only place that may run `docker image inspect`
  - `app.runtime.spec.PreflightFailed(StageError)`
  - `app.tasks._fetch_inputs(ctx, run, pipeline) -> None`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_fetch.py`:

```python
"""The fetch bookend: inputs arrive from the store before the pipeline runs."""

from __future__ import annotations

import pytest

from app.models import Run, RunStatus
from app.storage import get_store


def test_the_primary_input_lands_under_the_pipelines_declared_name(seeded_run):
    """The client's filename is metadata. What reaches a tool is the pipeline's
    own name, so spaces and shell metacharacters never get that far."""
    run, paths, pipeline = seeded_run
    assert (paths.input / pipeline.input_filename).exists()
    assert not (paths.input / "Hostile Name; rm -rf.fasta").exists()


def test_an_extra_input_lands_under_its_declared_name(seeded_run_with_extra):
    run, paths, pipeline = seeded_run_with_extra
    assert (paths.input / pipeline.extra_inputs["primer_bed"].filename).exists()


def test_fetch_appears_as_the_first_stage(seeded_run, session):
    run, _paths, _pipeline = seeded_run
    stages = [s.name for s in sorted(run.stages, key=lambda s: s.ordinal)]
    assert stages[0] == "fetch"


def test_a_missing_object_fails_the_run_at_fetch(client, session, monkeypatch):
    """Cheap to redo -- nothing has been computed -- so this is a plain failure
    rather than the retry machinery the upload side gets."""
    from app.tasks import execute_run

    run = _queue_run(session, input_key="uploads/absent.fasta")
    execute_run(run.id)

    session.refresh(run)
    assert run.status == RunStatus.FAILED
    assert run.error_stage == "fetch"


def test_an_oversized_object_fails_the_run_rather_than_filling_the_disk(
        session, monkeypatch):
    monkeypatch.setattr("app.tasks.MAX_INPUT_BYTES", 10)
    from app.tasks import execute_run

    run = _queue_run(session, input_key="uploads/big.fasta")   # 5000 bytes
    execute_run(run.id)

    session.refresh(run)
    assert run.status == RunStatus.FAILED
    assert "limit" in (run.error_message or "")


def test_preflight_names_the_missing_image_and_the_fix():
    from app.runtime.runner import preflight
    from app.runtime.spec import PreflightFailed

    with pytest.raises(PreflightFailed, match="make images-pull"):
        preflight(["dsihartpark/definitely-not-present:0.0.0"])
```

Fixtures `seeded_run`, `seeded_run_with_extra` and `_queue_run` put objects into
`LocalStore` under `uploads/`, create a `Run` with the matching `input_key`, and
run `execute_run` synchronously (Celery is already eager in the suite).

- [ ] **Step 2: Run to verify failure**

Run: `docker compose run --rm dev pytest tests/test_fetch.py -q`
Expected: FAIL — `ImportError: cannot import name 'preflight'`

- [ ] **Step 3: Add `PreflightFailed` to `app/runtime/spec.py`**

```python
class PreflightFailed(StageError):
    """A precondition for running this pipeline is not met on this host.

    Subclasses StageError so it flows through the existing failure, recording
    and webhook paths with no new code.
    """
```

- [ ] **Step 4: Add `preflight()` to `app/runtime/runner.py`**

This module is the only one permitted to start a process, which is why the
check lives here rather than in `tasks.py`.

```python
def preflight(images: list[str]) -> None:
    """Confirm every image this pipeline needs is already on the host.

    Without this a missing image surfaces at stage five, twenty minutes in, as
    an opaque Docker error. With it the run fails in under a second naming the
    image and the command that fixes it.
    """
    missing = []
    for image in images:
        result = subprocess.run(
            [DOCKER_BIN, "image", "inspect", image],
            capture_output=True, timeout=30,
        )
        if result.returncode != 0:
            missing.append(image)

    if missing:
        raise PreflightFailed(
            "preflight",
            f"image(s) not present on this host: {', '.join(missing)}. "
            f"Run 'make images-pull'.",
        )
```

Match the existing `StageError` constructor signature in `spec.py` — read it
before writing this and adjust the call to match.

- [ ] **Step 5: Add the fetch bookend to `app/tasks.py`**

Inside `execute_run`, before `pipeline.run(ctx)`:

```python
    try:
        with ctx.stage(FETCH_STAGE):
            preflight(pipeline.required_images())
            _fetch_inputs(ctx, run_snapshot, pipeline)
        result = pipeline.run(ctx)
    except StageError as exc:
        ...
```

and:

```python
def _fetch_inputs(ctx: RunContext, run: dict, pipeline) -> None:
    """Bring every declared input onto local disk under its normalised name.

    The client's filename never reaches this: destinations come from
    `pipeline.input_filename` and `extra_inputs[key].filename`, both of which
    the contract test forbids from containing a path separator. Traversal via a
    crafted object key is therefore structurally impossible, not defended
    against.
    """
    store = get_store()

    store.download(run["input_key"], ctx.paths.input / pipeline.input_filename,
                   MAX_INPUT_BYTES)

    for key, object_key in (run["extra_input_keys"] or {}).items():
        extra = pipeline.extra_inputs.get(key)
        if extra is None:
            raise StageFailed("fetch", f"pipeline declares no extra input {key!r}")
        store.download(object_key, ctx.paths.input / extra.filename, MAX_INPUT_BYTES)
```

`RunContext` receives `stages=list(pipeline.effective_stages)` and
`extra_inputs={key: pipeline.extra_inputs[key].filename for key in run.extra_input_keys}`,
so `ctx.extra_input("primer_bed")` keeps working unchanged.

If `pipeline.required_images()` does not exist, read `app/runtime/images.py` and
`app/pipelines/*/specs.py` to find how images are resolved, and add a
`required_images()` method to `Pipeline` returning the resolved image strings
for every `StageSpec` the pipeline uses. Declare it on the base class returning
`[]` so a pipeline that has not implemented it skips the image half of
preflight rather than failing.

- [ ] **Step 6: Run the tests**

Run: `docker compose run --rm dev pytest tests/test_fetch.py tests/ -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add app/tasks.py app/runtime/runner.py app/runtime/spec.py app/runtime/context.py tests/test_fetch.py
git commit -m "feat(worker): fetch inputs from object storage before the pipeline

A visible first stage rather than silent setup: a 90-second download made a
run report 0/8 and look hung.

Preflight runs first and costs under a second. A missing image otherwise
surfaces at stage five, twenty minutes in, as an opaque Docker error;
now it names the image and the command that fixes it. It lives in
runner.py because that is the only module allowed to start a process.

A fetch failure fails the run outright rather than retrying. Nothing has
been computed yet, so resubmitting costs one download -- the asymmetry
with the upload side, where twenty minutes of Clair3 is already spent."
```

---

## Task 9: The `publish` bookend and upload retry

**Files:**
- Modify: `app/tasks.py`, `app/api/runs.py`, `app/worker.py`
- Test: `tests/test_uploads.py` (create)

**Interfaces:**
- Consumes: `storage.get_store()`, `output_key()`, `bucket_name()`, Task 6's columns.
- Produces:
  - `app.tasks.upload_run_artifacts(run_id: str) -> dict` — Celery task
  - `app.tasks.sweep_pending_uploads() -> dict` — Celery task
  - `POST /v1/runs/{run_id}/reupload` — 202, service credential only

- [ ] **Step 1: Write the failing tests**

Create `tests/test_uploads.py`:

```python
"""Artifact upload, and the rule that a computed run is never discarded."""

from __future__ import annotations

import pytest

from app.models import DeliveryStatus, Run, RunStatus


def test_artifacts_land_under_the_run_prefix(completed_run):
    run = completed_run
    assert run.artifacts_uploaded is True
    for artifact in run.artifacts:
        assert artifact.s3_key == f"runs/{run.id}/{artifact.filename}"
        assert artifact.uploaded_at is not None


def test_a_run_json_manifest_is_written_beside_them(completed_run):
    """Makes the prefix self-describing, and is the recovery path if a webhook
    is lost permanently."""
    from app.storage import get_store

    assert get_store().head(f"runs/{completed_run.id}/run.json") is not None


def test_upload_failure_leaves_the_run_succeeded(failing_store_run):
    """Twenty minutes of Clair3 is not discarded because S3 blipped."""
    run = failing_store_run
    assert run.status == RunStatus.SUCCEEDED
    assert run.artifacts_uploaded is False
    assert run.upload_attempts == 1
    assert run.next_upload_attempt_at is not None


def test_the_webhook_is_withheld_until_the_upload_succeeds(failing_store_run):
    """The payload carries S3 keys. Sending it early hands Dharini keys that
    404, which is worse than a late webhook."""
    assert failing_store_run.deliveries == []


def test_a_retry_skips_artifacts_that_already_landed(partially_uploaded_run, counting_store):
    from app.tasks import upload_run_artifacts

    before = counting_store.uploads
    upload_run_artifacts(partially_uploaded_run.id)
    # Four of five were already stamped; only the fifth plus run.json move.
    assert counting_store.uploads - before == 2


def test_exhausting_the_retries_still_notifies(exhausted_run):
    """The run computed and the results are stranded. Dharini has to learn that
    rather than waiting forever for a delivery that will never come."""
    run = exhausted_run
    assert run.status == RunStatus.SUCCEEDED
    assert run.artifacts_uploaded is False
    assert run.upload_error
    assert len(run.deliveries) == 1


def test_a_failed_run_with_no_artifacts_still_fires_its_webhook(failed_run):
    """Making the webhook conditional on having artifacts is how a failed run
    silently notifies nobody."""
    assert failed_run.status == RunStatus.FAILED
    assert failed_run.artifacts_uploaded is True
    assert len(failed_run.deliveries) == 1


def test_retention_never_purges_a_run_whose_artifacts_are_not_in_s3(session, tmp_path):
    """Local files are the only copy until the upload lands. Purging them would
    destroy a successful result."""
    from app.tasks import cleanup_expired_runs

    run = _old_run(session, artifacts_uploaded=False)
    cleanup_expired_runs(hours=0)

    session.refresh(run)
    assert run.artifacts_purged is False


def test_retention_does_purge_an_uploaded_run(session):
    from app.tasks import cleanup_expired_runs

    run = _old_run(session, artifacts_uploaded=True)
    cleanup_expired_runs(hours=0)

    session.refresh(run)
    assert run.artifacts_purged is True


def test_reupload_requires_a_service_credential(client, completed_run):
    import jwt, time
    token = jwt.encode({"sub": "u17", "iss": "dharini-test", "aud": "pipeline-test",
                        "exp": int(time.time()) + 300}, "test-jwt-secret", "HS256")
    response = client.post(f"/v1/runs/{completed_run.id}/reupload",
                           headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403
```

Fixtures: `counting_store` (a `LocalStore` subclass counting `upload` calls),
`failing_store` (raises `TransferFailed` on `upload`), and helpers building runs
in each state.

- [ ] **Step 2: Run to verify failure**

Run: `docker compose run --rm dev pytest tests/test_uploads.py -q`
Expected: FAIL — `cannot import name 'upload_run_artifacts'`

- [ ] **Step 3: Implement `upload_run_artifacts` in `app/tasks.py`**

```python
@celery.task(name="app.tasks.upload_run_artifacts", bind=True, acks_late=True)
def upload_run_artifacts(self, run_id: str) -> dict:
    """Publish this run's artifacts to object storage, then notify.

    Deliberately not part of execute_run's failure path. A run that computed
    correctly but whose artifacts could not be uploaded is a SUCCEEDED run with
    artifacts_uploaded False -- exactly the rule WebhookDelivery already
    follows, and for the same reason: discarding twenty minutes of Clair3 over
    a transient S3 error is the expensive mistake.
    """
    store = get_store()

    with session_scope() as session:
        run = session.get(Run, run_id)
        if run is None or run.artifacts_uploaded:
            return {"run_id": run_id, "status": "skipped"}
        pending = [(a.id, a.filename, a.content_type)
                   for a in run.artifacts if a.uploaded_at is None]
        attempts = run.upload_attempts or 0

    paths = RunPaths.for_run(RUNS_ROOT, run_id)
    uploaded: list[int] = []
    failure: str | None = None

    for artifact_id, filename, content_type in pending:
        try:
            store.upload(paths.output / filename, output_key(run_id, filename), content_type)
            uploaded.append(artifact_id)
        except StorageError as exc:
            failure = f"{filename}: {exc}"
            break

    # Stamped per artifact, so a retry after four of five landed moves one file
    # rather than five.
    with session_scope() as session:
        for artifact_id in uploaded:
            artifact = session.get(RunArtifact, artifact_id)
            if artifact:
                artifact.s3_key = output_key(run_id, artifact.filename)
                artifact.uploaded_at = utcnow()

    if failure is None:
        failure = _write_run_manifest(run_id, store)

    with session_scope() as session:
        run = session.get(Run, run_id)
        run.output_bucket = bucket_name()
        run.upload_attempts = attempts + 1

        if failure is None:
            run.artifacts_uploaded = True
            run.upload_error = None
            run.next_upload_attempt_at = None
            terminal = True
        else:
            run.upload_error = failure[:8000]
            exhausted = run.upload_attempts >= S3_UPLOAD_MAX_ATTEMPTS
            terminal = exhausted
            if exhausted:
                run.next_upload_attempt_at = None
            else:
                index = min(run.upload_attempts - 1, len(S3_UPLOAD_BACKOFF_S) - 1)
                countdown = S3_UPLOAD_BACKOFF_S[index]
                run.next_upload_attempt_at = utcnow() + timedelta(seconds=countdown)

    if failure is None:
        _enqueue_delivery(run_id)
        return {"run_id": run_id, "status": "uploaded"}

    if terminal:
        # The run computed and the results are stranded on this box. Dharini
        # must be told, or it waits forever for a delivery that never comes.
        _enqueue_delivery(run_id)
        return {"run_id": run_id, "status": "abandoned", "error": failure}

    upload_run_artifacts.apply_async(args=[run_id], countdown=countdown)
    return {"run_id": run_id, "status": "retrying", "in_s": countdown}
```

`_write_run_manifest(run_id, store)` builds the same document `build_payload`
produces, writes it to `paths.root / "run.json"`, uploads it to
`output_key(run_id, "run.json")`, and returns `None` or an error string.

Replace `_enqueue_delivery(run_id)` at the end of `execute_run` with
`upload_run_artifacts.apply_async(args=[run_id])`. It is called
**unconditionally**, including for a failed run — with nothing to upload it
writes `run.json`, marks itself done and enqueues the delivery immediately.

- [ ] **Step 4: Add `sweep_pending_uploads` and the retention guard**

```python
@celery.task(name="app.tasks.sweep_pending_uploads")
def sweep_pending_uploads() -> dict:
    """Re-arms uploads whose retry message was lost, e.g. to a broker restart.

    The same backstop sweep_pending_deliveries provides, for the same reason: a
    scheduled Celery countdown lives in Redis and vanishes with it.
    """
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        overdue = [r.id for r in session.query(Run).filter(
            Run.artifacts_uploaded.is_(False),
            Run.next_upload_attempt_at.isnot(None),
            Run.next_upload_attempt_at <= now,
        ).limit(100).all()]

    for run_id in overdue:
        upload_run_artifacts.apply_async(args=[run_id])
    return {"requeued": len(overdue)}
```

In `cleanup_expired_runs`, beside the existing undelivered-webhook guard:

```python
            # Local files are the only copy until the upload lands. Purging them
            # would destroy a successful result outright.
            if not run.artifacts_uploaded:
                continue
```

In `app/worker.py`, add to `beat_schedule`:

```python
    "sweep-pending-uploads": {
        "task": "app.tasks.sweep_pending_uploads",
        "schedule": 300.0,
    },
```

- [ ] **Step 5: Add the reupload endpoint**

In `app/api/runs.py`, mirroring `redeliver`:

```python
@router.post("/{run_id}/reupload", status_code=202)
def reupload(run_id: str, session: Session = Depends(get_session),
             principal: Principal = Depends(require_service)):
    """Re-run artifact publication.

    The manual recovery path for the case retries are worst at: an outage
    longer than the nine-hour schedule.
    """
    run = _visible_run(session, run_id, principal)
    if not run.is_terminal:
        raise HTTPException(409, f"run is {run.status.value}; nothing to publish yet")
    if run.artifacts_uploaded:
        return {"run_id": run.id, "status": "already_uploaded"}

    run.upload_attempts = 0
    run.next_upload_attempt_at = utcnow()
    session.commit()

    from ..tasks import upload_run_artifacts
    upload_run_artifacts.apply_async(args=[run.id])
    return {"run_id": run.id, "status": "queued"}
```

- [ ] **Step 6: Run the tests**

Run: `docker compose run --rm dev pytest tests/test_uploads.py tests/ -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add app/tasks.py app/api/runs.py app/worker.py tests/test_uploads.py
git commit -m "feat(worker): publish artifacts to object storage, with retry

Upload failure leaves the run SUCCEEDED and retries across roughly nine
hours -- the rule WebhookDelivery already follows, for the same reason:
throwing away twenty minutes of Clair3 because S3 blipped is the expensive
mistake, not the cheap one.

The webhook is withheld until the upload completes, because the payload
carries S3 keys and a delivery pointing at objects that do not exist yet
is worse than a late one. When retries are exhausted it fires anyway with
artifacts_uploaded false, so Dharini learns the results are stranded
instead of waiting forever.

uploaded_at is stamped per artifact, so a retry after four of five landed
moves one file rather than five.

Retention now refuses to purge a run whose artifacts have not reached
storage. Those local files are the only copy."
```

---

## Task 10: Heartbeat, ETA, progress and queue

**Files:**
- Create: `app/progress.py`, `app/api/queue.py`
- Modify: `app/runtime/runner.py`, `app/runtime/context.py`, `app/tasks.py`, `app/api/runs.py`, `app/schemas.py`, `app/main.py`
- Test: `tests/test_progress.py` (create)

**Interfaces:**
- Consumes: `RunStage.last_heartbeat_at`, `Run.status`, `Pipeline.effective_stages`.
- Produces:
  - `app.progress.typical_durations(session, pipeline_id) -> dict[str, float]` — median seconds per stage name, cached `ETA_CACHE_TTL_S`
  - `app.progress.build_progress(session, run, pipeline) -> dict`
  - `app.progress.queue_snapshot(session, include_running: bool) -> dict`
  - `GET /v1/runs/{run_id}/progress` → `ProgressOut`
  - `GET /v1/queue` → `QueueOut`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_progress.py`:

```python
"""Progress reporting: what can be said truthfully about a running job."""

from __future__ import annotations

import pytest


def test_typical_duration_is_the_median_not_the_mean(session):
    """One pathological forty-minute alignment must not move everyone's ETA."""
    from app.progress import typical_durations

    _history(session, "p", "align", [100, 110, 120, 130, 3600])
    assert typical_durations(session, "p")["align"] == 120


def test_eta_is_null_when_there_is_no_history(running_run):
    """A first run of a new pipeline reports elapsed time and nothing else.
    Inventing a number would be worse than admitting ignorance."""
    payload = _progress(running_run)
    assert payload["stage_typical_s"] is None
    assert payload["eta_s"] is None


def test_eta_counts_the_remaining_stages_too(running_run_with_history):
    payload = _progress(running_run_with_history)
    assert payload["eta_s"] > 0
    assert payload["eta_at"] is not None


def test_progress_names_the_current_stage_and_its_position(running_run):
    payload = _progress(running_run)
    assert payload["stage"] == "variant"
    assert payload["stage_index"] == 5
    assert payload["stage_total"] == 9


def test_heartbeat_age_rises_when_the_sampler_stops(running_run, freeze_time):
    payload = _progress(running_run)
    assert payload["heartbeat_age_s"] < 2
    freeze_time(seconds=120)
    assert _progress(running_run)["heartbeat_age_s"] > 100


def test_a_queued_run_reports_its_position_not_a_stage(queued_run):
    payload = _progress(queued_run)
    assert payload["status"] == "QUEUED"
    assert payload["stage"] is None
    assert payload["queue_position"] == 2


def test_the_progress_payload_stays_small(running_run):
    """It is polled every few seconds; GET /v1/runs/{id} is the wrong shape."""
    import json
    assert len(json.dumps(_progress(running_run))) < 2048


def test_queue_depth_and_wait(client, queued_run):
    body = client.get("/v1/queue", headers={"X-API-Key": "test-key"}).json()
    assert body["queued"] >= 1
    assert body["worker_concurrency"] == 1
    assert "running" in body


def test_a_user_token_sees_depth_but_not_other_peoples_runs(client, queued_run):
    body = client.get("/v1/queue", headers=_bearer("u17")).json()
    assert "queued" in body
    assert "running" not in body


def test_a_user_may_not_read_someone_elses_progress(client_with_runs):
    client, _mine, theirs = client_with_runs
    assert client.get(f"/v1/runs/{theirs}/progress",
                      headers=_bearer("u17")).status_code == 404
```

- [ ] **Step 2: Run to verify failure**

Run: `docker compose run --rm dev pytest tests/test_progress.py -q`
Expected: FAIL — `No module named 'app.progress'`

- [ ] **Step 3: Add the heartbeat**

In `app/runtime/runner.py`, give `_PeakMemorySampler` an optional callback:

```python
class _PeakMemorySampler(threading.Thread):
    def __init__(self, container, interval, on_sample=None):
        ...
        self._on_sample = on_sample
        self._last_beat = 0.0

    def run(self) -> None:
        while ...:
            ...
            # Throttled well below the sampling interval used for memory: this
            # writes to Postgres, and liveness at ten-second resolution is as
            # useful as at two.
            now = time.monotonic()
            if self._on_sample and now - self._last_beat >= HEARTBEAT_INTERVAL_S:
                self._last_beat = now
                try:
                    self._on_sample()
                except Exception:
                    # A heartbeat is telemetry. Losing one must never take the
                    # stage down with it.
                    pass
```

`StageRunner.__init__` gains `on_heartbeat=None` and passes it through.
`RunContext` passes `self.recorder.heartbeat` when the recorder has one.

Add to `DbRecorder` in `app/tasks.py`:

```python
    def heartbeat(self, name: str) -> None:
        """Its own short-lived session, per write.

        Sharing a session with the worker thread would let the sampling thread
        hold a connection for the length of a twenty-minute stage.
        """
        with session_scope() as session:
            stage = self._current(session, name)
            if stage:
                stage.last_heartbeat_at = utcnow()
```

and a no-op `heartbeat` on `NullRecorder` in `context.py`, plus `heartbeat` in
the `StageRecorder` Protocol.

- [ ] **Step 4: Implement `app/progress.py`**

```python
"""What can be said truthfully about a run in flight.

Deliberately no sub-stage percentage. Clair3, Medaka and minimap2 emit nothing
machine-readable, so "62% through variant calling" would be a number we made
up. Stage, elapsed, liveness and a historically-derived typical duration are
what the data actually supports.
"""
```

`typical_durations(session, pipeline_id)` selects `duration_s` from
`run_stages` joined to `runs` where `runs.pipeline_id == pipeline_id` and
`run_stages.status == SUCCEEDED`, ordered by `runs.created_at desc`, limited to
`ETA_HISTORY_RUNS` runs; groups by stage name; returns the **median** of each.
Cached in a module-level `{pipeline_id: (expires_at, result)}` dict for
`ETA_CACHE_TTL_S`.

`build_progress(session, run, pipeline)` returns exactly the shape in spec
§13.1. `eta_s` is `max(0, typical_current - elapsed_current)` plus the sum of
typicals for stages after the current one; `None` if any needed typical is
missing.

`queue_snapshot(session, include_running)` returns
`{worker_concurrency, queued, estimated_wait_s}` plus `running` only when
`include_running`.

- [ ] **Step 5: Add the endpoints**

`GET /v1/runs/{run_id}/progress` in `app/api/runs.py` using `_visible_run`.
`GET /v1/queue` in a new `app/api/queue.py`, passing
`include_running=(principal.kind == "service")`. Register the router in
`app/main.py` with the same `require_principal` dependency.

Add `ProgressOut` and `QueueOut` to `app/schemas.py` matching §13.1 and §14.

- [ ] **Step 6: Run the tests**

Run: `docker compose run --rm dev pytest tests/test_progress.py tests/ -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add app/progress.py app/api/queue.py app/api/runs.py app/schemas.py app/main.py app/runtime/ app/tasks.py tests/test_progress.py
git commit -m "feat(api): report where a run actually is

A dedicated small endpoint for polling. GET /v1/runs/{id} drags params,
artifacts and deliveries with it and is the wrong shape to hit every three
seconds.

No sub-stage percentage. Clair3 and Medaka emit nothing machine-readable,
so a percentage would be invented; stage, elapsed, liveness and a median
of past runs are what the data supports. ETAs are null with no history
rather than guessed -- a first run of a new pipeline says so.

The median, not the mean: one pathological forty-minute alignment should
not move the estimate for everyone.

Liveness rides the peak-memory sampler that already runs, throttled to ten
seconds and swallowing its own errors -- a heartbeat is telemetry and must
never take a stage down with it."
```

---

## Task 11: Webhook payload

**Files:**
- Modify: `app/delivery.py`, `app/schemas.py`
- Test: `tests/test_webhooks.py` (extend)

- [ ] **Step 1: Write the failing tests**

```python
def test_artifacts_are_reported_by_bucket_and_key(delivered_payload):
    artifact = delivered_payload["artifacts"][0]
    assert artifact["bucket"] and artifact["key"].startswith("runs/")
    assert "url" not in artifact


def test_the_payload_carries_the_correlation_ids(delivered_payload):
    assert delivered_payload["run"]["external_id"] == "dharini-job-8831"
    assert delivered_payload["run"]["owner_id"] == "u17"


def test_the_payload_says_whether_artifacts_reached_storage(delivered_payload):
    assert delivered_payload["artifacts_uploaded"] is True
    assert delivered_payload["artifacts_error"] is None
    assert delivered_payload["output"]["prefix"].startswith("runs/")


def test_no_file_bytes_are_embedded(delivered_payload):
    """A multi-megabyte body that must survive six retries is not a delivery
    mechanism. Parsed variant rows stay; raw files are keys."""
    import json
    assert "snps_vcf_data" not in json.dumps(delivered_payload)
```

- [ ] **Step 2–4: Update `build_payload`, run, commit**

In `app/delivery.py`, replace the artifact block's `url` with
`"bucket": run.output_bucket or bucket_name()` and `"key": artifact.s3_key`,
add `external_id` and `owner_id` to the `run` object, and add the three
top-level keys. Drop the `PUBLIC_BASE_URL` import if nothing else uses it.

Run: `docker compose run --rm dev pytest tests/test_webhooks.py tests/ -q`

```bash
git commit -m "feat(webhooks): artifacts by bucket and key, not by URL

S3 is the durable store, so a PUBLIC_BASE_URL link would point at a local
file that retention deletes in six hours. Dharini presigns a GET instead.

external_id and owner_id are echoed so a consumer can correlate a delivery
without having recorded the run_id, which matters when the 202 was lost.

artifacts_uploaded tells a consumer the difference between results that
are in storage and results stranded on the worker after the upload retries
ran out."
```

---

## Task 12: JSON submission, and deleting the legacy router

The cutover. Must ship with Dharini.

**Files:**
- Modify: `app/api/runs.py`, `app/schemas.py`, `app/main.py`, `app/api/__init__.py`, `app/config.py`, `requirements.txt`
- Delete: `app/api/legacy.py`
- Test: rewrite `tests/test_api.py`

**Interfaces:**
- Produces: `app.schemas.RunRequest` with fields `pipeline_id: str`, `external_id: str`, `owner_id: str | None`, `input_key: str`, `input_filename: str | None`, `extra_input_keys: dict[str, str]`, `params: dict`, `callback_url: str | None`.

- [ ] **Step 1: Write the failing tests**

Rewrite `tests/test_api.py`'s submission tests against the JSON body, covering
every row of spec §7.1 — including the idempotency case, which must assert
**no second Celery task was dispatched**, not merely that the response was 200.

```python
def test_resubmitting_an_external_id_returns_the_original_run(client, monkeypatch):
    calls = []
    monkeypatch.setattr("app.tasks.execute_run.apply_async",
                        lambda *a, **k: calls.append(a))

    body = _valid_body(external_id="job-1")
    first = client.post("/v1/runs", json=body)
    second = client.post("/v1/runs", json=body)

    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["run_id"] == first.json()["run_id"]
    assert len(calls) == 1, "a duplicate submission must not start a second run"
```

- [ ] **Step 2: Implement the JSON endpoint**

`create_run` becomes:

```python
@router.post("", response_model=RunAccepted, status_code=202)
def create_run(request: RunRequest, response: Response,
               session: Session = Depends(get_session),
               principal: Principal = Depends(require_service)):
```

running the §7.1 ladder in order. On a duplicate `external_id`, set
`response.status_code = 200` and return the existing run's `RunAccepted`
without enqueueing anything.

- [ ] **Step 3: Delete the legacy router**

```bash
git rm app/api/legacy.py
```

Remove its import and `__all__` entry from `app/api/__init__.py`, its
`include_router` line from `app/main.py`, and the legacy tests from
`tests/test_api.py`. Remove `python-multipart` from `requirements.txt` and
`MAX_UPLOAD_BYTES` from `app/config.py`.

- [ ] **Step 4: Verify no multipart remains**

Run: `grep -rn "UploadFile\|multipart\|MAX_UPLOAD_BYTES" app/ requirements.txt`
Expected: no output.

- [ ] **Step 5: Run the whole suite**

Run: `docker compose run --rm dev pytest tests/ -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git commit -m "feat(api): submit runs by object key, and drop the legacy router

The file already exists in S3 by the time Dharini submits, so streaming it
a second time through uvicorn only bought a timeout risk and a second copy
on local disk.

external_id carries a unique constraint: a client timeout and retry now
returns the original run instead of starting a second one. The test
asserts no second Celery task is dispatched, not merely that the response
was 200 -- the response was already 200 before this worked.

BREAKING: /upload, /status/{job_id}, /download/... and /visualization/{id}
are gone, along with multipart submission. Everything they did is covered
by /v1/runs, /v1/pipelines and presigned GETs. Deploy with Dharini."
```

---

## Task 13: Documentation

**Files:** `readme.md`, `docs/PIPELINE_AUTHORING.md`, `.env.example`, `docker-compose.yml`

- [ ] **Step 1: `readme.md`** — redraw the architecture diagram to show S3 on both sides, replace the submission and webhook examples with the JSON body and key-bearing payload, document `X-API-Key` and Bearer auth, add the progress and queue endpoints to the endpoint table, and add a deployment section carrying the IAM policy (spec §8.1), the bucket versioning and lifecycle requirements, and the `make stamp` note from Task 4.

- [ ] **Step 2: `docs/PIPELINE_AUTHORING.md`** — document `extra_inputs`, note that `fetch` and `publish` are reserved and supplied by the platform, and state that pipelines never touch storage.

- [ ] **Step 3: `docker-compose.yml`** — add MinIO to the `dev` profile so the Docker-marked integration tests have an endpoint.

- [ ] **Step 4: Verify the docs match the code**

Run: `docker compose run --rm dev pytest tests/ -q` and re-read each changed
document against the code it describes. Every curl example must be executable
as written.

- [ ] **Step 5: Commit**

```bash
git commit -m "docs: object storage, authentication and the new submission flow"
```

---

## Self-Review

**Spec coverage.** Every numbered section of the spec maps to a task: §6→T3+T7, §7→T12, §8→T2+T13, §9→T2, §10→T8, §11→T9, §12→T11, §13→T10, §14→T10, §15→T4+T6, §16→T9, §17→T1, §18→T12, §19→every task's test step, §20→this plan's ordering, §21→T13, §22→gates below.

**Gates not covered by an automated test**, and therefore called out for manual execution before deployment:

- **G10** (IAM negative test) — run from the instance after deploying: confirm `ListBucket`, `GetObject` on `runs/*` and `DeleteObject` are all denied.
- **G11** (Alembic baseline) — `alembic revision --autogenerate` against a restored production dump must produce an empty revision before `make stamp` is run.
- **G12** (multi-GB upload) — one real `ont-lsdv` run with `keep_alignment: true`.
- **G13** (lifecycle noncurrent versions) — read the bucket lifecycle configuration back after applying it.
- **G9** (cutover) — Task 12 must deploy with Dharini.

**Ordering note.** Task 6 makes `external_id` `NOT NULL` while `create_run` still
takes multipart, so Task 6 Step 6 supplies a uuid4 default that Task 12 replaces
with the request field. Without that bridge the suite breaks between the two.
