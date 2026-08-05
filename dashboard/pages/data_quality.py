from __future__ import annotations

import pandas as pd
import plotly.express as px
import streamlit as st

from dashboard.components.cards import metric_card, page_header
from dashboard.components.charts import PLOT_COLORS, _layout, render_confidence_histogram, render_match_scatter
from dashboard.components.empty_states import empty_state
from dashboard.services.data_loader import AppData
from dashboard.services.metrics import confidence_distribution, coverage_metrics, prepare_audit_records
from dashboard.utils.formatting import numero, percentual


def render(data: AppData) -> None:
    records = prepare_audit_records(data.cruzamento)
    page_header("Qualidade dos dados", "Diagnóstico de confiança, falhas topológicas e lacunas das fontes operacionais.", "Confiabilidade")
    if records.empty:
        empty_state("Qualidade indisponível", "É necessário gerar cruzamento.csv antes de avaliar os indicadores.")
        return
    report = data.coverage_report
    metrics = coverage_metrics(records)
    failures = report.get("falhas_por_motivo", {}) or {}
    cards = st.columns(4)
    values = [
        ("Cobertura geoespacial", percentual(report.get("cobertura_pct", 0)), "Recapes com path GeoSampa"),
        ("Vias não resolvidas", numero(failures.get("SEM_RUA", 0) + failures.get("FUZZY_NAO_RESOLVEU", 0)), "Nome ou CODLOG sem resolução"),
        ("Falhas de caminho", numero(failures.get("SEM_CAMINHO", 0)), "Componentes sem caminho topológico"),
        ("Registros sem cobertura", numero(metrics["no_coverage"]), "Notificações sem recape associado"),
    ]
    for column, (label, value, context) in zip(cards, values):
        with column:
            metric_card(label, value, context, help_text=context)

    left, right = st.columns(2, gap="large")
    with left:
        st.markdown("<div class='section-kicker'>Histograma de confiança</div>", unsafe_allow_html=True)
        render_confidence_histogram(confidence_distribution(records))
        st.caption("Faixas: 0–69 crítica · 70–84 baixa · 85–94 aceitável · 95–100 alta.")
    with right:
        st.markdown("<div class='section-kicker'>Distância × score de nome</div>", unsafe_allow_html=True)
        render_match_scatter(records)

    st.markdown("<div class='section-kicker'>Falhas por etapa</div>", unsafe_allow_html=True)
    failure_data = pd.DataFrame([
        {"motivo": key.replace("_", " ").title(), "quantidade": value}
        for key, value in failures.items()
        if value
    ])
    if failure_data.empty:
        empty_state("Sem falhas técnicas registradas", "O relatório de cobertura não trouxe categorias com ocorrências.")
    else:
        failure_data = failure_data.sort_values("quantidade")
        figure = px.bar(failure_data, y="motivo", x="quantidade", orientation="h", color_discrete_sequence=[PLOT_COLORS["red"]])
        _layout(figure, 320)
        figure.update_layout(showlegend=False)
        st.plotly_chart(figure, use_container_width=True, config={"displayModeBar": False})

    st.markdown("<div class='section-kicker'>Falhas detalhadas de geometria</div>", unsafe_allow_html=True)
    if data.falhas.empty:
        empty_state("Arquivo de falhas indisponível", "O ETL não gerou recapes_sem_cobertura.csv neste processamento.")
        return
    columns = [
        "via", "de", "até", "codlog", "rua_encontrada_no_geosampa", "quantidade_segmentos_encontrados",
        "quantidade_intersecoes_de", "quantidade_intersecoes_ate", "componente_conectado_encontrado",
        "caminho_encontrado", "motivo_final_da_falha", "mensagem_detalhada",
    ]
    available = [column for column in columns if column in data.falhas.columns]
    st.dataframe(data.falhas[available], use_container_width=True, hide_index=True, height=420)
    st.download_button("Exportar falhas (CSV)", data=data.falhas.to_csv(index=False).encode("utf-8-sig"), file_name="recapes_sem_cobertura.csv", mime="text/csv")
