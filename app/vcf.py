"""Shared VCF parsing.

Both pipelines need the same thing: turn an snpEff-annotated VCF into rows the
consumer can store and query without re-parsing text. Keeping this in one place
means a fix to the ANN-field handling benefits every pipeline at once.
"""

from __future__ import annotations

import gzip
from pathlib import Path

# snpEff ANN subfield order (snpEff documentation, "ANN field" section).
_ANN_ALLELE, _ANN_EFFECT, _ANN_IMPACT, _ANN_GENE = 0, 1, 2, 3
_ANN_HGVS_C, _ANN_HGVS_P = 9, 10


def _open(path: Path):
    if str(path).endswith(".gz"):
        return gzip.open(path, "rt", errors="replace")
    return open(path, "r", errors="replace")


def _field(parts: list[str], index: int) -> str:
    return parts[index] if len(parts) > index else ""


def parse_annotated_vcf(path: Path, limit: int | None = None) -> list[dict]:
    """Parse a VCF into variant rows.

    Rows carry annotation fields when snpEff has run, and are still produced for
    un-annotated VCFs (effect/gene simply come back empty) so this works for any
    caller in the chain.
    """
    path = Path(path)
    if not path.exists():
        return []

    rows: list[dict] = []
    with _open(path) as handle:
        for line in handle:
            if line.startswith("#"):
                continue
            fields = line.rstrip("\n").split("\t")
            if len(fields) < 8:
                continue

            chrom, pos, _id, ref, alt, qual, filt, info = fields[:8]

            row = {
                "chrom": chrom,
                "pos": _int(pos),
                "ref": ref,
                "alt": alt,
                "qual": _float(qual),
                "filter": filt,
                "effect": "",
                "impact": "",
                "gene": "",
                "hgvs_c": "",
                "hgvs_p": "",
                "depth": None,
                "allele_freq": None,
            }

            for item in info.split(";"):
                if item.startswith("ANN="):
                    first = item[4:].split(",")[0].split("|")
                    row["effect"] = _field(first, _ANN_EFFECT)
                    row["impact"] = _field(first, _ANN_IMPACT)
                    row["gene"] = _field(first, _ANN_GENE)
                    row["hgvs_c"] = _field(first, _ANN_HGVS_C)
                    row["hgvs_p"] = _field(first, _ANN_HGVS_P)
                elif item.startswith("DP="):
                    row["depth"] = _int(item[3:])
                elif item.startswith("AF="):
                    row["allele_freq"] = _float(item[3:].split(",")[0])

            rows.append(row)
            if limit is not None and len(rows) >= limit:
                break

    return rows


def summarise(rows: list[dict]) -> dict:
    """Headline numbers for the run record."""
    by_impact: dict[str, int] = {}
    by_effect: dict[str, int] = {}
    snps = indels = 0

    for row in rows:
        if row.get("impact"):
            by_impact[row["impact"]] = by_impact.get(row["impact"], 0) + 1
        if row.get("effect"):
            by_effect[row["effect"]] = by_effect.get(row["effect"], 0) + 1
        ref, alt = row.get("ref", ""), row.get("alt", "")
        if len(ref) == 1 and len(alt) == 1:
            snps += 1
        else:
            indels += 1

    return {
        "variant_count": len(rows),
        "snp_count": snps,
        "indel_count": indels,
        "by_impact": by_impact,
        "by_effect": dict(sorted(by_effect.items(), key=lambda kv: -kv[1])[:10]),
    }


def count_variants(path: Path) -> int:
    path = Path(path)
    if not path.exists():
        return 0
    count = 0
    with _open(path) as handle:
        for line in handle:
            if not line.startswith("#") and line.strip():
                count += 1
    return count


def _int(value: str):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value: str):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
