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

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

DATA_DIR = BASE_DIR / "data"

# Absolute, identical host-side and container-side. See module docstring.
RUNS_ROOT = Path(os.getenv("RUNS_ROOT", str(BASE_DIR / "runs"))).resolve()
REFS_ROOT = Path(os.getenv("REFS_ROOT", str(DATA_DIR))).resolve()

# Retained for backwards compatibility with visualization.py and the legacy
# /status and /visualization endpoints.
RUNS_DIR = RUNS_ROOT

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

RETENTION_HOURS = int(os.getenv("RETENTION_HOURS", "72"))

# --------------------------------------------------------------------------
# Upload limits
# --------------------------------------------------------------------------

MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_SIZE", str(5 * 1024**3)))
