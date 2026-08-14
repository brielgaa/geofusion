from __future__ import annotations

import streamlit as st

from dashboard.components.cards import page_header
from dashboard.components.operational_ui import section_title
from dashboard.services.operational_dashboard import OperationalContext, REFERENCE_DATE


def render(context: OperationalContext) -> None:
    page_header("Sobre", "Contexto, limites de uso e contrato operacional do GeoFusion.", "Produto e método")
    left, right = st.columns([1.2, 1], gap="large")
    with left:
        section_title("O que é o GeoFusion", "primeira camada de produto")
        st.markdown("""
        <div class="detail-panel">
          <p>O GeoFusion transforma artefatos geoespaciais e de recape em uma ferramenta interna de decisão: localizar uma via, entender a superfície, consultar o recape mais recente, acompanhar a proteção e auditar a evidência.</p>
          <p style="color:#8FA1B7">A interface é operacional. Ela não altera o ETL, não promove geometrias shadow e não conclui automaticamente que uma notificação é violação.</p>
        </div>
        """, unsafe_allow_html=True)
    with right:
        section_title("Referência desta leitura", "transparência")
        st.markdown(f"<div class='detail-panel'><div class='gf-detail-label'>Data de referência</div><div class='gf-detail-value'>{REFERENCE_DATE.strftime('%d/%m/%Y')}</div><div class='gf-detail-label'>Registros</div><div class='gf-detail-value'>{len(context.recapes):,} recapes</div><div class='gf-detail-label'>Base</div><div class='gf-detail-value'>recape_clean.csv + índices GeoSampa</div></div>", unsafe_allow_html=True)

    section_title("Princípios de uso", "guardrails explícitos")
    principles = [
        ("Oficial primeiro", "Geometria oficial permanece a referência primária. Shadow e estimada aparecem como camadas separadas."),
        ("Ambiguidade visível", "Uma via com múltiplos segmentos retorna alternativas; nenhuma escolha é feita silenciosamente."),
        ("Tempo explícito", "Proteção usa data de término/conclusão disponível com uma data de referência explícita."),
        ("Atenção não é violação", "As notificações associadas usam NEEDS_ATTENTION como estado operacional de revisão."),
        ("Pesquisa arquivada", "A pesquisa experimental de image geometry permanece em arquivo e não é integrada ao produto operacional."),
    ]
    st.markdown("<div class='gf-principles'>", unsafe_allow_html=True)
    for title, description in principles:
        st.markdown(f"<div class='gf-principle'><strong>{title}</strong><span>{description}</span></div>", unsafe_allow_html=True)
    st.markdown("</div>", unsafe_allow_html=True)

    section_title("Limites atuais", "honestidade operacional")
    st.info("O lookup por número usa faixas de numeração GeoSampa, não um ponto predial exato. A superfície é conhecida no nível do registro de recape, não como join universal por segmento. A data de notificação não é data de execução.")
