from __future__ import annotations

import pandas as pd
import streamlit as st

from dashboard.components.cards import page_header
from dashboard.components.operational_ui import metric_row, section_title, status_badge, warning_panel
from dashboard.services.operational_dashboard import OperationalContext, REFERENCE_DATE
from dashboard.utils.formatting import data, numero


STATUSES = ["ACTIVE", "EXPIRING_SOON", "EXPIRED", "UNKNOWN_DATE"]


def render(context: OperationalContext) -> None:
    page_header("Proteção de Recapes", "Acompanhe janelas temporais de um ano a partir da data de término/conclusão disponível.", "Controle temporal")
    frame = context.recapes.copy()
    st.caption(f"Referência: {REFERENCE_DATE.strftime('%d/%m/%Y')} · início inclusivo · aniversário final exclusivo · {numero(len(frame))} registros avaliados.")
    counts = frame["protection_status"].value_counts()
    metric_row(
        [
            ("Ativas", numero(int(counts.get("ACTIVE", 0))), "janela em vigor"),
            ("Expiram em breve", numero(int(counts.get("EXPIRING_SOON", 0))), "até 30 dias"),
            ("Expiradas", numero(int(counts.get("EXPIRED", 0))), "fora da janela"),
            ("Data desconhecida", numero(int(counts.get("UNKNOWN_DATE", 0))), "não inferida"),
        ],
        primary_index=1,
    )
    section_title("Filtros", "aplicados antes da tabela")
    filter_cols = st.columns([1.7, 1.5, 1.5, 1.2], gap="medium")
    with filter_cols[0]:
        selected_status = st.multiselect("Status", STATUSES, default=["EXPIRING_SOON", "UNKNOWN_DATE"], format_func=lambda code: {"ACTIVE": "Ativa", "EXPIRING_SOON": "Expira em breve", "EXPIRED": "Expirada", "UNKNOWN_DATE": "Data desconhecida"}[code], key="protection_status_filter")
    with filter_cols[1]:
        selected_subprefecture = st.multiselect("Subprefeitura", sorted(frame["subprefeitura"].dropna().astype(str).replace("", pd.NA).dropna().unique().tolist()), key="protection_subprefecture_filter")
    with filter_cols[2]:
        selected_surface = st.multiselect("Revestimento", sorted(frame["surface_display"].dropna().astype(str).unique().tolist()), key="protection_surface_filter")
    with filter_cols[3]:
        limit = st.number_input("Máximo de linhas", min_value=25, max_value=1000, value=200, step=25, key="protection_limit")
    filtered = frame[frame["protection_status"].isin(selected_status)].copy() if selected_status else frame.iloc[0:0].copy()
    if selected_subprefecture:
        filtered = filtered[filtered["subprefeitura"].astype(str).isin(selected_subprefecture)]
    if selected_surface:
        filtered = filtered[filtered["surface_display"].astype(str).isin(selected_surface)]
    filtered = filtered.sort_values(["protection_status", "days_remaining", "completion_date"], na_position="last")
    st.caption(f"{numero(len(filtered))} registros no recorte · status de atenção é operacional, não uma conclusão de violação.")
    if not filtered.empty:
        table = filtered[["record_id", "street_display", "subprefeitura", "surface_display", "completion_date", "protection_start", "protection_end", "days_remaining", "protection_status", "quality_label"]].head(int(limit)).copy()
        for column in ["completion_date", "protection_start", "protection_end"]:
            table[column] = pd.to_datetime(table[column], errors="coerce").dt.strftime("%d/%m/%Y").fillna("—")
        table["days_remaining"] = table["days_remaining"].map(lambda value: numero(value, casas=0))
        table.columns = ["ID", "Via", "Subprefeitura", "Revestimento", "Data de término", "Início", "Fim", "Dias restantes", "Status", "Geometria"]
        table["Status"] = table["Status"].map(status_badge)
        st.markdown(table.to_html(index=False, escape=False, classes="gf-html-table"), unsafe_allow_html=True)
    else:
        warning_panel("Nenhum registro no recorte", "Selecione pelo menos um status ou limpe os filtros.")

    attention = frame[frame["protection_status"].eq("EXPIRING_SOON")]
    if not attention.empty:
        section_title("Fila NEEDS_ATTENTION", "recapes que pedem revisão operacional")
        attention_view = attention[["record_id", "street_display", "completion_date", "protection_end", "days_remaining"]].sort_values("days_remaining").head(20).copy()
        attention_view["completion_date"] = pd.to_datetime(attention_view["completion_date"], errors="coerce").dt.strftime("%d/%m/%Y").fillna("—")
        attention_view["protection_end"] = pd.to_datetime(attention_view["protection_end"], errors="coerce").dt.strftime("%d/%m/%Y").fillna("—")
        attention_view["status"] = "NEEDS_ATTENTION"
        st.dataframe(attention_view, hide_index=True, use_container_width=True)
