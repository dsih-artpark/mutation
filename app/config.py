"""Central configuration.

PATH PARITY
-----------
`RUNS_ROOT` and `REFS_ROOT` must resolve to the *same absolute path* on the
host, inside the worker container, and inside every tool container. The worker
spawns sibling containers through the host Docker daemon, and the daemon always
resolves bind-mount sources against the host filesystem -- never against the
calling container. Keeping one path namespace everywhere means we never
translate paths, and `Path.exists()` in pipeline code means what it says.

See docker-compose.yml, where both are mounted as `${RUNS_ROOT}:${RUNS_ROOT}`.
"""

from pathlib import Path
import os

from dotenv import load_dotenv

# The dev container and the test suite must not read a developer's .env.
#
# docker-compose.yml already withholds `env_file: .env` from the `dev` service
# for exactly this reason -- but that only controls the container's environment,
# and load_dotenv() reads the FILE, off the bind-mounted working tree, so the
# protection was bypassed the moment anyone created a .env to deploy with.
#
# The failure was not subtle once it happened -- 20 tests -- but it was
# misleading: the suite broke on a machine where no source had changed, and the
# variable responsible (S3_PREFIX) was one conftest.py had no reason to know
# about. That is the structural problem here. conftest sets a fixed list of
# variables with setdefault, which is an allowlist that has to be kept in sync
# with every setting ever added; anything it has not heard of leaks through.
# Refusing to read the file at all is the version that does not rot.
if os.getenv("DHARINI_IGNORE_DOTENV", "").lower() not in ("1", "true", "yes"):
    load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

DATA_DIR = BASE_DIR / "data"

# Absolute, identical host-side and container-side. See module docstring.
RUNS_ROOT = Path(os.getenv("RUNS_ROOT", str(BASE_DIR / "runs"))).resolve()
REFS_ROOT = Path(os.getenv("REFS_ROOT", str(DATA_DIR))).resolve()

RUNS_ROOT.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)

# --------------------------------------------------------------------------
# Reference data (snippy-fasta pipeline)
# --------------------------------------------------------------------------

# Named by filename and resolved inside REFS_ROOT, because REFS_ROOT is the
# only references directory mounted into tool containers. A reference anywhere
# else exists for the application and not for the tool that has to open it.
#
# This was a live bug: REFERENCE_PATH defaulted to /app/data/..., which is a
# path inside the application image. snippy therefore ran with
# `--ref /app/data/lsdv_reference_sequence.fasta` and reported "Invalid
# --reference filename", because the stage container mounts REFS_ROOT at its
# host path and has no /app at all. It only ever worked while the since-deleted
# local execution path ran tools in-process.
REFERENCE_FILENAME = os.getenv("REFERENCE_FILENAME", "lsdv_reference_sequence.fasta")
REFERENCE = (REFS_ROOT / REFERENCE_FILENAME).resolve()

_legacy_reference_path = os.getenv("REFERENCE_PATH")
if _legacy_reference_path:
    _legacy = Path(_legacy_reference_path).resolve()
    if not _legacy.is_relative_to(REFS_ROOT):
        raise RuntimeError(
            f"REFERENCE_PATH={_legacy_reference_path!r} is outside REFS_ROOT ({REFS_ROOT}), "
            "so no tool container can read it. Move the file into REFS_ROOT and set "
            "REFERENCE_FILENAME to its name instead."
        )
    REFERENCE = _legacy

SNPEFF_DB = os.getenv("SNPEFF_DB", "LSDV")

# --------------------------------------------------------------------------
# Broker / result backend
# --------------------------------------------------------------------------

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL

# How many runs may execute concurrently. Start at 1: on a single box, two
# concurrent Medaka/Clair3 stages contend for the same cores and both finish
# slower than they would back to back. Raise once run_stages tells you the
# real peak memory and wall time.
WORKER_CONCURRENCY = int(os.getenv("WORKER_CONCURRENCY", "1"))

# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    f"sqlite:///{BASE_DIR / 'dharini_pipeline.db'}",
)

# --------------------------------------------------------------------------
# Container execution
# --------------------------------------------------------------------------

DOCKER_BIN = os.getenv("DOCKER_BIN", "docker")

# Refuse to resolve an image that is not digest-pinned. Set true in production:
# an unpinned tag means a result cannot be traced to the code that produced it.
REQUIRE_PINNED_IMAGES = os.getenv("REQUIRE_PINNED_IMAGES", "false").lower() in (
    "1", "true", "yes",
)

# Ceiling applied to every stage regardless of what a pipeline requests, so a
# mis-declared StageSpec cannot claim the whole box.
MAX_STAGE_CPUS = float(os.getenv("MAX_STAGE_CPUS", "4"))
MAX_STAGE_MEMORY_GB = float(os.getenv("MAX_STAGE_MEMORY_GB", "8"))
DEFAULT_STAGE_TIMEOUT_S = int(os.getenv("DEFAULT_STAGE_TIMEOUT_S", "3600"))

# Sampling interval for the peak-memory recorder. Set to 0 to disable.
STATS_SAMPLE_INTERVAL_S = float(os.getenv("STATS_SAMPLE_INTERVAL_S", "2"))

# --------------------------------------------------------------------------
# Webhook delivery
# --------------------------------------------------------------------------

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
WEBHOOK_TIMEOUT_S = float(os.getenv("WEBHOOK_TIMEOUT_S", "15"))
WEBHOOK_MAX_ATTEMPTS = int(os.getenv("WEBHOOK_MAX_ATTEMPTS", "6"))

# Backoff schedule in seconds; index = attempt number - 1. Spans ~9 hours, so a
# consumer can be down for most of a working day without losing the delivery.
WEBHOOK_BACKOFF_S = [10, 60, 300, 1800, 7200, 21600]

