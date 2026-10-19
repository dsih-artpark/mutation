FROM mambaorg/micromamba:1.5-jammy

LABEL description="BamClipper 1.0.0 for LSDV primer clipping"

USER root
RUN micromamba install -y -n base -c bioconda -c conda-forge \
      bamclipper=1.0.0 \
      samtools=1.21 \
      parallel \
 && micromamba clean -a -y

ENV PATH=/opt/conda/bin:$PATH

WORKDIR /data
