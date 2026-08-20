# Replicates the package set of the current application conda environment
# exactly, so snippy-fasta produces identical output after containerisation.
#
# snpeff is present because snippy declares it as a dependency; the annotate
# stage uses the separate shared snpeff image, which carries the LSDV database.
FROM mambaorg/micromamba:1.5-jammy

USER root
RUN micromamba install -y -n base -c bioconda -c conda-forge \
      snippy=4.6.0 \
      samtools \
      snpeff=5.4 \
      openjdk=21 \
      perl-bioperl \
      gffread \
      mummer4 \
 && micromamba clean -a -y

ENV PATH=/opt/conda/bin:$PATH
