# ARTIC align_trim has no usable image on Docker Hub, and upstream's own
# Dockerfile pins nothing -- `conda install align_trim samtools` resolves to
# whatever bioconda serves that day, so two runs a month apart trim with
# different code and nothing records it. Every version below is pinned for the
# same reason docker/core/Dockerfile is.
FROM mambaorg/micromamba:1.5-jammy

# samtools is pinned to the SAME version as docker/core/Dockerfile. align_trim
# streams SAM to stdout straight into `samtools sort`, and the sorted BAM is
# then read by minimap2's image, Medaka and Clair3 -- the two images must not
# disagree about BAM writing.
#
# align_trim is packaged as pure Python (pyhdfd78af_0) and drags in its own
# recent CPython, which is why it cannot simply join the `core` image.
USER root
RUN micromamba install -y -n base -c bioconda -c conda-forge \
      align_trim=1.2.0 \
      samtools=1.21 \
 && micromamba clean -a -y

ENV PATH=/opt/conda/bin:$PATH

# Deliberately no ENTRYPOINT. Upstream's image sets `ENTRYPOINT ["align_trim"]`
# and consequently has to override it with `--entrypoint bash` to run anything
# else; our runner always supplies a full argv, which an entrypoint would
# prepend itself to and corrupt.
