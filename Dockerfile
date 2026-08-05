FROM --platform=linux/amd64 anaconda/miniconda

WORKDIR /app

RUN apt-get update && apt-get install -y \
    build-essential \
    wget \
    unzip \
    curl \
    ca-certificates \
    gnupg \
    && rm -rf /var/lib/apt/lists/*

# Docker CLI only -- no daemon. The worker talks to the host's daemon over the
# mounted socket to start per-tool containers (see docker-compose.yml).
RUN install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg \
       | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
    && chmod a+r /etc/apt/keyrings/docker.gpg \
    && echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.gpg] \
       https://download.docker.com/linux/debian $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
       > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y docker-ce-cli \
    && rm -rf /var/lib/apt/lists/*

# Accept Anaconda Terms of Service
RUN conda tos accept --override-channels --channel https://repo.anaconda.com/pkgs/main && \
    conda tos accept --override-channels --channel https://repo.anaconda.com/pkgs/r

# Tools for the snippy-fasta pipeline, which runs them directly rather than in
# per-tool containers. The ONT pipeline needs none of these -- its tools live in
# their own images precisely because they cannot coexist in one environment.
RUN conda create -n pipeline -y -c bioconda -c conda-forge \
    snippy=4.6.0 \
    samtools \
    snpeff=5.4 \
    openjdk=21 \
    perl-bioperl \
    gffread \
    mummer4

SHELL ["conda", "run", "-n", "pipeline", "/bin/bash", "-c"]

ENV CONDA_PREFIX=/opt/miniconda3/envs/pipeline
ENV PATH=/opt/miniconda3/envs/pipeline/bin:$PATH

# Dependencies before source, so a code change does not invalidate the conda
# and pip layers.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app

RUN chmod +x /app/setup_snpeff_lsdv.sh && /app/setup_snpeff_lsdv.sh

ENV PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "info"]
