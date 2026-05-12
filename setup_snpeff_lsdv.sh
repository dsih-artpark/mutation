#!/usr/bin/env bash

set -euo pipefail

# ===== CONFIG =====
# ACCESSION="MH893760"
ACCESSION="NC_003027"
DB_NAME="LSDV"
PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
CONFIG_FILE=$(ls -d $CONDA_PREFIX/share/snpeff-* | head -n 1)/snpEff.config
SNPEFF_BASE_DIR=$(dirname "$CONFIG_FILE")
SNPEFF_DATA_DIR="${SNPEFF_BASE_DIR}/data"

if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "❌ Could not locate snpEff.config"
    exit 1
fi

echo "📄 Using config: $CONFIG_FILE"

WORK_DIR="$(pwd)/snpeff_build"
RETRIES=2
SLEEP=5

echo "🔧 Setting up SnpEff database for ${DB_NAME}"

# ===== CHECK DEPENDENCIES =====
command -v snpEff >/dev/null || { echo "❌ snpEff not found"; exit 1; }
command -v curl >/dev/null || { echo "❌ curl not found"; exit 1; }

mkdir -p "$WORK_DIR"
cd "$WORK_DIR"

# ===== DOWNLOAD FUNCTION =====
download_with_retry() {
    local url=$1
    local output=$2

    for i in $(seq 1 $RETRIES); do
        echo "⬇️ Attempt $i: $url"
        if curl -fsSL "$url" -o "$output"; then
            return 0
        fi
        echo "⚠️ Retry in ${SLEEP}s..."
        sleep $SLEEP
    done

    echo "❌ Failed to download $url"
    exit 1
}

# ===== DOWNLOAD FILES =====
echo "📥 Downloading FASTA..."
download_with_retry \
"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=nuccore&id=${ACCESSION}&rettype=fasta&retmode=text" \
"sequences.fa"

echo "📥 Downloading GFF..."
download_with_retry \
"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=nuccore&id=${ACCESSION}&rettype=gff3&retmode=text" \
"genes.gff"

# ===== VALIDATION =====
echo "🔍 Validating downloads..."

if [[ ! -s sequences.fa ]]; then
    echo "❌ FASTA download failed"
    exit 1
fi

if [[ ! -s genes.gff ]]; then
    echo "❌ GFF download failed"
    exit 1
fi

if ! grep -q "##gff-version 3" genes.gff; then
    echo "❌ Invalid GFF format"
    exit 1
fi

echo "✅ Files validated"

# ===== EXPORT REFERENCE FOR SNIPPY =====
APP_DATA_DIR="${PROJECT_ROOT}/data"

echo "📦 Exporting reference genome for Snippy..."

mkdir -p "$APP_DATA_DIR"

cp sequences.fa "${APP_DATA_DIR}/lsdv_reference_sequence.fasta"
cp genes.gff "${APP_DATA_DIR}/lsdv_reference_sequence.gff"

echo "✅ Reference genome saved to ${APP_DATA_DIR}/lsdv_reference_sequence.fasta"

# ===== COPY TO SNPEFF =====
TARGET_DIR="${SNPEFF_DATA_DIR}/${DB_NAME}"

echo "📁 Copying to SnpEff directory..."
mkdir -p "$TARGET_DIR"

cp sequences.fa "${TARGET_DIR}/sequences.fa"
cp genes.gff "${TARGET_DIR}/genes.gff"

# ===== UPDATE CONFIG =====
if ! grep -q "${DB_NAME}.genome" "$CONFIG_FILE"; then
    echo "✏️ Updating snpEff.config..."
    echo "${DB_NAME}.genome : ${DB_NAME}" >> "$CONFIG_FILE"
fi

# ===== BUILD DATABASE =====
echo "🏗️ Building SnpEff database..."

for i in $(seq 1 $RETRIES); do
    if snpEff build -gff3 -noCheckCds -noCheckProtein -v "$DB_NAME"; then
    # if snpEff build -gff3 -v "$DB_NAME"; then
        echo "✅ SnpEff database built successfully"
        exit 0
    fi
    echo "⚠️ Build failed, retrying..."
    sleep $SLEEP
done

echo "❌ Failed to build SnpEff database"
exit 1
