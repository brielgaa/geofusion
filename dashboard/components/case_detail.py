"""Painel de investigação de um caso individual, sem persistência implícita."""
from __future__ import annotations

import json
from typing import Any

import pandas as pd
import streamlit as st

from dashboard.components.badges import status_badge
from dashboard.components.map_components import render_case_map
from dashboard.utils.formatting import data, distancia_km, escapar, numero, texto
from dashboard.utils.status import status_recape_label


def _field(label: str, value: Any) -> None:
    st.markdown(
        f'<div class="detail-grid-label">{escapar(label)}</div><div class="detail-grid-value">{escapar(value)}</div>',
        unsafe_allow_html=True,
    )


def _recape_for_case(record: pd.Series, recapes: pd.DataFrame) -> pd.Series | None:
    if recapes.empty or "id" not in recapes.columns or not record.get("id_recape"):
        return None
    target = str(record.get("id_recape")).strip()
    matches = recapes[recapes["id"].astype(str).str.strip().eq(target)]
    return matches.iloc[0] if not matches.empty else None


def _match_explanation(record: pd.Series) -> str:
    method = texto(record.get("metodo_match"), "SEM_COBERTURA")
    score = pd.to_numeric(pd.Series([record.get("score_confianca")]), errors="coerce").iloc[0]
    distance = distancia_km(record.get("dist_recape_km"))
    if method == "SEM_COBERTURA":
        return "Nenhum recape atingiu os critérios atuais de correspondência. O caso permanece disponível para investigação manual."
    if method == "NOME+COORD":
        score_text = f"similaridade de {score:.0f}%" if pd.notna(score) else "similaridade registrada"
        return f"Correspondência encontrada por nome da via e proximidade geográfica. O ponto está a {distance} do recape associado e teve {score_text}."
    if method == "NOME+CEP":
        score_text = f"similaridade de {score:.0f}%" if pd.notna(score) else "similaridade registrada"
        return f"Correspondência encontrada pelo nome da via com reforço de CEP, com {score_text}."
    if method == "NOME":
        return "Correspondência encontrada apenas pelo nome normalizado da via. A interface a marca para revisão manual por não haver reforço espacial ou de CEP disponível."
    return f"Correspondência encontrada pelo método {method.replace('_', ' ').lower()}. Distância registrada: {distance}."


def _technical_data(record: pd.Series, recape: pd.Series | None) -> dict[str, Any]:
    fields = {
        "nome_normalizado": record.get("rua_notif"),
        "metodo_match": record.get("metodo_match"),
        "score_fuzzy": record.get("score_confianca"),
        "distancia_recape_km": record.get("dist_recape_km"),
        "situacao_origem": record.get("situacao_codigo"),
    }
    if recape is not None:
        fields.update({
            "resolucao_via": recape.get("resolucao_via"),
            "status_rota": recape.get("status_path"),
            "quantidade_segmentos": recape.get("segment_count_path"),
            "comprimento_calculado_m": recape.get("comprimento_path_m"),
            "extensao_esperada_m": recape.get("extensao_m"),
            "desvio_extensao_pct": recape.get("desvio_extensao_pct"),
            "categoria_falha": recape.get("categoria_falha"),
        })
    return {key: value for key, value in fields.items() if texto(value, "")}


def _register_demo_action(case_id: str, action: str) -> None:
    actions = st.session_state.setdefault("demo_case_actions", {})
    actions[case_id] = action
    st.toast(f"Ação de demonstração registrada: {action.replace('_', ' ').title()}")


def render_case_detail(record: pd.Series, recapes: pd.DataFrame) -> None:
    recape = _recape_for_case(record, recapes)
    st.markdown('<div class="detail-panel">', unsafe_allow_html=True)
    header, badge_col = st.columns([5, 1])
    with header:
        st.markdown(f'<h2 class="detail-title">OS {escapar(record.get("numero_os"))}</h2>', unsafe_allow_html=True)
        st.caption(f"{texto(record.get('fonte_notif'))} · {texto(record.get('prefeitura_regional'))} · recebido em {data(record.get('data_recebimento'))}")
    with badge_col:
        status_badge(record.get("situacao_auditoria"))

    notification_col, recape_col = st.columns(2, gap="large")
    with notification_col:
        st.markdown("<div class='section-kicker'>Notificação</div>", unsafe_allow_html=True)
        _field("Endereço", record.get("rua_notif"))
        _field("Número", record.get("numero"))
        _field("CEP", record.get("cep"))
        _field("Coordenadas", f"{numero(record.get('latitude'), 6)}, {numero(record.get('longitude'), 6)}")
        _field("Status de origem", record.get("status_notif"))
    with recape_col:
        st.markdown("<div class='section-kicker'>Recape associado</div>", unsafe_allow_html=True)
        _field("Via", record.get("rua_recape"))
        _field("Trecho", f"{texto(recape.get('de') if recape is not None else '')} até {texto(recape.get('ate') if recape is not None else '')}")
        _field("Status", status_recape_label(record.get("status_recape")))
        _field("Extensão", f"{numero(record.get('extensao_m'))} m")
        _field("Área", f"{numero(record.get('area_m2'))} m²")
        _field("Término", data(record.get("data_termino_recape")))

    st.markdown("<div class='section-kicker'>Explicação do match</div>", unsafe_allow_html=True)
    st.write(_match_explanation(record))
    explanation_left, explanation_right = st.columns(2)
    with explanation_left:
        _field("Método", record.get("metodo_match"))
        _field("Score fuzzy", f"{numero(record.get('score_confianca'))}%")
    with explanation_right:
        _field("Distância", distancia_km(record.get("dist_recape_km")))
        _field("Regra visual", "Revisão manual" if bool(record.get("exige_revisao")) else "Cobertura confirmada")

    st.markdown("<div class='section-kicker'>Mapa do caso</div>", unsafe_allow_html=True)
    render_case_map(record, recapes)

    with st.expander("Dados técnicos e evidências", expanded=False):
        technical = _technical_data(record, recape)
        if technical:
            st.json(technical, expanded=False)
        else:
            st.caption("A base processada não trouxe evidências técnicas adicionais para este caso.")

    st.markdown("<div class='section-kicker'>Ações de demonstração</div>", unsafe_allow_html=True)
    st.caption("Estas ações ficam somente nesta sessão do navegador; não há persistência em banco de dados.")
    action_one, action_two, action_three, action_four = st.columns(4)
    case_id = str(record.get("case_id"))
    with action_one:
        if st.button("Confirmar", key=f"confirm_{case_id}", use_container_width=True):
            _register_demo_action(case_id, "CONFIRMADA")
    with action_two:
        if st.button("Rejeitar", key=f"reject_{case_id}", use_container_width=True):
            _register_demo_action(case_id, "REJEITADA")
    with action_three:
        if st.button("Marcar revisão", key=f"review_{case_id}", use_container_width=True):
            _register_demo_action(case_id, "REVISAO")
    payload = json.dumps(_technical_data(record, recape), ensure_ascii=False, default=str, indent=2)
    with action_four:
        st.download_button("Exportar caso", data=payload.encode("utf-8"), file_name=f"caso_{texto(record.get('numero_os'), 'sem_os')}.json", mime="application/json", use_container_width=True)
    st.code(payload, language="json")
    st.caption("Use o controle de cópia do bloco acima para copiar os dados técnicos do caso.")
    st.markdown("</div>", unsafe_allow_html=True)
