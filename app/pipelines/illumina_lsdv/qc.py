"""Paired-end read QC and preprocessing."""

from __future__ import annotations

from pathlib import Path

from ...runtime.context import RunContext
from . import specs
from .common import threads


def run(ctx: RunContext, r1: Path, r2: Path) -> tuple[Path, Path]:
    tool = str(ctx.param("qc.tool", "fastp")).lower()
    out = ctx.work_dir / "qc"
    out.mkdir(parents=True, exist_ok=True)

    with ctx.stage("qc"):
        if tool == "fastp":
            clean1, clean2 = out / "clean_R1.fastq.gz", out / "clean_R2.fastq.gz"
            html, report_json = out / "fastp.html", out / "fastp.json"
            argv = [
                "fastp", "--in1", str(r1), "--in2", str(r2),
                "--out1", str(clean1), "--out2", str(clean2),
                "--html", str(html), "--json", str(report_json),
                "--qualified_quality_phred", str(ctx.param("fastp.qualified_quality_phred", 20)),
                "--length_required", str(ctx.param("fastp.length_required", 50)),
                "--thread", str(threads(ctx, specs.QC.cpus)),
            ]
            if ctx.param("fastp.detect_adapter_for_pe", True):
                argv.append("--detect_adapter_for_pe")
            ctx.run(specs.QC, argv)
            ctx.require(clean1)
            ctx.require(clean2)
            _fastp_metrics(ctx, report_json)
            _publish_reports(ctx, [html, report_json])
            return clean1, clean2

        if tool == "fastqc":
            _run_fastqc(ctx, r1, r2, out / "raw_fastqc")
            _publish_fastqc(ctx, out / "raw_fastqc")
            return r1, r2

        if tool == "trimmomatic":
            clean1, clean2 = _run_trimmomatic(ctx, r1, r2, out)
            return clean1, clean2

        if tool == "fastqc_trimmomatic":
            raw = out / "raw_fastqc"
            _run_fastqc(ctx, r1, r2, raw)
            _publish_fastqc(ctx, raw, "Raw FastQC")
            clean1, clean2 = _run_trimmomatic(ctx, r1, r2, out)
            trimmed = out / "trimmed_fastqc"
            _run_fastqc(ctx, clean1, clean2, trimmed)
            _publish_fastqc(ctx, trimmed, "Trimmed FastQC")
            return clean1, clean2

        raise ValueError(f"unsupported QC tool: {tool}")


def _run_fastqc(ctx: RunContext, r1: Path, r2: Path, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    ctx.run(specs.QC, ["fastqc", "--threads", str(threads(ctx, specs.QC.cpus)),
                       "--outdir", str(out), str(r1), str(r2)])


def _run_trimmomatic(ctx: RunContext, r1: Path, r2: Path, out: Path) -> tuple[Path, Path]:
    p1, u1 = out / "trimmed_R1_paired.fastq.gz", out / "trimmed_R1_unpaired.fastq.gz"
    p2, u2 = out / "trimmed_R2_paired.fastq.gz", out / "trimmed_R2_unpaired.fastq.gz"
    adapter = "/opt/trimmomatic/TruSeq3-PE.fa"
    sliding = f"{ctx.param('trimmomatic.sliding_window_size', 4)}:{ctx.param('trimmomatic.sliding_window_quality', 20)}"
    ctx.run(specs.QC, [
        "trimmomatic", "PE", "-threads", str(threads(ctx, specs.QC.cpus)),
        str(r1), str(r2), str(p1), str(u1), str(p2), str(u2),
        f"ILLUMINACLIP:{adapter}:2:30:10", f"SLIDINGWINDOW:{sliding}",
        f"MINLEN:{ctx.param('trimmomatic.min_length', 50)}",
    ])
    ctx.require(p1)
    ctx.require(p2)
    _publish_reports(ctx, [u1, u2], "Trimmomatic unpaired reads")
    return p1, p2


def _publish_fastqc(ctx: RunContext, directory: Path, prefix: str = "FastQC") -> None:
    for html in sorted(directory.glob("*_fastqc.html")):
        ctx.artifact(html, kind="qc", label=f"{prefix}: {html.stem}")


def _publish_reports(ctx: RunContext, paths: list[Path], label: str = "QC report") -> None:
    for path in paths:
        if path.is_file() and path.stat().st_size:
            ctx.artifact(path, kind="qc", label=f"{label}: {path.name}")


def _fastp_metrics(ctx: RunContext, path: Path) -> None:
    import json

    try:
        data = json.loads(path.read_text(errors="replace"))
        before = data.get("summary", {}).get("before_filtering", {})
        after = data.get("summary", {}).get("after_filtering", {})
        if before.get("total_reads") is not None:
            ctx.metric("reads_before_qc", int(before["total_reads"]))
        if after.get("total_reads") is not None:
            ctx.metric("reads_after_qc", int(after["total_reads"]))
        if after.get("q30_rate") is not None:
            ctx.metric("qc_q30_percent", round(float(after["q30_rate"]) * 100, 2))
    except Exception as exc:
        ctx.log(f"[qc] could not parse fastp JSON metrics: {exc}")
