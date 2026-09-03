FROM continuumio/miniconda3:latest

LABEL description="BamClipper 1.0.0 for LSDV primer clipping"

# Install BamClipper and required dependencies
RUN conda install -y \
    -c bioconda \
    -c conda-forge \
    bamclipper=1.0.0 \
    samtools=1.21 \
    parallel \
 && conda clean -afy

WORKDIR /data
