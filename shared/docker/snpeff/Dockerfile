# snpEff pinned at 5.4 -- the version snippy-fasta uses today -- with the LSDV
# database built in. Parameterised by accession so a second pathogen is a
# manifest entry rather than a second Dockerfile.
FROM mambaorg/micromamba:1.5-jammy

ARG ACCESSION=NC_003027
ARG DB_NAME=LSDV

USER root
RUN micromamba install -y -n base -c bioconda -c conda-forge \
      snpeff=5.4 openjdk=21 curl \
 && micromamba clean -a -y
ENV PATH=/opt/conda/bin:$PATH

# `snpEff build` can exit 0 having produced no usable database, which would only
# surface during a real annotation. Assert on the artifact at build time.
RUN set -eux; \
    share="$(ls -d /opt/conda/share/snpeff-* | head -n1)"; \
    cfg="$share/snpEff.config"; \
    d="$share/data/${DB_NAME}"; mkdir -p "$d"; \
    base="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=nuccore&id=${ACCESSION}"; \
    curl -fsSL --retry 3 --retry-delay 5 "${base}&rettype=fasta&retmode=text" -o "$d/sequences.fa"; \
    curl -fsSL --retry 3 --retry-delay 5 "${base}&rettype=gff3&retmode=text"  -o "$d/genes.gff"; \
    test -s "$d/sequences.fa"; \
    grep -q '##gff-version 3' "$d/genes.gff"; \
    grep -q "^${DB_NAME}.genome" "$cfg" || echo "${DB_NAME}.genome : ${DB_NAME}" >> "$cfg"; \
    snpEff build -gff3 -noCheckCds -noCheckProtein -v "${DB_NAME}"; \
    test -s "$d/snpEffectPredictor.bin"

ENV SNPEFF_CONFIG=/opt/conda/share/snpeff-5.4-0/snpEff.config
