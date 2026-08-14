from __future__ import annotations

import pandas as pd
import streamlit as st

from dashboard.components.cards import page_header
from dashboard.components.operational_ui import geometry_quality_panel, info_grid, metric_row, provenance_panel, section_title, status_badge, warning_panel
from dashboard.services.operational_dashboard import OperationalContext, lookup
from dashboard.utils.formatting import data, numero
from src.operational.models import StreetLookupQuery


def _evidence_rows(context: OperationalContext, record_id: str) -> pd.DataFrame:
    shadow = context.repository.shadow_quality_by_id.get(record_id, {})
    validator = context.repository.validator_by_id.get(record_id, {})
    consensus = context.repository.consensus_by_id.get(record_id, {})
    row = context.repository.recape_by_id.get(record_id)
    return pd.DataFrame(
        [
            {"Família": "Oficial", "Status": "AVAILABLE" if row and row.geometry is not None else "UNAVAILABLE", "Fonte": "data/processed/recape_clean.csv:path", "Evidência": "geometria oficial preservada" if row and row.geometry is not None else "sem path oficial"},
            {"Família": "Reconstrução shadow", "Status": "AVAILABLE" if shadow.get("geometry_wkt") else "UNAVAILABLE", "Fonte": "route_geometry_quality_shadow.csv", "Evidência": shadow.get("geometry_confidence") or "—"},
            {"Família": "Validador", "Status": "AVAILABLE" if validator else "UNAVAILABLE", "Fonte": "geometry_validation_shadow.csv", "Evidência": validator.get("validation_class") or "—"},
            {"Família": "Consensus", "Status": "AVAILABLE" if consensus else "UNAVAILABLE", "Fonte": "consensus_evidence_shadow.csv", "Evidência": consensus.get("consensus_class") or "—"},
            {"Família": "Boundary / nome", "Status": "NOT_USED_IN_OPERATIONAL_QUERY", "Fonte": "auditorias congeladas", "Evidência": "família preservada fora da promoção oficial"},
        ]
    )


def render(context: OperationalContext) -> None:
    page_header("Auditoria", "Leia a evidência técnica de um recape sem misturar geometrias oficiais, reconstruções shadow e validações.", "Evidência rastreável")
    record_ids = context.recapes["record_id"].astype(str).tolist()
    input_id, select_col = st.columns([2, 3], gap="medium")
    with input_id:
        record_id = st.text_input("ID do recape", placeholder="Ex.: 2097", key="audit_record_id")
    with select_col:
        selected = st.selectbox("Ou selecione um ID carregado", [""] + record_ids[:2000], key="audit_record_select")
    record_id = record_id.strip() or selected
    if not record_id:
        warning_panel("Selecione um recape", "A auditoria é orientada por evidência e começa por um identificador rastreável.")
        return
    if record_id not in context.repository.recape_by_id:
        warning_panel("ID não encontrado", "O ID informado não está no recape_clean.csv operacional.", code="RECORD_ID_NOT_FOUND")
        return
    result = lookup(context, StreetLookupQuery(record_id=record_id))
    record = context.repository.recape_by_id[record_id]
    section_title("Caso auditado", f"ID {record_id}")
    info_grid(
        [("Via", record.street), ("Status", record.status), ("Subprefeitura", record.subprefecture), ("Revestimento", record.raw_surface_type), ("Data de término", record.resurfacing_date), ("Qualidade operacional", result.data_quality.status)],
        columns=3,
    )
    geometry_quality_panel(result.geometry)

    section_title("Famílias de evidência", "a ausência também é um resultado")
    evidence = _evidence_rows(context, record_id)
    available = int(evidence["Status"].eq("AVAILABLE").sum())
    unavailable = int(evidence["Status"].eq("UNAVAILABLE").sum())
    metric_row(
        [
            ("Fontes disponíveis", numero(available), "evidência carregada"),
            ("Indisponíveis", numero(unavailable), "lacunas preservadas"),
            ("Famílias observadas", numero(len(evidence)), "contrato de auditoria"),
        ],
        primary_index=0,
    )
    evidence["Status"] = evidence["Status"].map(status_badge)
    st.markdown(evidence.to_html(index=False, escape=False, classes="gf-html-table"), unsafe_allow_html=True)

    section_title("Linha do tempo", "datas explicitamente identificadas")
    timeline = pd.DataFrame(
        [
            {"Evento": "Data de término / conclusão", "Data": record.resurfacing_date, "Fonte": "recape_clean.csv:data_termino", "Uso": "janela de proteção"},
            {"Evento": "Execução da intervenção", "Data": "não disponível", "Fonte": "não há evento operacional explícito", "Uso": "não inferida"},
        ]
    )
    timeline["Data"] = timeline["Data"].map(data)
    st.dataframe(timeline, hide_index=True, use_container_width=True)
    warning_panel("Regra temporal", "A data de recebimento de uma notificação não é usada como data de execução do recape.", code="NOTIFICATION_DATE_NOT_USED_AS_EXECUTION_DATE")

    related = context.crossmatch[context.crossmatch.get("id_recape", pd.Series(dtype=str)).astype(str).eq(str(record_id))].copy() if not context.crossmatch.empty else pd.DataFrame()
    if not related.empty:
        section_title("Notificações associadas", "fila NEEDS_ATTENTION")
        related["classificação"] = "NEEDS_ATTENTION"
        columns = [column for column in ["numero_os", "rua_notif", "status_notif", "data_recebimento", "classificação"] if column in related.columns]
        st.dataframe(related[columns].head(100), hide_index=True, use_container_width=True)

    with st.expander("Proveniência completa", expanded=False):
        provenance_panel(result.location.provenance + result.surface.provenance + result.resurfacing.provenance)
        st.json(result.to_dict())
