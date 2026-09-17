"""The primer-scheme guards in ont_lsdv/primer.py.

The failures these defend against produce no error and no log line: a primer
BED written against a different genome makes align_trim match zero primers,
exit 0, and hand downstream an untrimmed BAM that the run records as trimmed.
BAMClipper is worse -- it accepts an ARTIC BED as if it were primer pairs.
"""

from __future__ import annotations

import pytest

from app.pipelines.ont_lsdv.primer import _validate_primer_pairs, _validate_primer_scheme

# Real values, not invented ones. The scheme committed alongside the upstream
# ONT pipeline is ARTIC's SARS-CoV-2 v3; the reference it ships is LSDV.
SARS_COV_2_BED = (
    "MN908947.3\t30\t54\tnCoV-2019_1_LEFT_1\t1\t+\tACCAACCAACTTTCGATCTCTTGT\n"
    "MN908947.3\t385\t410\tnCoV-2019_1_RIGHT_1\t1\t-\tCATCTTTAAGATGTTGACGTGCCTC\n"
)
LSDV_FAI = "NC_003027.1\t150773\t76\t70\t71\n"


def _write(tmp_path, bed_text, fai_text):
    bed = tmp_path / "primers.bed"
    bed.write_text(bed_text)
    reference = tmp_path / "reference.fasta"
    reference.write_text(">NC_003027.1\nACGT\n")
    if fai_text is not None:
        (tmp_path / "reference.fasta.fai").write_text(fai_text)
    return bed, reference


def test_rejects_a_scheme_for_another_genome(tmp_path):
    bed, reference = _write(tmp_path, SARS_COV_2_BED, LSDV_FAI)

    with pytest.raises(ValueError) as excinfo:
        _validate_primer_scheme(bed, reference)

    message = str(excinfo.value)
    # Both names must appear: "the scheme is wrong" is not actionable, "it says
    # MN908947.3 and the reference is NC_003027.1" is.
    assert "MN908947.3" in message
    assert "NC_003027.1" in message


def test_accepts_a_scheme_naming_a_reference_contig(tmp_path):
    bed, reference = _write(
        tmp_path,
        "NC_003027.1\t100\t124\tLSDV_1_LEFT\t1\t+\tACGTACGTACGT\n",
        LSDV_FAI,
    )

    _validate_primer_scheme(bed, reference)


def test_rejects_a_six_column_bed(tmp_path):
    """The classic ARTIC layout stops at `strand`. align_trim 1.2.0 needs the
    primer sequence too, and without it primalbedtools raises IndexError three
    frames inside a library -- which tells the operator nothing about the file
    they supplied."""
    bed, reference = _write(
        tmp_path,
        "NC_003027.1\t100\t124\tLSDV_1_LEFT\t1\t+\n",
        LSDV_FAI,
    )

    with pytest.raises(ValueError, match="7 columns"):
        _validate_primer_scheme(bed, reference)


def test_rejects_an_empty_scheme(tmp_path):
    bed, reference = _write(tmp_path, "# header only\n", LSDV_FAI)

    with pytest.raises(ValueError, match="no primer rows"):
        _validate_primer_scheme(bed, reference)


def test_ignores_bed_header_lines(tmp_path):
    """A `track` line's first field is not a contig name and must not be read
    as one, or a validly-headed BED is rejected for naming a contig called
    "track"."""
    bed, reference = _write(
        tmp_path,
        'track name="LSDV scheme"\n#comment\nNC_003027.1\t100\t124\tLSDV_1_LEFT\t1\t+\tACGTACGTACGT\n',
        LSDV_FAI,
    )

    _validate_primer_scheme(bed, reference)


def test_passes_when_the_reference_has_no_index(tmp_path):
    """prepare_reference always writes a .fai, but the guard must not turn a
    missing index into a spurious primer-scheme error -- that would send the
    operator looking at the wrong file."""
    bed, reference = _write(tmp_path, SARS_COV_2_BED, None)

    _validate_primer_scheme(bed, reference)


# ------------------------------------------------------------ BAMClipper BEDPE

