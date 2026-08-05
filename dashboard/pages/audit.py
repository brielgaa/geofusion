from __future__ import annotations

from math import ceil

import pandas as pd
import streamlit as st

from dashboard.components.case_detail import render_case_detail
from dashboard.components.cards import page_header
from dashboard.components.empty_states import empty_state
from dashboard.components.filters import build_audit_filters
from dashboard.services.data_loader import AppData
from dashboard.services.metrics import prepare_audit_records
from dashboard.utils.status import status_label, status_recape_label


def _display_table(records: pd.DataFrame) -> pd.DataFrame:
    def column(name: str, default: object = "") -> pd.Series:
        if name in records.columns:
            return records[name]
        return pd.Series(default, index=records.index)

    table = pd.DataFrame({
        "Selecionar": False,
        "Situação": records["situacao_auditoria"].map(status_label),
        "Número da OS": column("numero_os"),
        "Fonte": column("fonte_notif"),
        "Endereço": column("rua_notif"),
        "Regional": column("prefeitura_regional"),
        "Recebimento": column("data_recebimento", pd.NaT),
        "Recape associado": column("rua_recape"),
        "Status do recape": column("status_recape").map(status_recape_label),
        "Método": column("metodo_match"),
        "Confiança": column("score_confianca", 0),
        "Distância (km)": pd.to_numeric(column("dist_recape_km", None), errors="coerce"),
        "case_id": records["case_id"],
    })
    return table


def render(data: AppData) -> None:
    records = prepare_audit_records(data.cruzamento)
    page_header("Auditoria", "Fila operacional para priorizar, investigar e exportar casos individuais.", "Investigação de casos")
    if records.empty:
        empty_state("Fila indisponível", "O arquivo cruzamento.csv não está disponível ou não possui registros.")
        return

    filtered_result = build_audit_filters(records)
    filtered = filtered_result.records.sort_values("data_recebimento", ascending=False, na_position="last")
    st.caption(f"{len(filtered):,} de {len(records):,} casos encontrados.")
    if filtered.empty:
        empty_state("Nenhum caso encontrado", "Ajuste ou limpe os filtros para voltar à fila completa.")
        return

    top_left, top_right = st.columns([5, 1])
    with top_left:
        st.markdown("<div class='section-kicker'>Fila de auditoria</div>", unsafe_allow_html=True)
    with top_right:
        st.download_button("Exportar recorte", data=filtered.to_csv(index=False).encode("utf-8-sig"), file_name="auditoria_obras_sp.csv", mime="text/csv", use_container_width=True)

    page_size = st.selectbox("Casos por página", [25, 50, 100], index=1, key="audit_page_size")
    pages = max(1, ceil(len(filtered) / page_size))
    page = st.number_input("Página", min_value=1, max_value=pages, value=1, step=1, key="audit_page")
    start = (int(page) - 1) * page_size
    page_records = filtered.iloc[start:start + page_size]
    edited = st.data_editor(
        _display_table(page_records),
        key=f"audit_table_{page}_{page_size}",
        hide_index=True,
        use_container_width=True,
        disabled=[column for column in _display_table(page_records).columns if column != "Selecionar"],
        column_config={
            "Selecionar": st.column_config.CheckboxColumn("Abrir", help="Seleciona este caso para investigação."),
            "Recebimento": st.column_config.DateColumn("Recebimento", format="DD/MM/YYYY"),
            "Confiança": st.column_config.ProgressColumn("Confiança", min_value=0, max_value=100, format="%.0f%%"),
            "Distância (km)": st.column_config.NumberColumn("Distância", format="%.3f km"),
            "case_id": None,
        },
        height=520,
    )
    selected = edited.loc[edited["Selecionar"], "case_id"].tolist()
    if selected:
        st.session_state["selected_audit_case"] = selected[-1]
    st.caption(f"Página {int(page)} de {pages} · ordenado por data de recebimento mais recente.")

    selected_case = st.session_state.get("selected_audit_case")
    if selected_case:
        case = filtered[filtered["case_id"].eq(selected_case)]
        if not case.empty:
            st.divider()
            render_case_detail(case.iloc[0], data.recapes)
