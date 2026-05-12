FROM --platform=linux/amd64 anaconda/miniconda

WORKDIR /app

RUN apt-get update && apt-get install -y \
    build-essential \
    wget \
    unzip \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Accept Anaconda Terms of Service
RUN conda tos accept --override-channels --channel https://repo.anaconda.com/pkgs/main && \
    conda tos accept --override-channels --channel https://repo.anaconda.com/pkgs/r

RUN conda create -n pipeline -y -c bioconda -c conda-forge \
    snippy=4.6.0 \
    samtools \
    snpeff=5.4 \
    openjdk=21 \
    perl-bioperl \
    gffread

SHELL ["conda", "run", "-n", "pipeline", "/bin/bash", "-c"]

ENV CONDA_PREFIX /opt/miniconda3/envs/pipeline
ENV PATH /opt/miniconda3/envs/pipeline/bin:$PATH

COPY . /app

RUN chmod +x /app/setup_snpeff_lsdv.sh && /app/setup_snpeff_lsdv.sh

RUN pip install --no-cache-dir -r requirements.txt

RUN mkdir -p /app/runs/uploads

ENV PYTHONUNBUFFERED=1

EXPOSE 8000

# Run uvicorn directly to ensure logs are visible
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "info"]
