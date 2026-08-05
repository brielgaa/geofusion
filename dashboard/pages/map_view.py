from __future__ import annotations

import pandas as pd
import streamlit as st

from dashboard.components.case_detail import render_case_detail
from dashboard.components.cards import page_header
from dashboard.components.map_components import render_operational_map
from dashboard.services.data_loader import AppData
from dashboard.services.metrics import prepare_audit_records
from dashboard.utils.status import CONCLUIDO, EM_ANDAMENTO, PLANEJADO, REVISAO, SEM_COBERTURA, status_label


def render(data: AppData) -> None:
    records = prepare_audit_records(data.cruzamento)
    page_header("Mapa", "Contexto espacial para relacionar notificações, cobertura e trechos reais do GeoSampa.", "Camadas geoespaciais")
    if records.empty:
        st.info("Não há registros processados para exibir no mapa.")
        return
    search, regional = st.columns([2, 1])
    with search:
        query = st.text_input("Buscar por OS ou endereço", key="map_search", placeholder="Ex.: OS 123456 ou nome da via")
    with regional:
        regions = ["Todas"] + sorted(records["prefeitura_regional"].dropna().astype(str).unique().tolist()) if "prefeitura_regional" in records.columns else ["Todas"]
        region = st.selectbox("Zoom por regional", regions, key="map_regional")
    scoped = records.copy()
    if query.strip():
        query_text = query.strip()
        mask = scoped["numero_os"].fillna("").astype(str).str.contains(query_text, case=False, regex=False)
        for column in ("rua_notif", "rua_recape"):
            if column in scoped.columns:
                mask |= scoped[column].fillna("").astype(str).str.contains(query_text, case=False, regex=False)
        scoped = scoped[mask]
    if region != "Todas" and "prefeitura_regional" in scoped.columns:
        scoped = scoped[scoped["prefeitura_regional"].eq(region)]

    legend = [CONCLUIDO, PLANEJADO, EM_ANDAMENTO, SEM_COBERTURA, REVISAO]
    st.caption(" · ".join(f"{status_label(code)}" for code in legend) + ". As cores dos recapes usam semântica própria: verde, cinza, âmbar e azul.")
    selected = render_operational_map(scoped, data.recapes)
    if selected:
        st.session_state["selected_map_case"] = selected
    case_id = st.session_state.get("selected_map_case")
    if case_id:
        case = scoped[scoped["case_id"].eq(case_id)]
        if not case.empty:
            st.divider()
            render_case_detail(case.iloc[0], data.recapes)
