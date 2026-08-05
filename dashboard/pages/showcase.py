"""Composição de portfólio para apresentar o GeoSampa Pipeline."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from dashboard.components.cards import metric_card, page_header
from dashboard.components.charts import (
    render_coverage_waterfall,
    render_regional_ranking,
    render_temporal_coverage,
)
from dashboard.components.empty_states import empty_state
from dashboard.components.map_components import render_operational_map
from dashboard.services.data_loader import AppData
from dashboard.services.metrics import (
    coverage_metrics,
    coverage_waterfall,
    monthly_coverage,
    prepare_audit_records,
    regional_criticality,
)
from dashboard.utils.formatting import numero, percentual
from dashboard.utils.status import status_label


def _last_processes(records: pd.DataFrame) -> pd.DataFrame:
    """Seleciona uma leitura curta da fila sem alterar os dados da auditoria."""
    if records.empty:
        return pd.DataFrame()

    def column(name: str, default: object = "") -> pd.Series:
        if name in records.columns:
            return records[name]
        return pd.Series(default, index=records.index)

    table = pd.DataFrame(
        {
            "Situação": records["situacao_auditoria"].map(status_label),
            "OS": column("numero_os"),
            "Endereço": column("rua_notif"),
            "Regional": column("prefeitura_regional", "Não informado"),
            "Fonte": column("fonte_notif"),
            "Recebimento": pd.to_datetime(column("data_recebimento", pd.NaT), errors="coerce"),
            "Confiança": pd.to_numeric(column("score_confianca", 0), errors="coerce"),
        }
    )
    return table.sort_values("Recebimento", ascending=False, na_position="last").head(8)


def render(data: AppData) -> None:
    """Renderiza uma visão ampla e estável para captura horizontal."""
    records = prepare_audit_records(data.cruzamento)
    page_header(
        "GeoSampa Pipeline",
        "Auditoria geoespacial que conecta notificações, recapeamentos e qualidade do roteamento em uma única leitura operacional.",
        "Portfolio showcase · engenharia de dados + GIS",
    )

    updated = data.updated_at.strftime("%d/%m/%Y · %H:%M") if data.updated_at is not None else "não disponível"
    status = "Pipeline operacional" if not data.errors else "Dados parciais"
    status_left, status_right = st.columns([1, 3], gap="small")
    with status_left:
        st.caption(f"● {status}")
    with status_right:
        st.caption(f"Última atualização: {updated}")

    if records.empty:
        empty_state("Dados de cruzamento indisponíveis", "Rode python src/transform.py para gerar os artefatos processados.")
        return

    metrics = coverage_metrics(records)
    recape_total = len(data.recapes)
    alerts = int(metrics["review"]) + int(metrics["no_coverage"])
    kpis = st.columns(4, gap="medium")
    items = [
        ("Processos", numero(metrics["total"]), "Notificações analisadas no cruzamento", "Casos disponíveis para auditoria geoespacial."),
        ("Cobertura", percentual(metrics["confirmed_pct"]), f"{numero(metrics['confirmed'])} correspondências confirmadas", "Matches fortes que não exigem revisão manual."),
        ("Recapes", numero(recape_total), "Trechos processados pelo GeoSampa", "Base disponível para relacionar intervenções e notificações."),
        ("Alertas", numero(alerts), f"{numero(metrics['review'])} em revisão · {numero(metrics['no_coverage'])} sem cobertura", "Casos que merecem investigação no recorte atual."),
    ]
    for container, (label, value, context, help_text) in zip(kpis, items):
        with container:
            metric_card(label, value, context, help_text=help_text)

    st.markdown("<div class='section-kicker'>Mapa operacional · camadas, status e investigação</div>", unsafe_allow_html=True)
    selected_case = render_operational_map(records, data.recapes, key_prefix="showcase_map")
    if selected_case:
        st.caption(f"Caso selecionado: {selected_case}. Use a página Mapa para abrir os detalhes completos da investigação.")

    trend, regional, coverage = st.columns([1.2, 1, 1], gap="large")
    with trend:
        st.markdown("<div class='section-kicker'>Evolução mensal</div>", unsafe_allow_html=True)
        render_temporal_coverage(monthly_coverage(records))
    with regional:
        st.markdown("<div class='section-kicker'>Distribuição regional</div>", unsafe_allow_html=True)
        render_regional_ranking(regional_criticality(records))
    with coverage:
        st.markdown("<div class='section-kicker'>Cobertura do GeoJSON</div>", unsafe_allow_html=True)
        render_coverage_waterfall(coverage_waterfall(data.coverage_report, data.recapes))

    st.markdown("<div class='section-kicker'>Últimos processos</div>", unsafe_allow_html=True)
    latest = _last_processes(records)
    if latest.empty:
        empty_state("Sem processos para listar", "O recorte atual ainda não contém notificações processadas.")
        return
    st.dataframe(
        latest,
        use_container_width=True,
        hide_index=True,
        height=280,
        column_config={
            "Recebimento": st.column_config.DatetimeColumn("Recebimento", format="DD/MM/YYYY"),
            "Confiança": st.column_config.ProgressColumn("Confiança", min_value=0, max_value=100, format="%.0f%%"),
        },
    )
