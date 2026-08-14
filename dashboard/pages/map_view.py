from __future__ import annotations

import pandas as pd
import streamlit as st

from dashboard.components.cards import page_header
from dashboard.components.operational_map import render_recape_map
from dashboard.components.operational_ui import section_title, status_badge, warning_panel
from dashboard.services.operational_dashboard import OperationalContext
from dashboard.utils.formatting import data, numero


QUALITY_LABELS = {
    "OFFICIAL": "Oficial",
    "SHADOW_HIGH": "Shadow · alta",
    "SHADOW_MEDIUM": "Shadow · média",
    "ESTIMATED": "Estimada",
    "UNRESOLVED": "Não resolvida",
}


def render(context: OperationalContext) -> None:
    page_header("Mapa", "Explore recapes por qualidade geométrica e proteção temporal, mantendo as camadas oficiais e shadow separadas.", "Cobertura espacial")
    frame = context.recapes.copy()
    section_title("Filtros do mapa", "qualidade, proteção e recorte administrativo")
    filter_cols = st.columns([1.5, 1.7, 1.5, 1.5], gap="medium")
    with filter_cols[0]:
        qualities = st.multiselect("Qualidade geométrica", list(QUALITY_LABELS), default=["OFFICIAL", "SHADOW_HIGH", "SHADOW_MEDIUM", "ESTIMATED"], format_func=lambda code: QUALITY_LABELS[code], key="map_quality")
    with filter_cols[1]:
        subprefectures = st.multiselect("Subprefeitura", sorted(frame["subprefeitura"].dropna().astype(str).replace("", pd.NA).dropna().unique().tolist()), key="map_subprefecture")
    with filter_cols[2]:
        surfaces = st.multiselect("Revestimento", sorted(frame["surface_display"].dropna().astype(str).unique().tolist()), key="map_surface")
    with filter_cols[3]:
        protection = st.multiselect("Proteção", ["ACTIVE", "EXPIRING_SOON", "EXPIRED", "UNKNOWN_DATE"], format_func=lambda code: {"ACTIVE": "Ativa", "EXPIRING_SOON": "Expira em breve", "EXPIRED": "Expirada", "UNKNOWN_DATE": "Data desconhecida"}[code], key="map_protection")
    filtered = frame[frame["quality_code"].isin(qualities)].copy()
    if subprefectures:
        filtered = filtered[filtered["subprefeitura"].astype(str).isin(subprefectures)]
    if surfaces:
        filtered = filtered[filtered["surface_display"].astype(str).isin(surfaces)]
    if protection:
        filtered = filtered[filtered["protection_status"].isin(protection)]
    st.caption(f"{numero(len(filtered))} recapes no recorte · {numero(int(filtered['has_official_geometry'].sum()))} com geometria oficial · hover no trecho para inspeção.")

    section_title("Camadas e foco", "leitura visual da seleção")
    layer_cols = st.columns([2, 2, 2, 2], gap="medium")
    with layer_cols[0]:
        protection_overlay = st.checkbox("Colorir por proteção", value=False, key="map_protection_overlay")
    with layer_cols[1]:
        selected_id = st.selectbox("Foco no recape", [""] + filtered["record_id"].astype(str).head(1000).tolist(), format_func=lambda value: "Nenhum foco" if not value else f"ID {value}", key="map_selected_id")
    with layer_cols[2]:
        show_table = st.checkbox("Mostrar tabela", value=True, key="map_show_table")
    with layer_cols[3]:
        st.markdown("<div class='section-kicker'>Legenda</div><div style='font-size:.74rem;color:#8FA1B7'>Oficial · shadow alta/média · estimada · não resolvida</div>", unsafe_allow_html=True)

    render_recape_map(filtered, show_quality=set(qualities), protection_overlay=protection_overlay, selected_id=selected_id or None, key="operational_recape_map")
    if show_table:
        section_title("Registros no recorte", "máximo 500 para leitura")
        table = filtered[["record_id", "street_display", "subprefeitura", "surface_display", "quality_label", "protection_status", "completion_date"]].head(500).copy()
        table["completion_date"] = table["completion_date"].map(data)
        table.columns = ["ID", "Via", "Subprefeitura", "Revestimento", "Geometria", "Proteção", "Data de término"]
        table["Geometria"] = table["Geometria"].map(status_badge)
        table["Proteção"] = table["Proteção"].map(status_badge)
        st.markdown(table.to_html(index=False, escape=False, classes="gf-html-table"), unsafe_allow_html=True)
    if not qualities:
        warning_panel("Nenhuma camada selecionada", "Ative uma ou mais categorias de qualidade geométrica.")
