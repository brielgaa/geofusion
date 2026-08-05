from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

from dashboard.components.cards import metric_card, page_header
from dashboard.services.data_loader import AppData
from dashboard.services.metrics import coverage_metrics, prepare_audit_records
from dashboard.utils.formatting import numero, percentual, texto


PIPELINE_STEPS = [
    ("1. Ingestão", "Lê SGZ 156, SGZ Convias e a base de recapes.", "CSVs e XLSX brutos", "DataFrames tipados", "pandas", "Arquivo ausente, schema inesperado ou bloqueio no Windows."),
    ("2. Correção de encoding", "Recupera textos com mojibake antes da normalização.", "Campos textuais brutos", "Texto UTF-8 legível", "unicodedata", "Caracteres não recuperáveis ou origem inconsistente."),
    ("3. Normalização", "Padroniza CEPs, datas, coordenadas e nomes de logradouro.", "Dados de origem", "Chaves de comparação", "pandas + regras puras", "Nome vazio, coordenada fora da área ou data inválida."),
    ("4. Carregamento GeoSampa", "Obtém ou reutiliza segmentos reais de logradouro.", "WFS / GeoJSON em cache", "GeoDataFrame projetado", "GeoPandas + requests", "Indisponibilidade de WFS ou GeoJSON corrompido."),
    ("5. Grafo topológico", "Constrói ou lê o índice de segmentos, componentes e STRtree.", "Segmentos reais", "RoadGraph persistente", "NetworkX + STRtree", "Assinatura do cache inválida ou geometrias inválidas."),
    ("6. Roteamento", "Encontra trechos entre De e Até, escolhendo caminho pela extensão esperada.", "Via, interseções e grafo", "Path GeoJSON", "Shapely + multiprocessamento", "Sem via, interseção ou caminho conectado."),
    ("7. Matching", "Cruza notificações e recapes por nome, CEP e coordenadas quando disponíveis.", "Notificações + recapes", "cruzamento.csv", "RapidFuzz + índice espacial", "Sem cobertura ou baixa confiança."),
    ("8. Diagnóstico", "Classifica explicitamente falhas de geometria e mantém evidências.", "Resultado de rota", "Relatório e CSV de falhas", "Regras de diagnóstico", "Categoria técnica não esperada."),
    ("9. Persistência", "Grava CSVs processados, cache e relatório de cobertura.", "DataFrames e caches", "Artefatos locais", "CSV + JSON + pickle", "Permissão, disco ou arquivo aberto."),
    ("10. Dashboard", "Lê artefatos locais e permite investigação sem alterar o ETL.", "Dados processados", "Interface operacional", "Streamlit + Plotly + Pydeck", "Artefato ausente ou coluna indisponível."),
]


def _run_value(run: dict, key: str, fallback: str = "Não disponível") -> str:
    value = run.get(key)
    return texto(value, fallback)


def render(data: AppData) -> None:
    records = prepare_audit_records(data.cruzamento)
    metrics = coverage_metrics(records)
    page_header("Pipeline", "Estado atual dos artefatos, etapas de engenharia e diagnóstico geoespacial.", "Observabilidade local")
    run = data.pipeline_run
    report = data.coverage_report
    if run:
        st.caption(f"Última execução registrada: {_run_value(run, 'timestamp')} · status: {_run_value(run, 'status')}")
    else:
        st.caption("Não há histórico de execuções. Os indicadores abaixo refletem somente o estado atual dos arquivos processados.")

    summary = st.columns(4)
    cards = [
        ("Notificações", numero(len(data.notificacoes) or len(records)), "Arquivos SGZ unificados"),
        ("Recapes", numero(len(data.recapes)), f"{numero(report.get('com_geometria', 0))} com geometria"),
        ("Cobertura geométrica", percentual(report.get("cobertura_pct", 0)), "Trechos roteados no GeoSampa"),
        ("Cobertura de match", percentual(metrics["found_pct"]), "Notificações com recape associado"),
    ]
    for column, (label, value, context) in zip(summary, cards):
        with column:
            metric_card(label, value, context, help_text=context)

    diagram, state = st.columns([1.2, 1], gap="large")
    with diagram:
        st.markdown("<div class='section-kicker'>Arquitetura</div>", unsafe_allow_html=True)
        st.graphviz_chart(
            """
            digraph pipeline {
              rankdir=LR; bgcolor="transparent"; node [shape=box style="rounded,filled" fillcolor="#FFFFFF" color="#D0D5DD" fontname="Arial" fontsize=10]; edge [color="#98A2B3"];
              sgz156 [label="SGZ 156"]; convias [label="Convias"]; recape [label="Recapes"]; ingest [label="Ingestão"]; norm [label="Normalização"]; match [label="Matching"]; audit [label="Auditoria"];
              geosampa [label="Grafo GeoSampa" fillcolor="#EFF6FF"]; cache [label="Cache GeoJSON + grafo" fillcolor="#F7F8FA"];
              sgz156 -> ingest; convias -> ingest; recape -> ingest; ingest -> norm -> match -> audit; geosampa -> match; cache -> geosampa;
            }
            """,
            use_container_width=True,
        )
    with state:
        st.markdown("<div class='section-kicker'>Estado técnico atual</div>", unsafe_allow_html=True)
        cache_file = data.processed_dir.parent / "cache" / "geosampa_road_graph.pkl"
        state_rows = pd.DataFrame([
            {"Sinal": "Cache do grafo", "Estado": "Disponível" if cache_file.exists() else "Não encontrado"},
            {"Sinal": "Matches fuzzy", "Estado": numero(report.get("fuzzy", 0))},
            {"Sinal": "Falhas detalhadas", "Estado": numero(report.get("falhas_detalhadas", len(data.falhas)))},
            {"Sinal": "Workers", "Estado": _run_value(run, "workers", "Não registrado")},
        ])
        st.dataframe(state_rows, use_container_width=True, hide_index=True)

    st.markdown("<div class='section-kicker'>Etapas do pipeline</div>", unsafe_allow_html=True)
    for title, description, input_value, output_value, technology, failures in PIPELINE_STEPS:
        st.markdown(
            f"<div class='pipeline-step'><h3>{title}</h3><p>{description}<br/><b>Entrada:</b> {input_value} · <b>Saída:</b> {output_value}<br/><b>Tecnologia:</b> {technology} · <b>Falhas tratadas:</b> {failures}</p></div>",
            unsafe_allow_html=True,
        )

    st.markdown("<div class='section-kicker'>Arquivos processados</div>", unsafe_allow_html=True)
    files = [
        ("cruzamento.csv", len(data.cruzamento), "Resultado do matching de notificações"),
        ("notificacoes.csv", len(data.notificacoes), "Notificações unificadas"),
        ("recape_clean.csv", len(data.recapes), "Recapes enriquecidos e roteados"),
        ("recapes_sem_cobertura.csv", len(data.falhas), "Falhas técnicas classificadas"),
    ]
    st.dataframe(pd.DataFrame(files, columns=["Arquivo", "Registros", "Conteúdo"]), use_container_width=True, hide_index=True)
