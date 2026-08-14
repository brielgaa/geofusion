from __future__ import annotations

import json
import re

import pandas as pd
import streamlit as st

from dashboard.components.cards import page_header
from dashboard.components.operational_map import render_result_map
from dashboard.components.operational_ui import (
    empty_panel,
    geometry_quality_panel,
    info_grid,
    provenance_panel,
    section_title,
    status_badge,
    warning_panel,
)
from dashboard.services.operational_dashboard import OperationalContext, lookup
from src.operational.models import StreetLookupQuery


def _parse_number(value: str) -> int | None:
    if not value.strip():
        return None
    try:
        number = float(value.replace(",", "."))
        return int(number) if number.is_integer() else None
    except ValueError:
        return None


def _parse_coordinates(value: str) -> tuple[float, float] | None:
    matches = re.findall(r"-?\d+(?:[.,]\d+)?", value)
    if len(matches) != 2:
        return None
    try:
        first, second = (float(item.replace(",", ".")) for item in matches)
    except ValueError:
        return None
    if -25 < first < -22 and -48 < second < -45:
        return first, second
    if -25 < second < -22 and -48 < first < -45:
        return second, first
    return None


def _query_from_input(value: str, number: str) -> StreetLookupQuery:
    text = value.strip()
    coordinates = _parse_coordinates(text)
    record_match = re.search(r"(?:id|recape)\s*[:#]?\s*([A-Za-z0-9_-]+)$", text, flags=re.IGNORECASE)
    if record_match:
        return StreetLookupQuery(record_id=record_match.group(1))
    if coordinates:
        return StreetLookupQuery(latitude=coordinates[0], longitude=coordinates[1])
    return StreetLookupQuery(street=text or None, number=_parse_number(number))


def _render_ambiguity(result) -> None:
    warning_panel(
        "Consulta ambígua — nenhuma alternativa foi selecionada automaticamente",
        f"A busca retornou {result.candidate_count} segmentos compatíveis. Selecione uma alternativa para inspecionar a evidência geométrica.",
        code="DO_NOT_SELECT_SILENTLY",
    )
    alternatives = result.alternatives
    if not alternatives:
        return
    table = pd.DataFrame(
        [
            {
                "Alternativa": index + 1,
                "Segmento": candidate.segment_id,
                "Via retornada": candidate.street,
                "Faixa numérica": f"{candidate.number_range.get('start') or '—'}–{candidate.number_range.get('end') or '—'}",
                "Distância (m)": candidate.distance_to_segment_m,
                "Fonte": candidate.source,
            }
            for index, candidate in enumerate(alternatives)
        ]
    )
    table["Distância (m)"] = table["Distância (m)"].map(lambda value: f"{float(value):.1f}" if value is not None else "—")
    st.markdown(table.to_html(index=False, classes="gf-html-table"), unsafe_allow_html=True)
    selected = st.selectbox("Alternativa para inspeção", range(len(alternatives)), format_func=lambda index: f"{index + 1} · {alternatives[index].street} · {alternatives[index].segment_id}", key="query_alternative")
    candidate = alternatives[selected]
    section_title("Alternativa selecionada", "evidência do segmento")
    info_grid(
        [
            ("Via", candidate.street),
            ("Segmento", candidate.segment_id),
            ("Codlog", candidate.codlog),
            ("Faixa", f"{candidate.number_range.get('start') or '—'}–{candidate.number_range.get('end') or '—'}"),
            ("Método", candidate.number_match),
            ("Fonte", candidate.source),
        ],
        columns=3,
    )
    render_result_map(candidate.geometry_wkt, key="query_alternative_map")


