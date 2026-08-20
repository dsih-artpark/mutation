"""S3Store against a real S3 API.

tests/test_storage.py covers LocalStore, which is the backend the suite runs
on -- and therefore the backend that is NOT deployed. S3Store is the one that
handles production traffic, so leaving it exercised only by reading the code
puts the coverage exactly where it is least useful.

Runs against the `minio` service in the dev compose profile. Skipped when
S3_ENDPOINT_URL is unset, so the fast suite stays offline; set REQUIRE_S3=1 in
CI to turn that skip into a failure.
"""

from __future__ import annotations

import os
import uuid

import pytest

pytestmark = pytest.mark.docker

ENDPOINT = os.getenv("S3_ENDPOINT_URL", "")


@pytest.fixture(scope="module")
def s3_store():
    if not ENDPOINT:
        if os.getenv("REQUIRE_S3", "").lower() in ("1", "true", "yes"):
            pytest.fail("REQUIRE_S3 is set but S3_ENDPOINT_URL is not configured")
        pytest.skip("S3_ENDPOINT_URL not set; start the dev profile's minio service")

    import boto3

    from app.storage import S3Store

    bucket = f"dharini-test-{uuid.uuid4().hex[:8]}"
    client = boto3.client(
        "s3",
        endpoint_url=ENDPOINT,
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID", "dharini"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY", "dharini-dev-secret"),
        region_name="us-east-1",
    )
    client.create_bucket(Bucket=bucket)

    # SSE is empty here: MinIO rejects an explicit ServerSideEncryption header
    # unless KMS is configured, and asserting on real KMS would be testing AWS
    # rather than this module. Production sets S3_SSE=AES256, which AWS accepts
    # and which tests/test_storage.py's config coverage pins.
    yield S3Store(bucket, "us-east-1", ENDPOINT, "", "")

    objects = client.list_objects_v2(Bucket=bucket).get("Contents", [])
    for obj in objects:
        client.delete_object(Bucket=bucket, Key=obj["Key"])
    client.delete_bucket(Bucket=bucket)


def test_head_of_a_missing_object_is_none(s3_store):
    """Not an exception. With no s3:ListBucket -- which the instance role
    deliberately lacks -- S3 answers a missing object with AccessDenied rather
    than NoSuchKey, and both must read as absence."""
    assert s3_store.head("uploads/definitely-absent.fasta") is None


def test_upload_head_download_round_trip(s3_store, tmp_path):
    source = tmp_path / "a.fasta"
    source.write_bytes(b">chr1\nACGTACGT\n")

    s3_store.upload(source, "runs/r1/a.fasta", "text/plain")

    info = s3_store.head("runs/r1/a.fasta")
    assert info.size == 15
    assert info.content_type == "text/plain"
    assert info.etag

    dest = tmp_path / "out" / "a.fasta"
    assert s3_store.download("runs/r1/a.fasta", dest, max_bytes=1024) == 15
    assert dest.read_bytes() == source.read_bytes()


def test_download_of_a_missing_object_raises_object_not_found(s3_store, tmp_path):
    from app.storage import ObjectNotFound

    with pytest.raises(ObjectNotFound):
        s3_store.download("uploads/absent.fasta", tmp_path / "x", max_bytes=1024)


def test_download_enforces_the_cap_mid_stream(s3_store, tmp_path):
    """The reason download() streams instead of calling download_file: the cap
    has to stop a transfer in progress, not audit it afterwards."""
    from app.storage import ObjectTooLarge

    source = tmp_path / "big.fasta"
    source.write_bytes(b"A" * 200_000)
    s3_store.upload(source, "runs/r1/big.fasta", "text/plain")

    dest = tmp_path / "big-copy.fasta"
    with pytest.raises(ObjectTooLarge):
        s3_store.download("runs/r1/big.fasta", dest, max_bytes=50_000)

    assert not dest.exists(), "a partial file that still parses is worse than none"


def test_a_multipart_sized_object_survives_the_round_trip(s3_store, tmp_path):
    """upload_file negotiates multipart on its own above ~8 MB. This is the
    path a 3 GB BAM takes, at a size the suite can afford."""
    source = tmp_path / "large.bam"
    source.write_bytes(os.urandom(12 * 1024 * 1024))

    s3_store.upload(source, "runs/r1/large.bam", "application/octet-stream")
    dest = tmp_path / "large-copy.bam"
    written = s3_store.download("runs/r1/large.bam", dest, max_bytes=64 * 1024 * 1024)

    assert written == source.stat().st_size
    assert dest.read_bytes() == source.read_bytes()


def test_a_failed_upload_raises_transfer_failed_not_a_boto_exception(s3_store, tmp_path):
    """Regression: upload_file wraps failures in boto3.exceptions.
    S3UploadFailedError, which inherits from neither ClientError nor
    BotoCoreError. Catching only those two let a failed upload escape this
    module un-wrapped, so the worker's `except StorageError` retry path never
    saw it and the task crashed instead of backing off.
    """
    from app.storage import S3Store, TransferFailed

    source = tmp_path / "a.fasta"
    source.write_bytes(b">x\nACGT\n")
    nowhere = S3Store("bucket-that-does-not-exist-9f3c", "us-east-1", ENDPOINT, "", "")

    with pytest.raises(TransferFailed):
        nowhere.upload(source, "runs/r1/a.fasta", "text/plain")