# The first rows of LSDV-3.5.bed, a scheme uploaded to Dharini and run through
# BAMClipper, which accepted it without complaint.
LSDV_ARTIC_BED = (
    "NC_003027.1\t286\t311\tscheme _1_L\t1\t+\n"
    "NC_003027.1\t3688\t3714\tscheme _1_R\t1\t-\n"
)
LSDV_BEDPE = "NC_003027.1\t286\t311\tNC_003027.1\t3688\t3714\tamplicon_1\n"


def _write_pairs(tmp_path, bedpe_text, fai_text=LSDV_FAI):
    _, reference = _write(tmp_path, "", fai_text)
    pairs = tmp_path / "primers.bedpe"
    pairs.write_text(bedpe_text)
    return pairs, reference


def test_accepts_primer_pairs_on_a_reference_contig(tmp_path):
    pairs, reference = _write_pairs(tmp_path, "# amplicons\n\n" + LSDV_BEDPE)

    _validate_primer_pairs(pairs, reference)


def test_accepts_primer_pairs_without_an_amplicon_name(tmp_path):
    pairs, reference = _write_pairs(tmp_path, "NC_003027.1\t286\t311\tNC_003027.1\t3688\t3714\n")

    _validate_primer_pairs(pairs, reference)


@pytest.mark.parametrize("scheme", [LSDV_ARTIC_BED, SARS_COV_2_BED])
def test_rejects_an_artic_bed_given_as_primer_pairs(tmp_path, scheme):
    """Both have at least six columns, which is all BAMClipper checks."""
    pairs, reference = _write_pairs(tmp_path, scheme)

    with pytest.raises(ValueError, match="looks like an ARTIC primer BED"):
        _validate_primer_pairs(pairs, reference)


def test_rejects_space_separated_pairs(tmp_path):
    """BAMClipper splits on tabs, so a space-separated row is one field to it."""
    pairs, reference = _write_pairs(tmp_path, LSDV_BEDPE.replace("\t", " "))

    with pytest.raises(ValueError, match="separated by tabs"):
        _validate_primer_pairs(pairs, reference)


def test_rejects_a_short_row(tmp_path):
    pairs, reference = _write_pairs(tmp_path, "NC_003027.1\t286\t311\n")

    with pytest.raises(ValueError, match="6 columns"):
        _validate_primer_pairs(pairs, reference)


def test_rejects_non_numeric_coordinates(tmp_path):
    pairs, reference = _write_pairs(tmp_path, "NC_003027.1\t286\t311\tNC_003027.1\tstart\tend\n")

    with pytest.raises(ValueError, match="whole-number coordinates"):
        _validate_primer_pairs(pairs, reference)


def test_rejects_primers_on_different_contigs(tmp_path):
    pairs, reference = _write_pairs(tmp_path, "NC_003027.1\t286\t311\tchr2\t3688\t3714\n")

    with pytest.raises(ValueError, match="different contigs"):
        _validate_primer_pairs(pairs, reference)


@pytest.mark.parametrize("row", [
    "NC_003027.1\t311\t286\tNC_003027.1\t3688\t3714\n",   # left primer reversed
    "NC_003027.1\t3688\t3714\tNC_003027.1\t286\t311\n",   # right primer first
])
def test_rejects_misordered_coordinates(tmp_path, row):
    pairs, reference = _write_pairs(tmp_path, row)

    with pytest.raises(ValueError, match="left primer must end before"):
        _validate_primer_pairs(pairs, reference)


def test_rejects_pairs_for_another_genome(tmp_path):
    pairs, reference = _write_pairs(tmp_path, "MN908947.3\t30\t54\tMN908947.3\t385\t410\n")

    with pytest.raises(ValueError) as excinfo:
        _validate_primer_pairs(pairs, reference)

    assert "MN908947.3" in str(excinfo.value)
    assert "NC_003027.1" in str(excinfo.value)


def test_rejects_empty_pairs(tmp_path):
    pairs, reference = _write_pairs(tmp_path, "# header only\n")

    with pytest.raises(ValueError, match="no primer pairs"):
        _validate_primer_pairs(pairs, reference)