def _render_result(context: OperationalContext, result, query: StreetLookupQuery) -> None:
    if result.location.confidence == "AMBIGUOUS":
        _render_ambiguity(result.location)
        return
    if result.location.confidence in {"NOT_FOUND", "UNSUPPORTED"}:
        warning_panel("Localização não resolvida", "A camada operacional não encontrou uma via compatível com os parâmetros informados.", code=(result.location.match_method or "NOT_FOUND"))
        if result.location.alternatives:
            st.dataframe(pd.DataFrame([candidate.to_dict() for candidate in result.location.alternatives]), hide_index=True, use_container_width=True)
        else:
            section_title("Próximo passo", "ajuste a entrada e tente novamente")
            empty_panel("Nenhuma alternativa retornada", "Revise a grafia da via, informe um número quando disponível ou use coordenadas no formato latitude, longitude.")
        return

    section_title("Resultado operacional", f"{result.data_quality.status.lower()} · {result.location.match_method}")
    top = st.columns([2.2, 1, 1, 1], gap="medium")
    with top[0]:
        st.markdown(f"<div class='detail-panel'><div class='section-kicker'>Via resolvida</div><div style='font-size:1.14rem;font-weight:700;color:#E8EEF7'>{result.location.matched_street or '—'}</div><div style='color:#8FA1B7;margin-top:5px'>{result.location.normalized_street or 'normalização indisponível'}</div></div>", unsafe_allow_html=True)
    with top[1]:
        st.markdown("<div class='section-kicker'>Qualidade</div>", unsafe_allow_html=True)
        st.markdown(status_badge(result.data_quality.status), unsafe_allow_html=True)
    with top[2]:
        st.markdown("<div class='section-kicker'>Superfície</div>", unsafe_allow_html=True)
        st.markdown(status_badge("FOUND" if result.surface.status == "FOUND" else "UNKNOWN_DATE", label=result.surface.surface_type or "Não informada"), unsafe_allow_html=True)
    with top[3]:
        st.markdown("<div class='section-kicker'>Proteção</div>", unsafe_allow_html=True)
        st.markdown(status_badge(result.protection.status), unsafe_allow_html=True)

    info_grid(
        [
            ("Método de localização", result.location.match_method),
            ("Confiança", result.location.confidence),
            ("Segmento", result.location.segment_id),
            ("Regional", result.administrative_area.get("regional")),
            ("Subprefeitura", result.administrative_area.get("subprefecture")),
            ("Distância ao segmento", f"{result.location.distance_to_segment_m:.1f} m" if result.location.distance_to_segment_m is not None else None),
        ],
        columns=3,
    )

    latest = result.resurfacing.latest_resurfacing
    section_title("Recape mais recente", "data de término / conclusão")
    info_grid(
        [
            ("ID do recape", latest.resurfacing_id if latest else None),
            ("Data", latest.resurfacing_date if latest else None),
            ("Tipo de data", latest.resurfacing_date_type if latest else None),
            ("Fonte da data", latest.resurfacing_date_source if latest else None),
            ("Revestimento", latest.raw_surface_type if latest else result.surface.surface_type),
            ("Fim da proteção", result.protection.end_date),
        ],
        columns=3,
    )
    if result.protection.status == "EXPIRING_SOON":
        warning_panel("Proteção requer atenção", f"Restam {result.protection.days_remaining} dias na janela de proteção.", code="NEEDS_ATTENTION")
    elif result.protection.status == "UNKNOWN_DATE":
        warning_panel("Proteção sem data utilizável", "A data de término do recape não está disponível. A aplicação não infere a data de execução a partir da notificação.", code="NOTIFICATION_DATE_NOT_USED_AS_EXECUTION_DATE")

    left, right = st.columns([1.1, 1], gap="large")
    with left:
        render_result_map(result.geometry.official_wkt or result.geometry.shadow_wkt, latitude=query.latitude, longitude=query.longitude, key="query_result_map")
    with right:
        geometry_quality_panel(result.geometry)

    if query.record_id:
        related = context.crossmatch[context.crossmatch.get("id_recape", pd.Series(dtype=str)).astype(str).eq(str(query.record_id))].copy() if not context.crossmatch.empty else pd.DataFrame()
        if not related.empty:
            section_title("Notificações associadas", "fila de atenção operacional")
            related["análise"] = "NEEDS_ATTENTION"
            columns = [column for column in ["numero_os", "rua_notif", "status_notif", "data_recebimento", "análise"] if column in related.columns]
            st.dataframe(related[columns].head(100), hide_index=True, use_container_width=True)

    with st.expander("Proveniência e avisos", expanded=False):
        provenance_panel(result.location.provenance + result.surface.provenance + result.resurfacing.provenance)
        if result.warnings:
            st.code("\n".join(result.warnings), language="text")
    with st.expander("Contrato serializado", expanded=False):
        st.json(result.to_dict())


def render(context: OperationalContext) -> None:
    page_header("Consulta de Via", "Resolva uma via, número, coordenada ou ID de recape com alternativas explícitas e rastreabilidade.", "Busca operacional")
    prefill = st.session_state.pop("query_prefill", "")
    with st.form("operational_query_form"):
        input_col, number_col, submit_col = st.columns([5, 1.35, 1], gap="medium")
        with input_col:
            value = st.text_input("Via, coordenadas ou ID", value=prefill, placeholder="AV. ENG BILLINGS · -23.55,-46.73 · ID 2097")
        with number_col:
            number = st.text_input("Número (opcional)", placeholder="3400")
        with submit_col:
            st.write("")
            submitted = st.form_submit_button("Consultar", type="primary", use_container_width=True)
    st.caption("O número usa faixas GeoSampa quando disponíveis. Em caso de múltiplos segmentos, o sistema mantém a ambiguidade visível.")
    if submitted:
        st.session_state["last_operational_query"] = _query_from_input(value, number)
    query = st.session_state.get("last_operational_query")
    if query is None:
        warning_panel("Comece pela localização", "Informe uma via, coordenadas ou ID. A consulta não escolhe um segmento silenciosamente.")
        return
    result = lookup(context, query)
    _render_result(context, result, query)
