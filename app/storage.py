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
        # Read back what upload() was told, rather than guessing from the
        # extension -- the sidecar is the source of truth, not a fallback.
        sidecar = path.with_suffix(path.suffix + ".content-type")
        content_type = (
            sidecar.read_text().strip() if sidecar.is_file() else "application/octet-stream"
        )
        return ObjectInfo(
            key=key,
            size=path.stat().st_size,
            etag="",
            content_type=content_type,
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
        from boto3.exceptions import S3UploadFailedError
        from botocore.exceptions import BotoCoreError, ClientError

        extra = {"ContentType": content_type}
        # An empty S3_SSE omits the header and relies on the bucket's default
        # encryption. AWS has encrypted new objects with SSE-S3 since January
        # 2023, so the header is redundant there -- and an S3-compatible store
        # without KMS rejects it outright.
        if self.sse:
            extra["ServerSideEncryption"] = self.sse
            if self.sse == "aws:kms":
                extra["SSEKMSKeyId"] = self.kms_key_id

        try:
            # upload_file negotiates multipart on its own, which is what makes a
            # 3 GB BAM work with no special case here.
            self._client.upload_file(str(path), self.bucket, key, ExtraArgs=extra)
        except (ClientError, BotoCoreError, S3UploadFailedError) as exc:
            # S3UploadFailedError is boto3's own wrapper and inherits from
            # NEITHER botocore exception. Catching only those two lets a failed
            # upload escape this module un-wrapped, and the worker's
            # `except StorageError` retry path would never see it -- the run
            # would crash instead of retrying.
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
