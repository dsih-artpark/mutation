"""Human-readable run report.

The original wrote "COMPLETED" for every stage and "PIPELINE FINISHED
SUCCESSFULLY" unconditionally, regardless of what happened -- a template rather
than a report. This one is generated from what actually ran: recorded stage
outcomes, published artifacts and measured metrics.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from ...runtime.context import RunContext

_RULE = "=" * 60
_THIN = "-" * 60


def run(ctx: RunContext) -> Path:
    lines: list[str] = [
        _RULE,
        "ONT VARIANT ANALYSIS PIPELINE REPORT",
        _RULE,
        "",
        f"Run ID    : {ctx.run_id}",
        f"Generated : {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "",
        "TOOLS SELECTED",
        _THIN,
        f"QC              : {ctx.param('qc.tool', 'nanoplot')}",
        f"Alignment       : minimap2",
        f"Variant caller  : {ctx.param('variant.tool', 'bcftools')}",
        f"Consensus       : {ctx.param('consensus.tool', 'bcftools')}",
        f"Annotation      : snpEff",
        "",
        "STAGE OUTCOMES",
        _THIN,
    ]

    for entry in ctx.stage_log:
        name = entry["name"].ljust(16)
        status = entry["status"].ljust(10)
        detail = ""
        if entry.get("duration_s") is not None:
            detail = f"{entry['duration_s']:>8.1f}s"
            if entry.get("peak_memory_mb"):
                detail += f"   peak {entry['peak_memory_mb']:.0f} MB"
        elif entry.get("detail"):
            detail = entry["detail"]
        lines.append(f"{name}{status}{detail}")

    if ctx.result.metrics:
        lines += ["", "METRICS", _THIN]
        for key, value in sorted(ctx.result.metrics.items()):
            if isinstance(value, dict):
                value = ", ".join(f"{k}={v}" for k, v in value.items()) or "none"
            lines.append(f"{key.ljust(32)}{value}")

    lines += ["", "OUTPUT FILES", _THIN]
    if ctx.result.artifacts:
        for artifact in ctx.result.artifacts:
            size_mb = artifact.size_bytes / 10**6
            lines.append(f"{artifact.path.name.ljust(36)}{size_mb:>8.2f} MB   {artifact.label}")
    else:
        lines.append("(none published)")

    failed = [e for e in ctx.stage_log if e["status"] == "FAILED"]
    skipped = [e for e in ctx.stage_log if e["status"] == "SKIPPED"]

    lines += ["", _RULE]
    if failed:
        lines.append(f"PIPELINE FAILED AT: {failed[0]['name']}")
    else:
        lines.append("PIPELINE COMPLETED")
        if skipped:
            lines.append(f"Stages skipped: {', '.join(e['name'] for e in skipped)}")
    lines.append(_RULE)

    path = ctx.work_dir / "pipeline_report.txt"
    path.write_text("\n".join(lines) + "\n")
    return ctx.artifact(path, kind="report", label="Pipeline report")
