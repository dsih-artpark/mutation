"""Human-readable summary of an Illumina LSDV run."""

from __future__ import annotations

from datetime import datetime, timezone

from ...runtime.context import RunContext


def run(ctx: RunContext):
    primer_trimmed = ctx.result.metrics.get("primer_trimmed")
    if primer_trimmed:
        primer_text = "enabled (iVar)"
    elif ctx.param("primer.enabled", False):
        primer_text = "skipped (no BED uploaded)"
    else:
        primer_text = "disabled"

    consensus_stage = next((e for e in ctx.stage_log if e.get("name") == "consensus"), {})
    if consensus_stage.get("status") == "SUCCEEDED":
        consensus_text = "bcftools"
    elif consensus_stage.get("status") == "SKIPPED" or not ctx.param("consensus.enabled", True):
        consensus_text = "disabled"
    else:
        consensus_text = "bcftools" if ctx.param("consensus.enabled", True) else "disabled"

    metrics = ctx.result.metrics
    if "variant_filter_note" in metrics:
        filter_text = metrics["variant_filter_note"]
        calls_text = (f"{metrics.get('variant_count', 0)} called, "
                      f"{metrics.get('variant_count_pass', 0)} PASS")
    else:
        filter_text = calls_text = "n/a"

    if consensus_text == "disabled":
        mask_text = "not applied (consensus disabled)"
    elif "consensus_masked_positions" in metrics:
        mask_text = f"{metrics['consensus_mask_note']} ({metrics['consensus_masked_positions']} positions masked)"
    else:
        mask_text = metrics.get("consensus_mask_note", "n/a")

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
        f"Primer trim    : {primer_text}",
        f"Variant caller : {ctx.param('variant.tool', 'bcftools')}",
        f"Variant filter : {filter_text}",
        f"Variants       : {calls_text}",
        f"Consensus      : {consensus_text}",
        f"Low-depth mask : {mask_text}",
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
