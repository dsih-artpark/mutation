"""Paired-end read QC and preprocessing."""

from __future__ import annotations

from pathlib import Path

from ...runtime.context import RunContext
from . import specs
from .common import threads


def run(ctx: RunContext, r1: Path, r2: Path) -> tuple[Path, Path]:
    out = ctx.work_dir / "qc"
    out.mkdir(parents=True, exist_ok=True)

    with ctx.stage("qc"):
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
