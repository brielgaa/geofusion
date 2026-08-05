"""Filtros explícitos para a fila operacional de auditoria."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from math import ceil

import pandas as pd
import streamlit as st

from dashboard.utils.formatting import escapar
from dashboard.utils.status import status_label, status_recape_label


@dataclass
class FilterResult:
    records: pd.DataFrame
    active_filters: list[str]


def _options(records: pd.DataFrame, column: str) -> list[str]:
    if column not in records.columns:
        return []
    return sorted(value for value in records[column].dropna().astype(str).unique().tolist() if value and value != "nan")


def _clear(prefix: str) -> None:
    for suffix in (
        "os", "address", "source", "regional", "situation", "method", "confidence",
        "distance", "recape_status", "dates", "review_only",
    ):
        st.session_state.pop(f"{prefix}_{suffix}", None)


def _active_chips(filters: list[str]) -> None:
    if not filters:
        st.markdown('<div class="filter-summary">Nenhum filtro ativo — exibindo toda a base disponível.</div>', unsafe_allow_html=True)
        return
    chips = "".join(f'<span class="active-chip">{escapar(item)}</span>' for item in filters)
    st.markdown(f'<div class="filter-summary">Filtros ativos<br/>{chips}</div>', unsafe_allow_html=True)


def build_audit_filters(records: pd.DataFrame, prefix: str = "audit") -> FilterResult:
    """Renderiza filtros centrais e devolve um recorte sem mutar os dados."""
    if records.empty:
        return FilterResult(records.copy(), [])

    with st.container():
        st.markdown('<div class="filter-shell">', unsafe_allow_html=True)
        top_left, top_right = st.columns([5, 1])
        with top_left:
            st.markdown("<div class='section-kicker'>Filtros da auditoria</div>", unsafe_allow_html=True)
        with top_right:
            if st.button("Limpar filtros", key=f"{prefix}_clear", use_container_width=True):
                _clear(prefix)
                st.rerun()

        search_left, search_right = st.columns(2)
        with search_left:
            os_query = st.text_input("Buscar por OS", key=f"{prefix}_os", placeholder="Ex.: 123456")
        with search_right:
            address_query = st.text_input("Buscar por endereço", key=f"{prefix}_address", placeholder="Rua, avenida ou trecho")

        row_one = st.columns(4)
        with row_one[0]:
            sources = st.multiselect("Fonte", _options(records, "fonte_notif"), key=f"{prefix}_source")
        with row_one[1]:
            regionals = st.multiselect("Regional", _options(records, "prefeitura_regional"), key=f"{prefix}_regional")
        with row_one[2]:
            situations = st.multiselect(
                "Situação", _options(records, "situacao_auditoria"), key=f"{prefix}_situation", format_func=status_label
            )
        with row_one[3]:
            methods = st.multiselect("Método de match", _options(records, "metodo_match"), key=f"{prefix}_method")

        row_two = st.columns(4)
        with row_two[0]:
            confidence = st.slider("Faixa de confiança", 0, 100, (0, 100), key=f"{prefix}_confidence")
        distances = pd.to_numeric(records.get("dist_recape_km", pd.Series(dtype=float)), errors="coerce")
        max_distance = max(0.3, float(ceil(distances.dropna().max() * 10) / 10) if distances.notna().any() else 0.3)
        with row_two[1]:
            distance = st.slider("Distância máxima (km)", 0.0, max_distance, (0.0, max_distance), 0.05, key=f"{prefix}_distance")
        with row_two[2]:
            recape_status = st.multiselect(
                "Status do recape", _options(records, "status_recape"), key=f"{prefix}_recape_status", format_func=status_recape_label
            )
        with row_two[3]:
            review_only = st.checkbox("Somente casos em revisão", key=f"{prefix}_review_only")

        dates: tuple[date, date] | None = None
        if "data_recebimento" in records.columns:
            parsed_dates = pd.to_datetime(records["data_recebimento"], errors="coerce").dropna()
            if not parsed_dates.empty:
                start, end = parsed_dates.min().date(), parsed_dates.max().date()
                selected_dates = st.date_input("Período de recebimento", value=(start, end), min_value=start, max_value=end, key=f"{prefix}_dates")
                if isinstance(selected_dates, tuple) and len(selected_dates) == 2:
                    dates = selected_dates
        st.markdown("</div>", unsafe_allow_html=True)

    filtered = records.copy()
    active: list[str] = []
    if os_query.strip() and "numero_os" in filtered.columns:
        filtered = filtered[filtered["numero_os"].fillna("").astype(str).str.contains(os_query.strip(), case=False, regex=False)]
        active.append(f"OS contém {os_query.strip()}")
    if address_query.strip():
        address = address_query.strip()
        street_columns = [column for column in ("rua_notif", "rua_recape") if column in filtered.columns]
        if street_columns:
            mask = pd.Series(False, index=filtered.index)
            for column in street_columns:
                mask |= filtered[column].fillna("").astype(str).str.contains(address, case=False, regex=False)
            filtered = filtered[mask]
        active.append(f"Endereço contém {address}")
    selectors = (
        ("fonte_notif", sources, "Fonte"),
        ("prefeitura_regional", regionals, "Regional"),
        ("situacao_auditoria", situations, "Situação"),
        ("metodo_match", methods, "Método"),
        ("status_recape", recape_status, "Status recape"),
    )
    for column, selected, label in selectors:
        if selected and column in filtered.columns:
            filtered = filtered[filtered[column].isin(selected)]
            active.append(f"{label}: {len(selected)}")
    if "score_confianca" in filtered.columns and confidence != (0, 100):
        filtered = filtered[filtered["score_confianca"].between(confidence[0], confidence[1])]
        active.append(f"Confiança {confidence[0]}–{confidence[1]}")
    if "dist_recape_km" in filtered.columns and distance != (0.0, max_distance):
        distance_series = pd.to_numeric(filtered["dist_recape_km"], errors="coerce")
        filtered = filtered[distance_series.between(distance[0], distance[1])]
        active.append(f"Distância {distance[0]:.2f}–{distance[1]:.2f} km")
    if review_only and "exige_revisao" in filtered.columns:
        filtered = filtered[filtered["exige_revisao"]]
        active.append("Exige revisão")
    if dates and "data_recebimento" in filtered.columns:
        received = pd.to_datetime(filtered["data_recebimento"], errors="coerce").dt.date
        filtered = filtered[received.between(dates[0], dates[1])]
        active.append(f"Período {dates[0].strftime('%d/%m/%Y')}–{dates[1].strftime('%d/%m/%Y')}")

    _active_chips(active)
    return FilterResult(filtered, active)