# Only deliver to hosts matching this allowlist (comma-separated, empty = any).
# A callback URL is attacker-influenced input; without this the service is an
# SSRF pivot into your VPC.
WEBHOOK_ALLOWED_HOSTS = [
    h.strip() for h in os.getenv("WEBHOOK_ALLOWED_HOSTS", "").split(",") if h.strip()
]

# Callback hosts must resolve to a public address. Set true only when the
# consumer genuinely lives on a private address inside the same network -- it
# re-opens the SSRF surface, so pair it with WEBHOOK_ALLOWED_HOSTS.
#
# Loopback, link-local (the cloud metadata endpoint), multicast, reserved and
# unspecified addresses are rejected regardless of this setting.
WEBHOOK_ALLOW_PRIVATE_IPS = os.getenv("WEBHOOK_ALLOW_PRIVATE_IPS", "false").lower() in (
    "1", "true", "yes",
)

# Maximum number of parsed variant rows embedded in a webhook payload. Beyond
# this the consumer is expected to fetch the VCF artifact.
WEBHOOK_MAX_INLINE_VARIANTS = int(os.getenv("WEBHOOK_MAX_INLINE_VARIANTS", "2000"))

# --------------------------------------------------------------------------
# Public base URL used to build artifact download links in webhook payloads
# --------------------------------------------------------------------------

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")

# --------------------------------------------------------------------------
# Retention
# --------------------------------------------------------------------------

# Artifacts are on S3 once `publish` succeeds, so local disk is scratch. Six
# hours is enough to debug a run by hand; S3 lifecycle rules own the long term.
# A run whose artifacts have NOT reached S3 is never purged -- see tasks.py.
RETENTION_HOURS = int(os.getenv("RETENTION_HOURS", "6"))

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


# Where in the bucket this deployment lives. Empty means the bucket root;
# "app-data" puts every key under that folder. Configurable because a
# shared bucket is exactly the case where it differs between deployments --
# Dharini calls the same value S3_FOLDER, and the two MUST agree or this service
# looks for the upload somewhere Dharini never put it.
_raw_prefix = os.getenv("S3_PREFIX", "").strip().strip("/")

# Validated, not just normalised. This string is concatenated into every key
# and is mirrored in the IAM policy; a stray "..", backslash or control
# character produces keys that address objects nobody intended.
if _raw_prefix and (
    ".." in _raw_prefix.split("/")
    or "\\" in _raw_prefix
    or any(c in _raw_prefix for c in "\x00\n\r\t")
    or "//" in _raw_prefix
):
    raise RuntimeError(
        f"S3_PREFIX={_raw_prefix!r} is not a plain folder path. Use something "
        f"like 'app-data' or leave it empty for the bucket root."
    )

S3_PREFIX = f"{_raw_prefix}/" if _raw_prefix else ""

# The LEAF names are not configurable, deliberately. Together with S3_PREFIX
# they are written into the IAM policy -- GetObject on <prefix>uploads/*,
# PutObject on <prefix>runs/* -- so renaming one here without changing the
# policy produces AccessDenied on every run and nothing in the error says why.
# GET /health reports both, so a mismatch is at least visible.
#
# The trailing slash is load-bearing: `key.startswith("uploads")` would also
# accept "uploadsX/anything", which is a different namespace chosen by whoever
# supplied the key.
S3_ALLOWED_PREFIX = f"{S3_PREFIX}uploads/"
S3_OUTPUT_PREFIX = f"{S3_PREFIX}runs/"

# Static credentials. Left empty, boto3 falls back to its own chain -- on EC2
# that is the instance role, locally it is ~/.aws/credentials or an SSO session.
# Setting them here is therefore an OVERRIDE, not the only way in, and the same
# image runs either way: keys in a developer's .env, nothing at all on a host
# that has a role attached.
#
# Standard AWS names on purpose. The `aws` CLI, boto3's own chain and every
# other SDK read exactly these, so a credential that works in the shell works
# in the container with nothing translated.
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID", "").strip()
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY", "").strip()
# Only for temporary credentials from `sts assume-role` / SSO. A long-lived
# IAM user key does not have one.
AWS_SESSION_TOKEN = os.getenv("AWS_SESSION_TOKEN", "").strip()

if bool(AWS_ACCESS_KEY_ID) != bool(AWS_SECRET_ACCESS_KEY):
    # Half-configured is the worst of the three states: boto3 would silently
    # ignore the lone value and fall through to the credential chain, so the
    # process comes up looking configured and reaches whatever account the
    # host happens to be pointed at -- or nothing.
    raise RuntimeError(
        "AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY must be set together. "
        "Set both to use a static credential, or neither to use the instance "
        "role / ambient AWS config."
    )

S3_SSE = os.getenv("S3_SSE", "AES256").strip()
S3_KMS_KEY_ID = os.getenv("S3_KMS_KEY_ID", "").strip()
if S3_SSE == "aws:kms" and not S3_KMS_KEY_ID:
    raise RuntimeError("S3_SSE=aws:kms requires S3_KMS_KEY_ID.")

LOCAL_STORAGE_ROOT = Path(
    os.getenv("LOCAL_STORAGE_ROOT", str(RUNS_ROOT / "_objects"))
).resolve()

# The ceiling on an object this service is willing to fetch. Enforced twice:
# by HeadObject at submission time, so a too-large key is a 413 in milliseconds,
# and again as bytes land during `fetch`, because declared size is metadata and
# metadata is not a guarantee.
MAX_INPUT_BYTES = int(os.getenv("MAX_INPUT_BYTES", str(5 * 1024**3)))

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

