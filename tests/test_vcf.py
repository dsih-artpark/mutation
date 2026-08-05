"""VCF parsing and summarisation."""

import gzip

import pytest

from app.vcf import count_variants, parse_annotated_vcf, summarise

SAMPLE = (
    "##fileformat=VCFv4.2\n"
    "##contig=<ID=LSDV,length=150773>\n"
    "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
    "LSDV\t1042\t.\tA\tG\t228.4\tPASS\tDP=120;AF=0.98;"
    "ANN=G|missense_variant|MODERATE|LSDV008|gene|transcript|p1|c|1|c.100A>G|p.Lys34Glu\n"
    "LSDV\t2500\t.\tGT\tG\t190\tPASS\tDP=88;"
    "ANN=G|frameshift_variant|HIGH|LSDV011|gene|transcript|p1|c|1|c.55delT|p.Ser19fs\n"
    "LSDV\t9000\t.\tC\tT\t45\tLowQual\tDP=12\n"
)


@pytest.fixture
def vcf(tmp_path):
    path = tmp_path / "annotated.vcf"
    path.write_text(SAMPLE)
    return path


def test_parses_all_records(vcf):
    assert len(parse_annotated_vcf(vcf)) == 3


def test_extracts_snpeff_annotation_fields(vcf):
    first = parse_annotated_vcf(vcf)[0]
    assert first["chrom"] == "LSDV"
    assert first["pos"] == 1042
    assert first["effect"] == "missense_variant"
    assert first["impact"] == "MODERATE"
    assert first["gene"] == "LSDV008"
    assert first["hgvs_c"] == "c.100A>G"
    assert first["hgvs_p"] == "p.Lys34Glu"
    assert first["depth"] == 120
    assert first["allele_freq"] == 0.98


def test_unannotated_records_still_parse(vcf):
    """Works for any caller in the chain, not only post-snpEff files."""
    third = parse_annotated_vcf(vcf)[2]
    assert third["pos"] == 9000
    assert third["effect"] == ""
    assert third["filter"] == "LowQual"
    assert third["allele_freq"] is None


def test_reads_gzipped_vcf(tmp_path):
    path = tmp_path / "annotated.vcf.gz"
    with gzip.open(path, "wt") as handle:
        handle.write(SAMPLE)
    assert len(parse_annotated_vcf(path)) == 3


def test_limit_caps_rows(vcf):
    assert len(parse_annotated_vcf(vcf, limit=2)) == 2


def test_missing_file_returns_empty(tmp_path):
    assert parse_annotated_vcf(tmp_path / "nope.vcf") == []


def test_summary_classifies_snps_and_indels(vcf):
    stats = summarise(parse_annotated_vcf(vcf))
    assert stats["variant_count"] == 3
    assert stats["snp_count"] == 2
    assert stats["indel_count"] == 1
    assert stats["by_impact"] == {"MODERATE": 1, "HIGH": 1}


def test_count_variants_skips_headers(vcf):
    assert count_variants(vcf) == 3
