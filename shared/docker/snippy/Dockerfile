# Replicates the package set of the current application conda environment
# exactly, so snippy-fasta produces identical output after containerisation.
#
# snpeff is present because snippy declares it as a dependency; the annotate
# stage uses the separate shared snpeff image, which carries the LSDV database.
FROM mambaorg/micromamba:1.5-jammy

USER root
RUN micromamba install -y -n base -c bioconda -c conda-forge \
      snippy=4.6.0 \
      # PIN, LOAD-BEARING. snippy 4.6.0 declares only `vcflib >=1.0.0_rc3` --
      # no upper bound -- so conda resolved 1.0.10, whose vcfstreamsort and
      # vcfuniq are compiled with AVX-512 (EVEX-encoded: vpternlogd, %k1 mask
      # registers). Those raise SIGILL on any CPU without it: Apple Silicon's
      # x86 emulator, the hosted CI runner, and AMD EPYC Milan -- which is the
      # m6a instance this service runs on. snippy died at freebayes-parallel
      # with exit 132 on all three.
      #
      # Every published 1.0.2 build is plain SSE2 with no AVX-512 at all, and
      # 1.0.2 satisfies snippy's constraint. Verified by disassembling
      # vcfuniq out of each candidate package before choosing.
      #
      # This is the second time an unpinned TRANSITIVE dependency has broken a
      # tool here; the first removed a module NanoPlot imports. Pin what breaks
      # you, not just what you asked for.
      vcflib=1.0.2 \
      samtools \
      snpeff=5.4 \
      openjdk=21 \
      perl-bioperl \
      gffread \
      mummer4 \
 && micromamba clean -a -y

ENV PATH=/opt/conda/bin:$PATH
