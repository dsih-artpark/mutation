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


def test_prefix_leaf_names_are_not_configuration(monkeypatch):
    """The leaf names are mirrored in the IAM policy, so renaming one without
    changing the policy is AccessDenied on every run with nothing in the error
    to explain it. The env must not be able to move them."""
    config = _reload(monkeypatch, S3_ALLOWED_PREFIX="somewhere-else",
                     S3_OUTPUT_PREFIX="elsewhere", S3_PREFIX="", API_KEYS="d:sk")
    assert config.S3_ALLOWED_PREFIX == "uploads/"
    assert config.S3_OUTPUT_PREFIX == "runs/"


def test_no_prefix_means_the_bucket_root(monkeypatch):
    config = _reload(monkeypatch, S3_PREFIX="", API_KEYS="d:sk")
    assert config.S3_PREFIX == ""
    assert config.S3_ALLOWED_PREFIX == "uploads/"


@pytest.mark.parametrize("given", [
    "SDS004-ES_App_Data",        # as written in Dharini's S3_FOLDER
    "/SDS004-ES_App_Data",       # leading slash: an S3 key never starts with one
    "SDS004-ES_App_Data/",       # trailing slash, the shape most people type
    "/SDS004-ES_App_Data/",
])
def test_a_folder_prefix_is_normalised_and_nests_both_prefixes(monkeypatch, given):
    config = _reload(monkeypatch, S3_PREFIX=given, API_KEYS="d:sk")
    assert config.S3_PREFIX == "SDS004-ES_App_Data/"
    assert config.S3_ALLOWED_PREFIX == "SDS004-ES_App_Data/uploads/"
    assert config.S3_OUTPUT_PREFIX == "SDS004-ES_App_Data/runs/"


@pytest.mark.parametrize("hostile", ["a/../../b", "a\\b", "a//b", "a\nb"])
def test_a_prefix_that_is_not_a_plain_folder_path_is_rejected(monkeypatch, hostile):
    # Concatenated into every key, so a traversal segment here addresses
    # objects nobody intended -- and is mirrored in the IAM policy besides.
    with pytest.raises(RuntimeError, match="plain folder path"):
        _reload(monkeypatch, S3_PREFIX=hostile, API_KEYS="d:sk")


@pytest.mark.parametrize("prefix", ["", "SDS004-ES_App_Data"])
def test_prefixes_end_with_a_slash(monkeypatch, prefix):
    """Load-bearing: startswith("uploads") would also accept "uploadsX/evil"."""
    config = _reload(monkeypatch, S3_PREFIX=prefix, API_KEYS="d:sk")
    assert config.S3_ALLOWED_PREFIX.endswith("/")
    assert config.S3_OUTPUT_PREFIX.endswith("/")


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


# --------------------------------------------------------------------------
# AWS credentials
# --------------------------------------------------------------------------

def test_both_credentials_set_are_read_through(monkeypatch):
    config = _reload(
        monkeypatch, API_KEYS="d:sk", ALLOW_UNAUTHENTICATED=None,
        AWS_ACCESS_KEY_ID="AKIAEXAMPLE", AWS_SECRET_ACCESS_KEY="s3cr3t",
    )
    assert config.AWS_ACCESS_KEY_ID == "AKIAEXAMPLE"
    assert config.AWS_SECRET_ACCESS_KEY == "s3cr3t"


def test_neither_credential_set_is_the_instance_role_path(monkeypatch):
    # Empty is not an error: boto3 falls back to its own chain, which on EC2
    # finds the instance role.
    config = _reload(
        monkeypatch, API_KEYS="d:sk", ALLOW_UNAUTHENTICATED=None,
        AWS_ACCESS_KEY_ID="", AWS_SECRET_ACCESS_KEY="",
    )
    assert config.AWS_ACCESS_KEY_ID == ""
    assert config.AWS_SECRET_ACCESS_KEY == ""


@pytest.mark.parametrize("present,missing", [
    ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"),
    ("AWS_SECRET_ACCESS_KEY", "AWS_ACCESS_KEY_ID"),
])
def test_half_a_credential_is_a_startup_error(monkeypatch, present, missing):
    # The dangerous state: boto3 ignores a lone key and falls through to the
    # credential chain, so the process comes up looking configured while
    # reaching for whatever account the host happens to offer.
    env = {present: "value", missing: ""}
    with pytest.raises(RuntimeError, match="must be set together"):
        _reload(monkeypatch, API_KEYS="d:sk", ALLOW_UNAUTHENTICATED=None, **env)


# --------------------------------------------------------------------------
# .env isolation
# --------------------------------------------------------------------------

def test_a_deployment_dotenv_cannot_reach_the_test_suite(monkeypatch, tmp_path):
    """A .env in the working tree must not decide whether the suite passes.

    Regression: config.py called load_dotenv() unconditionally, which reads the
    FILE off the bind-mounted checkout -- so withholding `env_file: .env` from
    the dev service in docker-compose.yml did nothing. Creating a .env to
    deploy with broke 20 tests on a machine where no source had changed.

    conftest.py's setdefault list could not have caught it: it is an allowlist
    that has to know every setting in advance, and the variable responsible was
    one it had never heard of.
    """
    (tmp_path / ".env").write_text(
        "S3_PREFIX=leaked-from-dotenv\n"
        "REQUIRE_PINNED_IMAGES=true\n"
        "STORAGE_BACKEND=s3\n"
        "S3_BUCKET=someone-elses-bucket\n"
    )
    monkeypatch.chdir(tmp_path)

    config = _reload(monkeypatch, API_KEYS="d:sk", S3_PREFIX=None,
                     DHARINI_IGNORE_DOTENV="1")

    assert config.S3_PREFIX == ""
    assert config.S3_ALLOWED_PREFIX == "uploads/"
    assert config.S3_BUCKET != "someone-elses-bucket"
    assert config.REQUIRE_PINNED_IMAGES is False


# --------------------------------------------------------------------------
# Nested prefixes and Dharini's variable spellings
# --------------------------------------------------------------------------

def test_a_nested_prefix_gives_the_pipeline_its_own_subfolder(monkeypatch):
    """One workload's folder inside a shared app-data folder."""
    config = _reload(monkeypatch, API_KEYS="d:sk",
                     S3_PREFIX="SDS004-ES_App_Data/genomic-sequencing")
    assert config.S3_ALLOWED_PREFIX == "SDS004-ES_App_Data/genomic-sequencing/uploads/"
    assert config.S3_OUTPUT_PREFIX == "SDS004-ES_App_Data/genomic-sequencing/runs/"
