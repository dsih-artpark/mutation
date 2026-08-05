# Replaces the untracked Dockerfile_lsdv_core_v4, which pinned nothing and drew
# from Ubuntu apt -- so a rebuild silently changed tool versions, and the tools
# were several years old.
FROM mambaorg/micromamba:1.5-jammy

USER root
RUN micromamba install -y -n base -c bioconda -c conda-forge \
      minimap2=2.28 \
      samtools=1.21 \
      bcftools=1.21 \
      fastp=0.24.0 \
      nanofilt=2.8.0 \
      nanoplot=1.42.0 \
 && micromamba clean -a -y

ENV PATH=/opt/conda/bin:$PATH
