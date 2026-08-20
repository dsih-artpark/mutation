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


def test_prefixes_are_constants_not_configuration(monkeypatch):
    """They are mirrored in the instance role's IAM policy, so changing one
    without changing the policy is AccessDenied on every run with nothing in
    the error to explain it. The env must not be able to move them."""
    config = _reload(monkeypatch, S3_ALLOWED_PREFIX="somewhere-else",
                     S3_OUTPUT_PREFIX="elsewhere", API_KEYS="d:sk")
    assert config.S3_ALLOWED_PREFIX == "uploads/"
    assert config.S3_OUTPUT_PREFIX == "runs/"


def test_prefixes_end_with_a_slash(monkeypatch):
    """Load-bearing: startswith("uploads") would also accept "uploadsX/evil"."""
    config = _reload(monkeypatch, API_KEYS="d:sk")
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
