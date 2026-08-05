"""The primer-scheme guard in ont_lsdv/primer.py.

The failure this defends against produced no error and no log line: a primer
BED written against a different genome makes align_trim match zero primers,
exit 0, and hand downstream an untrimmed BAM that the run records as trimmed.
"""

from __future__ import annotations

import pytest

from app.pipelines.ont_lsdv.primer import _validate_primer_scheme

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
