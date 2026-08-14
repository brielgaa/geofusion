from __future__ import annotations

import streamlit as st

from dashboard.components.cards import page_header
from dashboard.components.operational_ui import horizontal_bars, metric_row, section_title, status_badge
from dashboard.services.operational_dashboard import OperationalContext, REFERENCE_DATE
from dashboard.utils.formatting import numero, percentual


def _count(frame, column: str, value: str) -> int:
    return int(frame[column].eq(value).sum()) if column in frame.columns else 0


def render(context: OperationalContext) -> None:
    frame = context.recapes
    page_header(
        "Home",
        "Leitura operacional de recapes, proteção temporal e qualidade geométrica do acervo GeoFusion.",
        "Operational dashboard",
    )
    if frame.empty:
        st.error("Não foi possível carregar recape_clean.csv.")
        return

    total = len(frame)
    official = _count(frame, "quality_code", "OFFICIAL")
    shadow = int(frame["quality_code"].isin(["SHADOW_HIGH", "SHADOW_MEDIUM"]).sum())
    estimated = _count(frame, "quality_code", "ESTIMATED")
    unresolved = _count(frame, "quality_code", "UNRESOLVED")
    active = _count(frame, "protection_status", "ACTIVE")
    expiring = _count(frame, "protection_status", "EXPIRING_SOON")
    unknown = _count(frame, "protection_status", "UNKNOWN_DATE")

    st.caption(f"{numero(total)} recapes indexados · data de referência da proteção: {REFERENCE_DATE.strftime('%d/%m/%Y')} · artefatos separados entre oficial e shadow.")
    metric_row(
        [
            ("Recapes no acervo", numero(total), "base operacional carregada"),
            ("Geometria oficial", percentual(official / total * 100), f"{numero(official)} registros"),
            ("Shadow / estimada", percentual((shadow + estimated) / total * 100), f"{numero(shadow + estimated)} registros"),
            ("Proteção ativa", numero(active), f"{numero(expiring)} expiram em até 30 dias"),
        ],
        primary_index=1,
    )

    section_title("Entrada operacional", "pesquisa reproduzível")
    search_col, button_col = st.columns([6, 1], gap="medium")
    with search_col:
        search = st.text_input("Buscar via, número ou ID do recape", placeholder="Ex.: AV. ENG BILLINGS 3400 · ID 2097 · -23.55,-46.73", label_visibility="collapsed", key="home_search")
    with button_col:
        st.write("")
        if st.button("Consultar", type="primary", use_container_width=True, key="home_search_button"):
            st.session_state["query_prefill"] = search
            st.session_state["pending_navigation"] = "Consulta de Via"
            st.rerun()

    section_title("Cobertura geométrica", "camadas não substituem a fonte oficial")
    coverage = frame["quality_code"].value_counts().rename(index={"OFFICIAL": "Oficial", "SHADOW_HIGH": "Shadow · alta", "SHADOW_MEDIUM": "Shadow · média", "ESTIMATED": "Estimada", "UNRESOLVED": "Não resolvida"}).rename_axis("camada").reset_index(name="recapes")
    coverage["percentual"] = coverage["recapes"].map(lambda value: f"{value / total * 100:.1f}%")
    left, right = st.columns([1.2, 1], gap="large")
    with left:
        horizontal_bars(list(zip(coverage["camada"], coverage["recapes"])), total)
        st.markdown(coverage.to_html(index=False, classes="gf-html-table"), unsafe_allow_html=True)
    with right:
        st.markdown(f'<div class="detail-panel"><div class="section-kicker">Proteção temporal</div><div style="font-size:1.25rem;font-weight:700;color:#E8EEF7">{numero(active)} ativas</div><div style="color:#8FA1B7;margin:6px 0 14px">{numero(expiring)} expiram em breve · {numero(unknown)} sem data utilizável</div><div>{status_badge("ACTIVE")} &nbsp; {status_badge("EXPIRING_SOON")} &nbsp; {status_badge("UNKNOWN_DATE")}</div></div>', unsafe_allow_html=True)

    section_title("Próximas ações", "atalhos de operação")
    cols = st.columns(3)
    cards = [
        ("Resolver uma via", "Use rua + número, coordenadas ou ID quando houver ambiguidade.", "Consulta de Via"),
        ("Revisar proteção", "Priorize recapes que expiram em breve ou têm data desconhecida.", "Proteção de Recapes"),
        ("Inspecionar cobertura", "Veja a separação entre oficial, shadow e estimada no mapa.", "Mapa"),
    ]
    for col, (title, text, page) in zip(cols, cards):
        with col:
            st.markdown(f'<div class="detail-panel gf-action-card"><strong>{title}</strong><p style="color:#8FA1B7;font-size:.82rem;min-height:42px">{text}</p></div>', unsafe_allow_html=True)
            if st.button(f"Abrir · {page}", key=f"home_{page}", use_container_width=True):
                st.session_state["pending_navigation"] = page
                st.rerun()
