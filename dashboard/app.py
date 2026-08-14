"""Entry point for the GeoFusion operational dashboard."""
from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from dashboard.pages import about, audit, data_quality, map_view, overview, pipeline, protection, query
from dashboard.services.operational_dashboard import REFERENCE_DATE, OperationalContext, dataset_signature, load_operational_context
from dashboard.styles.theme import inject_theme


st.set_page_config(
    page_title="GeoFusion · Operational Dashboard",
    page_icon="◈",
    layout="wide",
    initial_sidebar_state="expanded",
)


PAGES = {
    "Home": overview.render,
    "Consulta de Via": query.render,
    "Proteção de Recapes": protection.render,
    "Mapa": map_view.render,
    "Auditoria": audit.render,
    "Qualidade": data_quality.render,
    "Pipeline": pipeline.render,
    "Sobre": about.render,
}


def render_topbar(context: OperationalContext) -> str:
    st.markdown(
        f"""
        <div class="gf-topbar">
          <div class="gf-brand"><span class="gf-brand-mark"></span>GeoFusion <span style="color:#8FA1B7;font-weight:500">/ operacional</span></div>
          <div class="gf-topbar-note">Dados operacionais · referência {REFERENCE_DATE.strftime('%d/%m/%Y')}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    with st.sidebar:
        st.markdown("### GeoFusion")
        st.caption("Operational Dashboard")
        st.divider()
        pending = st.session_state.pop("pending_navigation", None)
        if pending in PAGES:
            st.session_state["main_navigation"] = pending
        page = st.radio(
            "Navegação principal",
            list(PAGES),
            key="main_navigation",
            label_visibility="collapsed",
        )
        st.divider()
        st.caption(f"{len(context.recapes):,} recapes indexados")
    if context.errors:
        st.warning("A aplicação iniciou com dados parciais. " + " · ".join(context.errors))
    return page


def main() -> None:
    inject_theme()
    context = load_operational_context(str(PROJECT_DIR), dataset_signature(PROJECT_DIR))
    selected_page = render_topbar(context)
    PAGES[selected_page](context)


if __name__ == "__main__":
    main()
