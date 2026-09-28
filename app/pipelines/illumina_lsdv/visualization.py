"""Interactive overview of annotated LSDV variants with gene and impact tracks."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

import plotly.graph_objects as go

from ...runtime.context import RunContext


def _wrap_text(text: str, width: int = 80) -> str:
    if not text:
        return ""
    return "<br>".join([text[i : i + width] for i in range(0, len(text), width)])


def _parse_attributes(attr_str: str) -> dict[str, str]:
    attrs = {}
    for item in attr_str.split(";"):
        if "=" in item:
            k, v = item.split("=", 1)
            attrs[k] = unquote(v)
    return attrs


def _is_noncoding_effect(effect_name: str) -> bool:
    e = str(effect_name).lower()
    noncoding_keywords = [
        "upstream",
        "downstream",
        "intergenic",
        "utr",
        "intron",
        "non_coding",
        "noncoding",
        "regulatory_region",
        "splice_region",
    ]
    return any(k in e for k in noncoding_keywords)


def _pretty_effect_name(effect_name: str) -> str:
    if not effect_name:
        return "Unknown"
    name = str(effect_name).replace("_variant", "").replace("_", " ")
    return name[:1].upper() + name[1:]


def _marker_style(impact: str) -> dict:
    if impact == "HIGH":
        return dict(
            symbol="triangle-up",
            size=22,
            color="#ff0000",
            line=dict(width=0.1, color="red"),
        )
    elif impact == "MODERATE":
        return dict(
            symbol="line-ns",
            size=18,
            color="#8000ff",
            line=dict(width=2.5, color="#8000ff"),
        )
    elif impact == "LOW":
        return dict(
            symbol="circle",
            size=11,
            color="#ffd700",
            line=dict(width=0.8, color="black"),
        )
    elif impact == "MODIFIER":
        return dict(
            symbol="circle",
            size=9,
            color="#00cc44",
            line=dict(width=0.8, color="black"),
        )
    else:
        return dict(
            symbol="circle",
            size=9,
            color="#808080",
            line=dict(width=0.8, color="black"),
        )


def _hover_label_style(impact: str) -> dict:
    marker = _marker_style(impact)
    return dict(
        bgcolor="white",
        bordercolor=marker["color"],
        font=dict(color="black"),
    )


def run(ctx: RunContext, annotated_vcf: Path, rows: list[dict]) -> Path:
    output = ctx.work_dir / "visualization.html"

    if not rows:
        output.write_text(
            "<!doctype html><html><body><h2>No variants identified</h2>"
            "<p>The filtered VCF contained no variant records.</p></body></html>\n"
        )
        return output

    positions = [row.get("pos") for row in rows if row.get("pos") is not None]
    xmin = min(positions) if positions else 0
    xmax = max(positions) if positions else 150000
    default_range = [max(0, xmin - 500), xmax + 500]

    # Load GFF from refs directory if available
    gff_file = Path(ctx.refs_dir) / "lsdv_reference_sequence.gff3"
    genes_by_id = {}

    if gff_file.is_file():
        for line in gff_file.read_text(errors="replace").splitlines():
            if line.startswith("#"):
                continue
            fields = line.strip().split("\t")
            if len(fields) < 9:
                continue
            seqid, source, feature_type, start, end, score, strand, phase, attributes = fields
            attrs = _parse_attributes(attributes)

            if feature_type in ["gene", "mRNA"]:
                gene_id = attrs.get("ID", "")
                gene_name = attrs.get("Name", attrs.get("gene", attrs.get("ID", "")))
                if not gene_id or not gene_name:
                    continue
                genes_by_id[gene_id] = {
                    "id": gene_id,
                    "name": gene_name,
                    "start": int(start),
                    "end": int(end),
                    "cds_start": None,
                    "cds_end": None,
                    "note": "",
                    "product": "",
                }
            elif feature_type == "CDS":
                parent_id = attrs.get("Parent", "")
                if parent_id in genes_by_id:
                    genes_by_id[parent_id]["cds_start"] = int(start)
                    genes_by_id[parent_id]["cds_end"] = int(end)
                    genes_by_id[parent_id]["note"] = attrs.get("Note", "")
                    genes_by_id[parent_id]["product"] = attrs.get("product", "")

    def gene_sort_key(name):
        m = re.search(r"(\d+)", str(name))
        return int(m.group(1)) if m else float("inf")

    genes = sorted(genes_by_id.values(), key=lambda g: gene_sort_key(g["name"]))

    # Impact and Effect counts
    all_impacts = [str(r.get("impact") or "MODIFIER") for r in rows]
    all_effects = [str(r.get("effect") or "variant") for r in rows]

    preferred_impact_order = ["HIGH", "MODERATE", "LOW", "MODIFIER"]
    observed_impacts = list(dict.fromkeys(all_impacts))
    impact_order = [x for x in preferred_impact_order if x in observed_impacts]
    impact_order += [x for x in observed_impacts if x not in impact_order]

    impact_counts = {imp: all_impacts.count(imp) for imp in impact_order}
    legend_name_map = {
        imp: f"{imp} ({impact_counts.get(imp, 0)})"
        for imp in impact_order
    }

    observed_effects = list(dict.fromkeys(all_effects))
    effect_counts = {eff: all_effects.count(eff) for eff in observed_effects}
    coding_effects = sorted([e for e in observed_effects if not _is_noncoding_effect(e)])
    noncoding_effects = sorted([e for e in observed_effects if _is_noncoding_effect(e)])
    effect_order = coding_effects + noncoding_effects

    hovertemplate = (
        "<b>Position:</b> %{x:.0f}<br>"
        "<b>Mutation:</b> %{customdata[5]}<br>"
        "<b>Gene:</b> %{customdata[0]}<br>"
        "<b>Effect:</b> %{customdata[1]}<br>"
        "<b>Impact:</b> %{customdata[2]}<br>"
        "<b>HGVS.c:</b> %{customdata[3]}<br>"
        "<b>HGVS.p:</b> %{customdata[4]}<br>"
        "<b>DP:</b> %{customdata[6]}<br>"
        "<b>AF:</b> %{customdata[7]}<extra></extra>"
    )

    fig = go.Figure()

    baseline_shape = dict(
        type="line",
        x0=0,
        x1=xmax + 500,
        y0=1,
        y1=1,
        line=dict(color="#9aa5b1", width=2),
    )
    fig.add_shape(**baseline_shape)

    trace_meta = []
    legend_shown = set()

    for impact in impact_order:
        for effect in effect_order:
            sub = [
                r for r in rows
                if str(r.get("impact") or "MODIFIER") == impact
                and str(r.get("effect") or "variant") == effect
            ]
            if not sub:
                continue

            showlegend = impact not in legend_shown
            if showlegend:
                legend_shown.add(impact)

            customdata = [
                [
                    r.get("gene") or "",
                    r.get("effect") or "",
                    r.get("impact") or "",
                    r.get("hgvs_c") or "",
                    r.get("hgvs_p") or "",
                    f"{r.get('ref', '')} → {r.get('alt', '')}",
                    r.get("depth") if r.get("depth") is not None else "",
                    r.get("allele_freq") if r.get("allele_freq") is not None else "",
                ]
                for r in sub
            ]

            fig.add_trace(
                go.Scatter(
                    x=[r.get("pos") for r in sub],
                    y=[1] * len(sub),
                    mode="markers",
                    name=legend_name_map[impact],
                    legendgroup=impact,
                    showlegend=showlegend,
                    marker=_marker_style(impact),
                    customdata=customdata,
                    hovertemplate=hovertemplate,
                    hoverlabel=_hover_label_style(impact),
                )
            )

            trace_meta.append({
                "impact": impact,
                "effect": effect,
                "region": "noncoding" if _is_noncoding_effect(effect) else "coding",
            })

    n_traces = len(trace_meta)

    def vis_all():
        return [True] * n_traces

    def vis_effect(effect_name):
        return [m["effect"] == effect_name for m in trace_meta]

    def vis_region(region_name):
        return [m["region"] == region_name for m in trace_meta]

    effect_buttons = [
        dict(label="Clear all", method="update", args=[{"visible": vis_all()}]),
        dict(label="All Effects", method="update", args=[{"visible": vis_all()}]),
    ]

    if coding_effects:
        effect_buttons.append(
            dict(label="--- Coding ---", method="update", args=[{"visible": vis_all()}])
        )
        effect_buttons.append(
            dict(
                label=f"All Coding ({sum(effect_counts.get(e, 0) for e in coding_effects)})",
                method="update",
                args=[{"visible": vis_region("coding")}],
            )
        )
        for e in coding_effects:
            effect_buttons.append(
                dict(
                    label=f"{_pretty_effect_name(e)} ({effect_counts.get(e, 0)})",
                    method="update",
                    args=[{"visible": vis_effect(e)}],
                )
            )

    if noncoding_effects:
        effect_buttons.append(
            dict(label="--- Non-coding ---", method="update", args=[{"visible": vis_all()}])
        )
        effect_buttons.append(
            dict(
                label=f"All Non-coding ({sum(effect_counts.get(e, 0) for e in noncoding_effects)})",
                method="update",
                args=[{"visible": vis_region("noncoding")}],
            )
        )
        for e in noncoding_effects:
            effect_buttons.append(
                dict(
                    label=f"{_pretty_effect_name(e)} ({effect_counts.get(e, 0)})",
                    method="update",
                    args=[{"visible": vis_effect(e)}],
                )
            )

    def build_gene_description(g):
        lines = []
        if g.get("note"):
            lines.append(f"<b>Note:</b> {_wrap_text(g['note'], width=80)}")
        if g.get("product"):
            lines.append(f"<b>Product:</b> {_wrap_text(g['product'], width=80)}")
        return "<br>".join(lines)

    def gene_shapes_for_window(selected_gene):
        zoom_start = max(0, selected_gene["start"] - 100)
        zoom_end = selected_gene["end"] + 100
        shapes = [baseline_shape]

        for g in genes:
            if g["start"] <= zoom_end and g["end"] >= zoom_start:
                is_selected = g["name"] == selected_gene["name"]
                shapes.append(
                    dict(
                        type="line",
                        x0=g["start"],
                        x1=g["end"],
                        y0=0.93,
                        y1=0.93,
                        line=dict(
                            color="#cc6f8d" if is_selected else "#9e9e9e",
                            width=6 if is_selected else 2.5,
                        ),
                    )
                )
                if is_selected and g.get("cds_start") is not None and g.get("cds_end") is not None:
                    shapes.append(
                        dict(
                            type="line",
                            x0=g["cds_start"],
                            x1=g["cds_end"],
                            y0=0.85,
                            y1=0.85,
                            line=dict(color="#1f77b4", width=4),
                        )
                    )
        return shapes, zoom_start, zoom_end

    gene_buttons = [
        dict(
            label="Clear all",
            method="relayout",
            args=[{
                "xaxis.range": default_range,
                "shapes": [baseline_shape],
                "annotations[0].text": "",
                "annotations[1].text": "",
                "annotations[1].x": 0,
                "annotations[2].text": "",
                "annotations[2].x": 0,
            }],
        ),
        dict(
            label="All Genes",
            method="relayout",
            args=[{
                "xaxis.range": default_range,
                "shapes": [baseline_shape],
                "annotations[0].text": "",
                "annotations[1].text": "",
                "annotations[1].x": 0,
                "annotations[2].text": "",
                "annotations[2].x": 0,
            }],
        ),
    ]

    for g in genes:
        shapes, zoom_start, zoom_end = gene_shapes_for_window(g)
        gene_mid = (g["start"] + g["end"]) / 2
        desc = build_gene_description(g)

        gene_buttons.append(
            dict(
                label=g["name"],
                method="relayout",
                args=[{
                    "xaxis.range": [zoom_start, zoom_end],
                    "shapes": shapes,
                    "annotations[0].text": desc,
                    "annotations[1].text": g["name"],
                    "annotations[1].x": gene_mid,
                    "annotations[2].text": "CDS",
                    "annotations[2].x": gene_mid,
                }],
            )
        )

    fig.add_annotation(
        x=0.5,
        y=0.63,
        xref="paper",
        yref="paper",
        text="",
        showarrow=False,
        font=dict(size=14, color="#1f4eaa"),
        align="center",
        xanchor="center",
    )

    fig.add_annotation(
        x=0,
        y=0.905,
        xref="x",
        yref="y",
        text="",
        showarrow=False,
        font=dict(size=13, color="#9a4662"),
        align="center",
    )

    fig.add_annotation(
        x=0,
        y=0.82,
        xref="x",
        yref="y",
        text="",
        showarrow=False,
        font=dict(size=11, color="#1f77b4"),
        align="center",
    )

    fig.update_xaxes(
        range=default_range,
        title="Genome position",
        rangeslider=dict(visible=True, thickness=0.18),
    )

    fig.update_yaxes(
        visible=False,
        range=[0.8, 1.35],
    )

    fig.update_layout(
        title="LSDV Mutations Overview",
        template="plotly_white",
        width=1500,
        height=650,
        plot_bgcolor="white",
        paper_bgcolor="white",
        margin=dict(t=190, b=50),
        legend=dict(
            title="Impact",
            orientation="h",
            yanchor="bottom",
            y=1.02,
            xanchor="left",
            x=0,
            traceorder="normal",
        ),
        updatemenus=[
            dict(
                buttons=effect_buttons,
                direction="down",
                showactive=True,
                active=1,
                x=0.78,
                xanchor="right",
                y=1.17,
                yanchor="top",
            ),
            dict(
                buttons=gene_buttons,
                direction="down",
                showactive=True,
                active=1,
                x=0.98,
                xanchor="right",
                y=1.17,
                yanchor="top",
            ),
        ],
        annotations=fig.layout.annotations,
    )

    fig.write_html(output, include_plotlyjs=True, full_html=True)
    return output
