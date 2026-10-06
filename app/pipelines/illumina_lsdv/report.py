"""Human-readable summary of an Illumina LSDV run."""

from __future__ import annotations

from datetime import datetime, timezone

from ...runtime.context import RunContext


def run(ctx: RunContext):
    lines = [
        "=" * 64,
        "ILLUMINA LSDV VARIANT ANALYSIS REPORT",
        "=" * 64,
        f"Run ID     : {ctx.run_id}",
        f"Generated  : {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "",
        "WORKFLOW SETTINGS",
        "-" * 64,
        "QC             : fastp",
        "Aligner        : bwa",
        f"Primer trim    : {'enabled' if ctx.param('primer.enabled', False) else 'disabled'}",
        f"Variant caller : {ctx.param('variant.tool', 'bcftools')}",
        "Consensus      : bcftools",
        "Annotation     : snpEff / LSDV",
        "",
        "STAGE OUTCOMES",
        "-" * 64,
    ]

    for entry in ctx.stage_log:
        duration = entry.get("duration_s")
        detail = f"{duration:.1f}s" if duration is not None else entry.get("detail", "")
        lines.append(f"{entry['name']:<16}{entry['status']:<12}{detail}")

    if ctx.result.metrics:
        lines += ["", "METRICS", "-" * 64]
        for key, value in sorted(ctx.result.metrics.items()):
            if isinstance(value, dict):
                value = ", ".join(f"{name}={count}" for name, count in value.items()) or "none"
            lines.append(f"{key:<34}{value}")

    lines += ["", "PUBLISHED OUTPUTS", "-" * 64]
    for artifact in ctx.result.artifacts:
        lines.append(f"{artifact.path.name:<36}{artifact.size_bytes / 10**6:>8.2f} MB  {artifact.label}")
    lines += ["", "=" * 64, "PIPELINE COMPLETED", "=" * 64]

    output = ctx.work_dir / "pipeline_report.txt"
    output.write_text("\n".join(lines) + "\n")
    return ctx.artifact(output, kind="report", label="Pipeline report")
