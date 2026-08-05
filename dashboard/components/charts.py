"""Gráficos orientados à investigação, com estados vazios explícitos."""
from __future__ import annotations

from typing import Any

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from dashboard.components.empty_states import empty_state
from dashboard.services.metrics import match_flow
from dashboard.utils.status import STATUS_META, status_label


PLOT_COLORS = {
    "blue": "#2563EB",
    "blue_soft": "#A8C7FA",
    "green": "#16794A",
    "amber": "#D28B00",
    "red": "#C23A30",
    "purple": "#6E3BB7",
    "slate": "#98A2B3",
    "grid": "#253247",
    "ink": "#C5D1E0",
}


def _rgba(hex_color: str, alpha: float) -> str:
    """Converte token hexadecimal para a sintaxe RGBA aceita pelo Plotly."""
    color = hex_color.lstrip("#")
    red, green, blue = (int(color[index:index + 2], 16) for index in (0, 2, 4))
    return f"rgba({red},{green},{blue},{alpha})"


def _layout(fig: go.Figure, height: int = 310) -> go.Figure:
    fig.update_layout(
        height=height,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font={"family": "Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif", "color": PLOT_COLORS["ink"], "size": 12},
        margin={"l": 8, "r": 8, "t": 28, "b": 8},
        legend={"orientation": "h", "y": 1.12, "x": 0, "font": {"size": 11}},
        hoverlabel={"bgcolor": "#111827", "bordercolor": "#34445D", "font": {"color": "#E7EEF8"}},
    )
    fig.update_xaxes(showgrid=False, zeroline=False, linecolor="#D0D5DD")
    fig.update_yaxes(gridcolor=PLOT_COLORS["grid"], zeroline=False)
    return fig


def render_match_sankey(records: pd.DataFrame) -> None:
    flow = match_flow(records)
    if flow.empty:
        empty_state("Sem fluxo para mostrar", "Aplique filtros menos restritivos para investigar as correspondências.")
        return

    methods = flow["metodo"].drop_duplicates().tolist()
    destinations = flow["destino"].drop_duplicates().tolist()
    labels = ["Notificações"] + methods + [status_label(item) for item in destinations]
    method_index = {value: position + 1 for position, value in enumerate(methods)}
    destination_index = {value: 1 + len(methods) + position for position, value in enumerate(destinations)}
    total = int(flow["quantidade"].sum())
    source: list[int] = []
    target: list[int] = []
    value: list[int] = []
    colors: list[str] = []
    for method, quantity in flow.groupby("metodo")["quantidade"].sum().items():
        source.append(0)
        target.append(method_index[method])
        value.append(int(quantity))
        colors.append("rgba(37,99,235,.32)")
    for row in flow.itertuples(index=False):
        source.append(method_index[row.metodo])
        target.append(destination_index[row.destino])
        value.append(int(row.quantidade))
        colors.append(_rgba(STATUS_META.get(row.destino, STATUS_META["EM_ANDAMENTO"]).color, 0.4))
    node_colors = [PLOT_COLORS["blue"]] + ["#98A2B3"] * len(methods) + [
        STATUS_META.get(item, STATUS_META["EM_ANDAMENTO"]).color for item in destinations
    ]
    fig = go.Figure(
        go.Sankey(
            arrangement="snap",
            node={"pad": 18, "thickness": 14, "line": {"color": "rgba(0,0,0,0)", "width": 0}, "label": labels, "color": node_colors},
            link={
                "source": source,
                "target": target,
                "value": value,
                "color": colors,
                "hovertemplate": "%{source.label} → %{target.label}<br><b>%{value:,.0f}</b> registros<extra></extra>",
            },
        )
    )
    _layout(fig, 350)
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
    st.caption(f"Fluxo calculado sobre {total:,.0f} notificações filtradas. Métodos e situação final são campos do cruzamento.")


def render_coverage_waterfall(data: pd.DataFrame) -> None:
    if data.empty:
        empty_state("Relatório de cobertura indisponível", "Rode o ETL para gerar geosampa_coverage_report.json.")
        return
    measures = ["absolute"] + ["relative"] * max(len(data) - 2, 0) + ["total"]
    colors = [PLOT_COLORS["blue"]] + [PLOT_COLORS["red"]] * max(len(data) - 2, 0) + [PLOT_COLORS["green"]]
    fig = go.Figure(
        go.Waterfall(
            orientation="v",
            measure=measures,
            x=data["etapa"],
            y=[data.iloc[0]["quantidade"]] + [-value for value in data.iloc[1:-1]["quantidade"]] + [data.iloc[-1]["quantidade"]],
            text=[f"{value:,.0f}" for value in data["quantidade"]],
            textposition="outside",
            connector={"line": {"color": "#D0D5DD"}},
            increasing={"marker": {"color": PLOT_COLORS["blue"]}},
            decreasing={"marker": {"color": PLOT_COLORS["red"]}},
            totals={"marker": {"color": PLOT_COLORS["green"]}},
        )
    )
    _layout(fig, 330)
    fig.update_xaxes(tickangle=-24)
    fig.update_yaxes(title="Recapes")
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
    st.caption("Perdas mutuamente exclusivas classificadas pelo diagnóstico topológico do GeoSampa.")


