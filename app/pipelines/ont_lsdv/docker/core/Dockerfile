# Replaces the untracked Dockerfile_lsdv_core_v4, which pinned nothing and drew
# from Ubuntu apt -- so a rebuild silently changed tool versions, and the tools
# were several years old.
FROM mambaorg/micromamba:1.5-jammy

# python-kaleido is a transitive dependency, pinned here because pinning only the
# direct ones is what let this image regress. nanoplotter/plot.py does
# `from kaleido.scopes.plotly import PlotlyScope`; that module was removed in
# kaleido 1.x, so an unconstrained solve takes 1.3.0 and NanoPlot dies at import
# -- and NanoPlot is the default qc.tool. 0.2.1 is the last of the 0.x line.
USER root
RUN micromamba install -y -n base -c bioconda -c conda-forge \
      minimap2=2.28 \
      samtools=1.21 \
      bcftools=1.21 \
      fastp=0.24.0 \
      nanofilt=2.8.0 \
      nanoplot=1.42.0 \
      'plotly<7' \
      python-kaleido=0.2.1 \
 && micromamba clean -a -y

ENV PATH=/opt/conda/bin:$PATH
