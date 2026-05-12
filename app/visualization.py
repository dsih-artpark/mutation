import pandas as pd
import plotly.graph_objects as go
from urllib.parse import unquote
import re
from pathlib import Path
from .config import RUNS_DIR, DATA_DIR

def generate_visualization(run_id: str):
    """
    Generates an interactive Plotly visualization for the given run_id.
    Fetches the annotated.vcf from the run directory and the GFF3 file from the data folder.
    Returns the path to the generated HTML file.
    """
    run_dir = RUNS_DIR / run_id
    vcf_file = run_dir / "annotated.vcf"
    gff_file = DATA_DIR / "lsdv_reference_sequence.gff3"
    output_html = run_dir / "visualization.html"

    if not vcf_file.exists():
        raise FileNotFoundError(f"Annotated VCF not found for run {run_id} at {vcf_file}")

    rows = []

    with open(vcf_file) as f:
        for line in f:
            if line.startswith("#"):
                continue

            fields = line.strip().split("\t")

            chrom = fields[0]
            pos = fields[1]
            ref = fields[3]
            alt = fields[4]
            info = fields[7]

            ann_field = [x for x in info.split(";") if x.startswith("ANN=")]
            if not ann_field:
                continue

            ann_entries = ann_field[0].replace("ANN=", "").split(",")
            first = ann_entries[0].split("|")

            effect = first[1] if len(first) > 1 else ""
            impact = first[2] if len(first) > 2 else ""
            gene = first[3] if len(first) > 3 else ""
            hgvs_c = first[9] if len(first) > 9 else ""
            hgvs_p = first[10] if len(first) > 10 else ""

            dp = ""
            af = ""

            for item in info.split(";"):
                if item.startswith("DP="):
                    dp = item.split("=", 1)[1]
                elif item.startswith("AF="):
                    af = item.split("=", 1)[1]

            rows.append({
                "pos": int(pos),
                "ref": ref,
                "alt": alt,
                "impact": impact,
                "effect": effect,
                "gene": gene,
                "hgvs_c": hgvs_c,
                "hgvs_p": hgvs_p,
                "dp": dp,
                "af": af
            })

    if not rows:
        # Create an empty visualization or handle error
        return None

    df = pd.DataFrame(rows)
    df = df.sort_values("pos").reset_index(drop=True)
    df["mutation"] = df["ref"] + " → " + df["alt"]

    xmax = df["pos"].max()
    xmin = df["pos"].min()
    default_range = [0, xmin + 500]

    # -----------------------------
    # Parse GFF genes
    # -----------------------------
    def parse_attributes(attr_str):
        attrs = {}
        for item in attr_str.split(";"):
            if "=" in item:
                k, v = item.split("=", 1)
                attrs[k] = unquote(v)
        return attrs

    genes = []

    with open(gff_file) as f:
        for line in f:
            if line.startswith("#"):
                continue

            fields = line.strip().split("\t")
            if len(fields) < 9:
                continue

            seqid, source, feature_type, start, end, score, strand, phase, attributes = fields

            if feature_type != "gene":
                continue

            attrs = parse_attributes(attributes)
            gene_name = attrs.get("Name", "")
            if not gene_name:
                continue

            gene_note = attrs.get("Note", "")
            genes.append({
                "name": gene_name,
                "start": int(start),
                "end": int(end),
                "note": gene_note
            })

    def gene_sort_key(name):
        m = re.search(r"(\d+)", str(name))
        return int(m.group(1)) if m else float("inf")

    genes = sorted(genes, key=lambda g: gene_sort_key(g["name"]))

    # -----------------------------
    # Dynamic impact handling
    # -----------------------------
    preferred_impact_order = ["HIGH", "MODERATE", "LOW", "MODIFIER"]
    observed_impacts = df["impact"].dropna().unique().tolist()

    impact_order = [x for x in preferred_impact_order if x in observed_impacts]
    impact_order += [x for x in observed_impacts if x not in impact_order]

    impact_counts = df["impact"].value_counts().to_dict()

    legend_name_map = {
        impact: f"{impact} ({impact_counts.get(impact, 0)})"
        for impact in impact_order
    }

    # -----------------------------
    # Dynamic effect handling
    # -----------------------------
    effect_counts = df["effect"].value_counts().to_dict()
    observed_effects = df["effect"].dropna().unique().tolist()

    def is_noncoding_effect(effect_name):
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
            "splice_region"
        ]
        return any(k in e for k in noncoding_keywords)

    coding_effects = [e for e in observed_effects if not is_noncoding_effect(e)]
    noncoding_effects = [e for e in observed_effects if is_noncoding_effect(e)]

    coding_effects = sorted(coding_effects)
    noncoding_effects = sorted(noncoding_effects)
    effect_order = coding_effects + noncoding_effects

    def pretty_effect_name(effect_name):
        if not effect_name:
            return "Unknown"
        name = effect_name.replace("_variant", "")
        name = name.replace("_", " ")
        return name[:1].upper() + name[1:]

    # -----------------------------
    # Styling
    # -----------------------------
    def marker_style(impact):
        if impact == "HIGH":
            return dict(
                symbol="triangle-up",
                size=22,
                color="#ff0000",
                line=dict(width=0.1, color="red")
            )
        elif impact == "MODERATE":
            return dict(
                symbol="line-ns",
                size=18,
                color="#8000ff",
                line=dict(width=2.5, color="#8000ff")
            )
        elif impact == "LOW":
            return dict(
                symbol="circle",
                size=11,
                color="#ffd700",
                line=dict(width=0.8, color="black")
            )
        elif impact == "MODIFIER":
            return dict(
                symbol="circle",
                size=9,
                color="#00cc44",
                line=dict(width=0.8, color="black")
            )
        else:
            return dict(
                symbol="circle",
                size=9,
                color="#808080",
                line=dict(width=0.8, color="black")
            )

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
        x1=xmax,
        y0=1,
        y1=1,
        line=dict(color="#9aa5b1", width=2)
    )
    fig.add_shape(**baseline_shape)

    trace_meta = []
    legend_shown = set()

    for impact in impact_order:
        for effect in effect_order:
            sub = df[(df["impact"] == impact) & (df["effect"] == effect)].copy()
            if sub.empty:
                continue

            showlegend = impact not in legend_shown
            if showlegend:
                legend_shown.add(impact)

            fig.add_trace(
                go.Scatter(
                    x=sub["pos"],
                    y=[1] * len(sub),
                    mode="markers",
                    name=legend_name_map[impact],
                    legendgroup=impact,
                    showlegend=showlegend,
                    marker=marker_style(impact),
                    customdata=sub[["gene", "effect", "impact", "hgvs_c", "hgvs_p", "mutation", "dp", "af"]],
                    hovertemplate=hovertemplate
                )
            )

            trace_meta.append({
                "impact": impact,
                "effect": effect,
                "region": "noncoding" if is_noncoding_effect(effect) else "coding"
            })

    n_traces = len(trace_meta)

    def vis_all():
        return [True] * n_traces

    def vis_effect(effect_name):
        return [m["effect"] == effect_name for m in trace_meta]

    def vis_region(region_name):
        return [m["region"] == region_name for m in trace_meta]

    # -----------------------------
    # Effect dropdown
    # -----------------------------
    effect_buttons = [
        dict(label="Clear all", method="update", args=[{"visible": vis_all()}]),
        dict(label="All Effects", method="update", args=[{"visible": vis_all()}])
    ]

    if coding_effects:
        effect_buttons.append(
            dict(label="--- Coding ---", method="update", args=[{"visible": vis_all()}])
        )
        effect_buttons.append(
            dict(
                label=f"All Coding ({sum(effect_counts.get(e, 0) for e in coding_effects)})",
                method="update",
                args=[{"visible": vis_region("coding")}]
            )
        )
        for e in coding_effects:
            effect_buttons.append(
                dict(
                    label=f"{pretty_effect_name(e)} ({effect_counts.get(e, 0)})",
                    method="update",
                    args=[{"visible": vis_effect(e)}]
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
                args=[{"visible": vis_region("noncoding")}]
            )
        )
        for e in noncoding_effects:
            effect_buttons.append(
                dict(
                    label=f"{pretty_effect_name(e)} ({effect_counts.get(e, 0)})",
                    method="update",
                    args=[{"visible": vis_effect(e)}]
                )
            )

    # -----------------------------
    # Gene dropdown
    # -----------------------------
    def gene_shapes_for_window(selected_gene):
        zoom_start = max(0, selected_gene["start"] - 100)
        zoom_end = selected_gene["end"] + 100

        shapes = [baseline_shape]

        overlapping = []
        for g in genes:
            if g["start"] <= zoom_end and g["end"] >= zoom_start:
                overlapping.append(g)

        for g in overlapping:
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
                        width=6 if is_selected else 2.5
                    )
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
                "annotations[1].x": 0
            }]
        ),
        dict(
            label="All Genes",
            method="relayout",
            args=[{
                "xaxis.range": default_range,
                "shapes": [baseline_shape],
                "annotations[0].text": "",
                "annotations[1].text": "",
                "annotations[1].x": 0
            }]
        )
    ]

    for g in genes:
        shapes, zoom_start, zoom_end = gene_shapes_for_window(g)
        gene_mid = (g["start"] + g["end"]) / 2

        gene_buttons.append(
            dict(
                label=g["name"],
                method="relayout",
                args=[{
                    "xaxis.range": [zoom_start, zoom_end],
                    "shapes": shapes,
                    "annotations[0].text": g["note"] if g["note"] else "",
                    "annotations[1].text": g["name"],
                    "annotations[1].x": gene_mid
                }]
            )
        )

    # 0 = Note annotation
    fig.add_annotation(
        x=0.5,
        y=1.20,
        xref="paper",
        yref="paper",
        text="",
        showarrow=False,
        font=dict(size=20, color="#1f4eaa"),
        align="center"
    )

    # 1 = Gene name on line
    fig.add_annotation(
        x=0,
        y=0.905,
        xref="x",
        yref="y",
        text="",
        showarrow=False,
        font=dict(size=13, color="#9a4662"),
        align="center"
    )

    fig.update_xaxes(
        range=default_range,
        title="Genome position",
        rangeslider=dict(visible=True, thickness=0.18)
    )

    fig.update_yaxes(
        visible=False,
        range=[0.8, 1.35]
    )

    fig.update_layout(
        title=f"LSDV Mutations Overview - Run {run_id}",
        template="plotly_white",
        width=1500,
        height=650,
        plot_bgcolor="white",
        paper_bgcolor="white",
        margin=dict(t=170, b=50),
        legend=dict(
            title="Impact",
            orientation="h",
            yanchor="bottom",
            y=1.02,
            xanchor="left",
            x=0,
            traceorder="normal"
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
                yanchor="top"
            ),
            dict(
                buttons=gene_buttons,
                direction="down",
                showactive=True,
                active=1,
                x=0.98,
                xanchor="right",
                y=1.17,
                yanchor="top"
            )
        ],
        annotations=fig.layout.annotations
    )

    fig.write_html(str(output_html))
    return output_html