def render_temporal_coverage(data: pd.DataFrame) -> None:
    if data.empty:
        empty_state("Sem histórico temporal", "Não há datas de recebimento válidas no recorte atual.")
        return
    fig = go.Figure()
    fig.add_bar(name="Volume total", x=data["mes"], y=data["total"], marker_color="#D0D5DD", hovertemplate="%{x}<br>Volume: %{y:,.0f}<extra></extra>")
    fig.add_scatter(name="Cobertura confirmada", x=data["mes"], y=data["confirmada"], mode="lines+markers", line={"color": PLOT_COLORS["green"], "width": 2.5}, hovertemplate="%{x}<br>Confirmada: %{y:,.0f}<extra></extra>")
    fig.add_scatter(name="Sem cobertura", x=data["mes"], y=data["sem_cobertura"], mode="lines+markers", line={"color": PLOT_COLORS["red"], "width": 2}, hovertemplate="%{x}<br>Sem cobertura: %{y:,.0f}<extra></extra>")
    fig.add_scatter(name="Baixa confiança", x=data["mes"], y=data["baixa_confianca"], mode="lines", line={"color": PLOT_COLORS["purple"], "width": 1.5, "dash": "dot"}, hovertemplate="%{x}<br>Baixa confiança: %{y:,.0f}<extra></extra>")
    _layout(fig, 335)
    fig.update_layout(barmode="overlay")
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
    st.caption("As linhas mostram quantidade; a tabela de apoio apresenta percentuais por período.")


def render_regional_ranking(data: pd.DataFrame) -> None:
    if data.empty:
        empty_state("Sem recorte regional", "A base não informou prefeitura regional para os registros filtrados.")
        return
    view = data.head(12).sort_values("criticidade")
    fig = go.Figure()
    fig.add_bar(
        y=view["regional"], x=view["sem_cobertura_pct"], orientation="h", name="Sem cobertura",
        marker_color=PLOT_COLORS["red"], hovertemplate="%{y}<br>Sem cobertura: %{x:.1f}%<extra></extra>",
    )
    fig.add_bar(
        y=view["regional"], x=view["baixa_confianca_pct"], orientation="h", name="Baixa confiança",
        marker_color=PLOT_COLORS["purple"], hovertemplate="%{y}<br>Baixa confiança: %{x:.1f}%<extra></extra>",
    )
    _layout(fig, 360)
    fig.update_layout(barmode="stack")
    fig.update_xaxes(title="Percentual de casos no recorte")
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})


def render_confidence_histogram(data: pd.DataFrame) -> None:
    if data.empty:
        empty_state("Sem matches para classificar", "Não há correspondências no recorte atual.")
        return
    palette = {"Crítica": PLOT_COLORS["red"], "Baixa": PLOT_COLORS["amber"], "Aceitável": PLOT_COLORS["blue"], "Alta": PLOT_COLORS["green"]}
    fig = px.bar(data, x="faixa", y="quantidade", color="faixa", color_discrete_map=palette, category_orders={"faixa": ["Crítica", "Baixa", "Aceitável", "Alta"]})
    _layout(fig, 300)
    fig.update_layout(showlegend=False)
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})


def render_match_scatter(records: pd.DataFrame) -> None:
    required = {"dist_recape_km", "score_confianca", "metodo_match"}
    if not required.issubset(records.columns):
        empty_state("Dados insuficientes para o scatter", "São necessários distância, score e método de match.")
        return
    plot = records[records["match_encontrado"]].copy()
    plot["distancia_m"] = pd.to_numeric(plot["dist_recape_km"], errors="coerce") * 1000
    plot = plot.dropna(subset=["distancia_m", "score_confianca"])
    if plot.empty:
        empty_state("Sem distância calculada", "A estratégia de match atual não registrou distâncias para este recorte.")
        return
    size = None
    if "extensao_m" in plot.columns:
        extension = pd.to_numeric(plot["extensao_m"], errors="coerce")
        if extension.notna().any():
            fallback_size = max(float(extension.median()), 1.0)
            plot["tamanho_recape"] = extension.fillna(fallback_size).clip(lower=1)
            size = "tamanho_recape"
    fig = px.scatter(
        plot, x="distancia_m", y="score_confianca", color="metodo_match", size=size,
        hover_data=[column for column in ["numero_os", "rua_notif", "rua_recape"] if column in plot.columns],
        color_discrete_sequence=[PLOT_COLORS["blue"], PLOT_COLORS["green"], PLOT_COLORS["purple"], PLOT_COLORS["amber"]],
    )
    _layout(fig, 360)
    fig.add_hline(y=85, line_dash="dot", line_color=PLOT_COLORS["amber"], annotation_text="limite de aceitação")
    fig.add_vline(x=300, line_dash="dot", line_color="#98A2B3", annotation_text="300 m")
    fig.update_xaxes(title="Distância do recape (m)")
    fig.update_yaxes(title="Score do nome")
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
