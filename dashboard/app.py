"""Ponto de entrada do dashboard de auditoria Obras SP.

Execução compatível com:
    streamlit run dashboard/app.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from dashboard.pages import about, audit, data_quality, map_view, overview, pipeline, showcase
from dashboard.services.data_loader import AppData, load_app_data
from dashboard.styles.theme import inject_theme


st.set_page_config(
    page_title="Obras SP · Auditoria geoespacial",
    layout="wide",
    initial_sidebar_state="collapsed",
)


@st.cache_data(show_spinner="Carregando artefatos processados…")
def load_dashboard_data(project_dir: str) -> AppData:
    return load_app_data(Path(project_dir))


PAGES = {
    "Visão geral": overview.render,
    "Showcase": showcase.render,
    "Auditoria": audit.render,
    "Mapa": map_view.render,
    "Pipeline": pipeline.render,
    "Qualidade dos dados": data_quality.render,
    "Sobre o projeto": about.render,
}


def render_topbar(data: AppData) -> str:
    st.markdown(
        """
        <div class="obras-topbar">
          <div class="obras-brand"><span class="obras-brand-mark"></span>Obras SP</div>
          <div class="obras-nav-note">Auditoria geoespacial de notificações e recapeamentos</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    page = st.radio(
        "Navegação principal",
        list(PAGES),
        horizontal=True,
        label_visibility="collapsed",
        key="main_navigation",
    )
    st.divider()
    if data.errors:
        st.warning("A aplicação iniciou com dados parciais. " + " · ".join(data.errors))
    return page


def main() -> None:
    inject_theme()
    data = load_dashboard_data(str(PROJECT_DIR))
    selected_page = render_topbar(data)
    PAGES[selected_page](data)


if __name__ == "__main__":
    main()
