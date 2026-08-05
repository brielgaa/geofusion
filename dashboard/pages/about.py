from __future__ import annotations

import pandas as pd
import streamlit as st

from dashboard.components.cards import page_header
from dashboard.components.empty_states import empty_state
from dashboard.services.data_loader import AppData
from dashboard.utils.formatting import mascarar, numero, percentual, texto


def _real_case(recapes: pd.DataFrame) -> pd.Series | None:
    if recapes.empty or "path" not in recapes.columns:
        return None
    rows = recapes[recapes["path"].notna()]
    return rows.iloc[0] if not rows.empty else None


def render(data: AppData) -> None:
    page_header("Sobre o projeto", "Case técnico de auditoria geoespacial para investigação operacional de recapes em São Paulo.", "Contexto e engenharia")
    first, second = st.columns(2, gap="large")
    with first:
        st.markdown("#### Problema")
        st.write("Equipes operacionais recebem notificações e mantêm recapes em bases fragmentadas. Validar a relação entre ambos exigia consultar fontes separadas, interpretar nomes divergentes, localizar trechos e verificar o estado da obra manualmente.")
        st.markdown("#### Antes")
        st.write("Consulta de bases isoladas → comparação de nomes e endereços → interpretação de trechos → validação de status → consolidação manual.")
    with second:
        st.markdown("#### Solução")
        st.write("O pipeline corrige encoding, normaliza logradouros e cruza notificações com recapes. Para os trechos, usa segmentos reais do GeoSampa, grafo topológico, roteamento, validação de extensão e diagnóstico explícito de falhas.")
        st.markdown("#### Diferencial técnico")
        st.write("STRtree para busca espacial, NetworkX por logradouro, cache persistente com invalidação por assinatura, multiprocessamento, caminho escolhido pela extensão esperada e nenhum recape descartado silenciosamente.")

    st.markdown("#### Caso técnico real, com dados mascarados")
    case = _real_case(data.recapes)
    if case is None:
        empty_state("Nenhum trecho roteado disponível", "A demonstração de caso aparece quando recape_clean.csv contém uma geometria válida.")
    else:
        technical = pd.DataFrame([
            ("Identificador", mascarar(case.get("id"))),
            ("Via normalizada", mascarar(case.get("rua_norm"), manter=7)),
            ("Método de resolução", texto(case.get("resolucao_via"))),
            ("Status da rota", texto(case.get("status_path"))),
            ("Segmentos", numero(case.get("segment_count_path"))),
            ("Extensão esperada", f"{numero(case.get('extensao_m'))} m"),
            ("Extensão calculada", f"{numero(case.get('comprimento_path_m'))} m"),
            ("Desvio", percentual(case.get("desvio_extensao_pct"))),
            ("Resultado", "Geometria final baseada em segmentos reais"),
        ], columns=["Evidência", "Valor"])
        st.dataframe(technical, use_container_width=True, hide_index=True)
        st.caption("Identificadores e nomes de via são parcialmente mascarados nesta página. A auditoria operacional continua disponível localmente para usuários autorizados.")

    third, fourth = st.columns(2, gap="large")
    with third:
        st.markdown("#### Limitações reais")
        st.write("A qualidade depende das fontes; há nomes divergentes, coordenadas ausentes e inconsistências de trecho. A interface é Streamlit local, não possui persistência multiusuário e usa CSVs como camada de armazenamento.")
    with fourth:
        st.markdown("#### Próximos passos")
        st.write("PostgreSQL/PostGIS, API FastAPI, dbt, orquestração, autenticação, auditoria multiusuário, histórico de runs e testes de regressão geoespacial.")
