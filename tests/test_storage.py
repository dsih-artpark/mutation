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
