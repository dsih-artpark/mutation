"""Quality control: NanoPlot (+ optional NanoFilt) or fastp."""

from __future__ import annotations

import re
from pathlib import Path

from ...runtime.context import RunContext
from ...runtime.runner import sh
from . import specs

FILTERED = "filtered.fastq.gz"


def run(ctx: RunContext) -> Path:
    """Execute QC. Returns the FASTQ downstream stages should use."""
    tool = str(ctx.param("qc.tool", "nanoplot")).lower()
    fastq = ctx.input_path

    with ctx.stage("qc"):
        if tool == "nanoplot":
            stats = _nanoplot(ctx, fastq)
            if ctx.param("nanofilt.enabled", False):
                return _nanofilt(ctx, fastq, stats)
            return fastq
        if tool == "fastp":
            return _fastp(ctx, fastq)
        raise ValueError(f"unsupported QC tool: {tool}")


# ---------------------------------------------------------------- NanoPlot

def _nanoplot(ctx: RunContext, fastq: Path) -> Path:
    outdir = ctx.work_dir / "qc"
    outdir.mkdir(parents=True, exist_ok=True)

    ctx.run(
        specs.NANOPLOT,
        ["NanoPlot", "--fastq", str(fastq), "--outdir", str(outdir), "-t", str(_threads(ctx, 2))],
    )

    stats = outdir / "NanoStats.txt"
    report = outdir / "NanoPlot-report.html"
    if report.exists():
        ctx.artifact(report, kind="qc", label="NanoPlot report")

    if stats.exists():
        parsed = parse_nanostats(stats)
        for key, value in parsed.items():
            ctx.metric(f"qc_{key}", value)
        # Copy rather than publish: NanoFilt still needs to read it below.
        ctx.artifact(_copy(stats, ctx.work_dir / "NanoStats.txt"), kind="qc", label="NanoPlot statistics")
    return stats


def _nanofilt(ctx: RunContext, fastq: Path, stats: Path) -> Path:
    if str(ctx.param("filter.mode", "recommended")).lower() == "recommended":
        if not stats.exists():
            ctx.log("[qc] NanoStats.txt missing; falling back to manual thresholds")
            quality, length = _manual(ctx)
        else:
            quality, length = recommended_thresholds(parse_nanostats(stats))
            ctx.log(f"[qc] recommended NanoFilt thresholds: -q {quality} -l {length}")
    else:
        quality, length = _manual(ctx)

    ctx.metric("nanofilt_min_quality", quality)
    ctx.metric("nanofilt_min_length", length)

    output = ctx.work_dir / FILTERED

    # A pipe genuinely needs a shell here. The script is a literal constant and
    # every value arrives through a quoted environment variable, so a filename
    # containing shell metacharacters is inert.
    #
    # `gzip -cdf` decompresses gzipped input and passes plain input through
    # unchanged, so this works whether or not the upload was compressed.
    ctx.run(
        specs.NANOFILT,
        sh('gzip -cdf "$FQ" | NanoFilt -q "$Q" -l "$L" '
           '--headcrop "$HEAD" --tailcrop "$TAIL" | gzip > "$OUT"'),
        env={
            "FQ": str(fastq),
            "Q": str(quality),
            "L": str(length),
            "HEAD": str(int(ctx.param("nanofilt.headcrop", 0))),
            "TAIL": str(int(ctx.param("nanofilt.tailcrop", 0))),
            "OUT": str(output),
        },
    )
    ctx.require(output)
    return output


# ------------------------------------------------------------------- fastp

def _fastp(ctx: RunContext, fastq: Path) -> Path:
    output = ctx.work_dir / FILTERED
    html = ctx.work_dir / "fastp.html"
    report_json = ctx.work_dir / "fastp.json"

    ctx.run(
        specs.FASTP,
        [
            "fastp",
            "-i", str(fastq),
            "-o", str(output),
            "-q", str(int(ctx.param("fastp.qualified_quality_phred", 15))),
            "-l", str(int(ctx.param("fastp.length_required", 500))),
            "-h", str(html),
            "-j", str(report_json),
            "-w", str(min(_threads(ctx, 2), 16)),
        ],
    )
    ctx.require(output)

    if report_json.exists():
        for key, value in parse_fastp_json(report_json).items():
            ctx.metric(f"qc_{key}", value)
    if html.exists():
        ctx.artifact(html, kind="qc", label="fastp report")
    return output


# ------------------------------------------------------------------ parsing

def parse_nanostats(path: Path) -> dict:
    """Extract mean read quality and length from NanoPlot's NanoStats.txt."""
    data: dict[str, float] = {}
    try:
        text = Path(path).read_text(errors="replace")
    except OSError:
        return data

    for line in text.splitlines():
        line = line.strip()
        for label, key in (("Mean read quality", "mean_quality"), ("Mean read length", "mean_length")):
            if line.startswith(label):
                match = re.search(r"([\d,]+\.?\d*)", line.split(":", 1)[-1])
                if match:
                    data[key] = float(match.group(1).replace(",", ""))
    return data


def parse_fastp_json(path: Path) -> dict:
    import json

    try:
        payload = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}

    before = payload.get("summary", {}).get("before_filtering", {})
    after = payload.get("summary", {}).get("after_filtering", {})
    return {
        "q20_rate": round(before.get("q20_rate", 0) * 100, 2),
        "q30_rate": round(before.get("q30_rate", 0) * 100, 2),
        "mean_length": before.get("read1_mean_length", 0),
        "reads_before": before.get("total_reads", 0),
        "reads_after": after.get("total_reads", 0),
    }


def recommended_thresholds(stats: dict) -> tuple[int, int]:
    """Derive NanoFilt thresholds from observed read statistics.

    Domain heuristic carried over from the original implementation unchanged.
    """
    quality_observed = stats.get("mean_quality", 0)
    length_observed = stats.get("mean_length", 0)

    if quality_observed < 8:
        quality = 8
    elif quality_observed < 10:
        quality = 10
    else:
        quality = 12

    if length_observed < 800:
        length = 300
    elif length_observed < 2000:
        length = 500
    else:
        length = 1000

    return quality, length


# ------------------------------------------------------------------ helpers

def _manual(ctx: RunContext) -> tuple[int, int]:
    return (
        int(ctx.param("nanofilt.min_quality", 10)),
        int(ctx.param("nanofilt.min_length", 500)),
    )


def _threads(ctx: RunContext, cap: int) -> int:
    return max(1, min(int(ctx.param("threads", 4)), cap))


def _copy(source: Path, destination: Path) -> Path:
    import shutil

    shutil.copy(source, destination)
    return destination
