# 🧬 LSDV Variant Calling & Annotation Pipeline

A production-ready pipeline for **variant calling (Snippy)** and **variant annotation (SnpEff)** exposed via a **FastAPI service**, with optional background processing using **Celery + Redis**.

---

## 🚀 Features

* Upload one or multiple FASTA files via API
* Variant calling using Snippy
* Variant annotation using SnpEff (LSDV database)
* Async processing with Celery
* Job status tracking
* Dockerized for production
* Local development support (Linux/macOS)

---

## 🧱 Tech Stack

* FastAPI (API layer)
* Celery + Redis (background jobs)
* Snippy (variant calling)
* SnpEff (annotation)
* Conda (environment management)

---

## ⚙️ Prerequisites (Linux/macOS)

Make sure you have:

* Python 3.10+
* Git
* curl or wget
* Java (OpenJDK 11+)

Check Java:

```bash
java -version
```

---

## 🐍 Step 1: Install Miniconda

We recommend installing **Miniconda** (lightweight Conda).

### Download installer

#### macOS (Apple Silicon)

```bash
curl -O https://repo.anaconda.com/miniconda/Miniconda3-latest-MacOSX-arm64.sh
```

#### macOS (Intel) / Linux

```bash
curl -O https://repo.anaconda.com/miniconda/Miniconda3-latest-MacOSX-x86_64.sh
```

### Install

```bash
bash Miniconda3-latest-*.sh
```

Follow prompts:

* Accept license
* Use default install path
* Type `yes` to initialize

### Reload shell

```bash
source ~/.zshrc   # or ~/.bashrc
```

Verify:

```bash
conda --version
```

---

## 🧪 Step 2: Create environment

```bash
conda create -n lsdv_pipeline python=3.10 -y
conda activate lsdv_pipeline
```

---

## 🧬 Step 3: Install bioinformatics tools

```bash
conda install -c bioconda -c conda-forge snippy=4.6.0 samtools=1.17 snpeff openjdk=21 perl-bioperl -y
```

---

## 📥 Step 4: Download SnpEff database

Make it executable

```bash
chmod +x setup_snpeff_lsdv.sh
```

Then run it

```bash
./setup_snpeff_lsdv.sh
```

---

## 📦 Step 5: Install Python dependencies

```bash
pip install -r requirements.txt
```

---

## 🔐 Step 6: Configure environment variables

Create `.env` file:

```env
APP_ENV=development

REDIS_URL=redis://localhost:6379/0

SNPEFF_DB=LSDV
REFERENCE_PATH=./data/lsdv_reference_sequence.fasta

MAX_UPLOAD_SIZE=524288000
MAX_RETRIES=2
RETRY_DELAY=5
```

---

## 📁 Step 7: Project setup

Ensure reference file exists:

```bash
data/lsdv_reference_sequence.fasta
```

Create runtime directories:

```bash
mkdir -p runs/uploads
```

---

## 🧠 Step 8: Run Redis

### macOS (Homebrew)

```bash
brew install redis
brew services start redis
```

### Linux

```bash
sudo apt install redis-server
sudo systemctl start redis
```

Verify:

```bash
redis-cli ping
# Output: PONG
```

---

## ⚙️ Step 9: Run Celery worker

```bash
celery -A app.worker.celery worker --loglevel=info
```

---

## 🚀 Step 10: Run FastAPI server

```bash
uvicorn app.main:app --reload
```

---

## 🌐 Access API

Open:
[http://localhost:8000/docs](http://localhost:8000/docs)

---

## 📡 API Usage

### Upload FASTA files

```bash
curl -X POST http://localhost:8000/upload \
  -F "files=@sample1.fasta" \
  -F "files=@sample2.fasta"
```

Response:

```json
{
  "job_ids": ["id1", "id2"]
}
```

---

### Check job status

```bash
curl http://localhost:8000/status/<job_id>
```

---

## 🧪 Development Mode (without Celery)

For easier debugging, you can run tasks synchronously.

Modify `main.py`:

```python
# Instead of:
task = process_fasta.delay(path)

# Use:
result = process_fasta(path)
```

---

## ⚠️ Troubleshooting

### Snippy not found

```bash
which snippy
```

→ Ensure conda env is active

---

### SnpEff fails

```bash
snpEff databases
```

→ Ensure LSDV is downloaded

---

### Java issues

```bash
java -version
```

→ Must be **OpenJDK 21** (for modern SnpEff compatibility).

### macOS: Library or Java Mismatch
If you see `Library not loaded` or `UnsupportedClassVersionError`, ensure your system's global `JAVA_HOME` is not overriding the Conda version. The pipeline is configured to automatically handle this, but you can manually check:
```bash
unset JAVA_HOME
```

---

### Redis not working

```bash
redis-cli ping
```

→ Should return `PONG`

---

### Permission issues

```bash
chmod -R 755 runs/
```

---

## 🧹 Cleanup (optional)

Remove old runs:

```bash
rm -rf runs/*
```

---

## 🐳 Docker (Production)

```bash
docker-compose up --build
```

---

## 🧠 Best Practices

* Use one Conda env per project
* Keep `.env` out of Git
* Avoid installing tools in base env
* Use Celery for heavy workloads
* Monitor disk usage (VCF files can grow large)
