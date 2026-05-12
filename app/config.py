from pathlib import Path
import os
from dotenv import load_dotenv

load_dotenv()

# Dynamic base dir (works everywhere)
BASE_DIR = Path(__file__).resolve().parent.parent

DATA_DIR = BASE_DIR / "data"
RUNS_DIR = BASE_DIR / "runs"

REFERENCE = Path(os.getenv("REFERENCE_PATH", DATA_DIR / "lsdv_reference_sequence.fasta"))
SNPEFF_DB = os.getenv("SNPEFF_DB", "LSDV")

# Ensure directories exist
RUNS_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL

DOCKER_CONDA = "/opt/miniconda3/envs/pipeline"
DOCKER_CONDA_ALT = "/opt/conda/envs/pipeline"
MAC_CONDA = "/opt/homebrew/Caskroom/miniconda/base/envs/lsdv_pipeline"
