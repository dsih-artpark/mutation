FROM mambaorg/micromamba:1.5-jammy

USER root
RUN micromamba install -y -n base -c bioconda -c conda-forge \
      python=3.11 \
      fastp=1.3.3 \
      fastqc=0.12.1 \
      trimmomatic=0.41 \
      bwa=0.7.18 \
      bowtie2=2.5.5 \
      samtools=1.22.1 \
      bcftools=1.22 \
      ivar=1.4.4 \
      lofreq=2.1.5 \
      freebayes=1.3.10 \
 && micromamba clean -a -y

ENV PATH=/opt/conda/bin:$PATH

# Give Trimmomatic's adapter file a stable path across Conda build revisions.
RUN set -eux; \
    adapter="$(find /opt/conda -path '*/adapters/TruSeq3-PE.fa' -print -quit)"; \
    test -n "$adapter"; \
    mkdir -p /opt/trimmomatic; \
    ln -s "$adapter" /opt/trimmomatic/TruSeq3-PE.fa

WORKDIR /work
