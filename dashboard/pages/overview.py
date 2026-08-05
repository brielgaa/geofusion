from __future__ import annotations

import pandas as pd
import streamlit as st

from dashboard.components.cards import metric_card, page_header
from dashboard.components.charts import render_coverage_waterfall, render_match_sankey, render_regional_ranking, render_temporal_coverage
from dashboard.components.empty_states import empty_state
from dashboard.services.data_loader import AppData
from dashboard.services.metrics import coverage_delta, coverage_metrics, coverage_waterfall, monthly_coverage, prepare_audit_records, regional_criticality
from dashboard.utils.formatting import numero, percentual


def render(data: AppData) -> None:
    records = prepare_audit_records(data.cruzamento)
    page_header("Visão geral", "Cobertura geoespacial, qualidade de correspondência e áreas que exigem investigação.", "Operação diária")
    if records.empty:
        empty_state("Dados de cruzamento indisponíveis", "Rode python src/transform.py para gerar os artefatos processados.")
        return

    metrics = coverage_metrics(records)
    delta = coverage_delta(records)
    updated = data.updated_at.strftime("%d/%m/%Y %H:%M") if data.updated_at is not None else "não disponível"
    quality = "Em atenção" if metrics["confirmed_pct"] < 70 else "Saudável"
    st.caption(f"Dados atualizados em {updated} · {numero(metrics['total'])} registros · qualidade da última leitura: {quality}")

    primary, definition = st.columns([2, 1], gap="large")
    with primary:
        delta_text = f"{delta:+.1f} p.p. vs. mês anterior".replace(".", ",") if delta is not None else None
        metric_card(
            "Cobertura confirmada",
            percentual(metrics["confirmed_pct"]),
            f"{numero(metrics['confirmed'])} casos com correspondência que não exige revisão manual.",
            primary=True,
            delta=delta_text,
            help_text="Percentual de notificações com match encontrado por regra forte e sem sinalização de revisão.",
        )
    with definition:
        metric_card(
            "Leitura do indicador",
            numero(metrics["found"]),
            f"{numero(metrics['review'])} matches exigem revisão; {numero(metrics['no_coverage'])} ficaram sem cobertura.",
            help_text="Match encontrado inclui correspondências que ainda precisam de confirmação humana.",
        )

    cards = st.columns(4, gap="medium")
    card_items = [
        ("Notificações analisadas", numero(metrics["total"]), "Volume no recorte atual", "Registros gerados pelo cruzamento."),
        ("Correspondências encontradas", numero(metrics["found"]), percentual(metrics["found_pct"]), "Notificações com algum recape associado."),
        ("Casos em revisão", numero(metrics["review"]), percentual(metrics["review_pct"]), "Matches de método fraco ou score inferior a 85."),
        ("Sem cobertura", numero(metrics["no_coverage"]), percentual(metrics["no_coverage_pct"]), "Notificações sem recape associado pela estratégia atual."),
    ]
    for column, (label, value, context, help_text) in zip(cards, card_items):
        with column:
            metric_card(label, value, context, help_text=help_text)

    left, right = st.columns([1.15, 1], gap="large")
    with left:
        st.markdown("<div class='section-kicker'>Fluxo de correspondência</div>", unsafe_allow_html=True)
        render_match_sankey(records)
    with right:
        st.markdown("<div class='section-kicker'>Perdas no roteamento GeoSampa</div>", unsafe_allow_html=True)
        render_coverage_waterfall(coverage_waterfall(data.coverage_report, data.recapes))

    trend, regional = st.columns([1.25, 1], gap="large")
    with trend:
        st.markdown("<div class='section-kicker'>Cobertura por mês</div>", unsafe_allow_html=True)
        monthly = monthly_coverage(records)
        render_temporal_coverage(monthly)
        if not monthly.empty:
            table = monthly[["mes", "total", "confirmada", "sem_cobertura", "baixa_confianca", "cobertura_pct"]].copy()
            table["cobertura_pct"] = table["cobertura_pct"].map(lambda value: f"{value:.1f}%")
            st.dataframe(table, use_container_width=True, hide_index=True, height=210)
    with regional:
        st.markdown("<div class='section-kicker'>Regionais críticas</div>", unsafe_allow_html=True)
        ranking = regional_criticality(records)
        render_regional_ranking(ranking)
        if not ranking.empty:
            view = ranking[["regional", "total", "sem_cobertura_pct", "baixa_confianca_pct", "status"]].head(10).copy()
            view["sem_cobertura_pct"] = view["sem_cobertura_pct"].map(lambda value: f"{value:.1f}%")
            view["baixa_confianca_pct"] = view["baixa_confianca_pct"].map(lambda value: f"{value:.1f}%")
            st.dataframe(view, use_container_width=True, hide_index=True, height=210)
