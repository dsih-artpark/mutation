"""`tools/run_local.py` — the command line a pipeline author actually uses.

It sits between a developer's shell and the parameters a pipeline receives, so
a mis-parse here does not fail: it runs the pipeline with the wrong settings and
reports success. `--param primer.enabled=true` arriving as the *string* "true"
would enable nothing and say nothing.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from run_local import _parse_params  # noqa: E402


@pytest.mark.parametrize(
    "argument, expected",
    [
        # JSON-decoded, because the schema declares types and a shell only
        # produces strings.
        ("threads=8", {"threads": 8}),
        ("primer.enabled=true", {"primer": {"enabled": True}}),
        ("primer.enabled=false", {"primer": {"enabled": False}}),
        ("bcftools.max_depth=1000", {"bcftools": {"max_depth": 1000}}),
        # Not valid JSON, so it stays a string rather than becoming an error.
        ("variant.tool=clair3", {"variant": {"tool": "clair3"}}),
        ("reference=lsdv_reference_sequence.fasta",
         {"reference": "lsdv_reference_sequence.fasta"}),
        # A filename with a space survives; the shell already quoted it.
        ("reference=my ref.fasta", {"reference": "my ref.fasta"}),
    ],
)
def test_a_single_parameter_keeps_its_type(argument, expected):
    assert _parse_params([argument]) == expected


def test_dotted_keys_sharing_a_parent_merge():
    """Each --param must not replace the object the previous one built."""
    assert _parse_params(["bcftools.ploidy=1", "bcftools.max_depth=500"]) == {
        "bcftools": {"ploidy": 1, "max_depth": 500}
    }


def test_separate_parents_stay_separate():
    assert _parse_params(["variant.tool=medaka", "consensus.tool=bcftools"]) == {
        "variant": {"tool": "medaka"},
        "consensus": {"tool": "bcftools"},
    }


def test_a_later_parameter_wins_over_an_earlier_one():
    assert _parse_params(["threads=2", "threads=8"]) == {"threads": 8}


def test_a_pair_without_an_equals_sign_is_rejected():
    with pytest.raises(SystemExit, match="expects key=value"):
        _parse_params(["threads"])


def test_an_empty_value_is_a_string_not_an_error():
    """`--param reference=` is a mistake, but it is the schema's job to say so
    with the parameter's name and constraints, not this parser's."""
    assert _parse_params(["reference="]) == {"reference": ""}


def test_a_scalar_and_an_object_for_the_same_key_names_both_flags():
    """Bare dict subscription raises TypeError here, which says nothing about
    which two flags disagree."""
    with pytest.raises(SystemExit, match="conflicts with an earlier value"):
        _parse_params(["primer=1", "primer.enabled=true"])


def test_the_reverse_conflict_is_caught_too():
    with pytest.raises(SystemExit, match="would replace the object"):
        _parse_params(["primer.enabled=true", "primer=1"])
