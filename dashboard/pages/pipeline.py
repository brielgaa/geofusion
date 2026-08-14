from __future__ import annotations

import streamlit as st

from dashboard.components.cards import page_header
from dashboard.components.operational_ui import info_grid, section_title, status_badge
from dashboard.services.operational_dashboard import OperationalContext
from dashboard.utils.formatting import numero


def _value(payload: dict, *keys: str, default: object = "—") -> object:
    current = payload
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key, default)
    return current


def render(context: OperationalContext) -> None:
    page_header("Pipeline", "Status dos artefatos e arquitetura que alimentam o dashboard operacional.", "Execução e dependências")
    pipeline = context.pipeline_run
    status = str(pipeline.get("status") or "UNKNOWN").upper()
    st.markdown(f"<div class='detail-panel'><div class='section-kicker'>Última execução registrada</div><div style='display:flex;align-items:center;gap:12px'><span style='font-size:1.2rem;font-weight:700;color:#E8EEF7'>{pipeline.get('timestamp') or 'não registrado'}</span>{status_badge(status, label=status)}</div></div>", unsafe_allow_html=True)
    info_grid(
        [
            ("Duração", pipeline.get("duration_seconds") and f"{float(pipeline['duration_seconds']) / 60:.1f} min"),
            ("Notificações", _value(pipeline, "counts", "notificacoes")),
            ("Recapes", len(context.recapes)),
            ("Geometrias oficiais geradas", _value(pipeline, "counts", "geometrias_geradas")),
            ("Falhas de geometria", _value(pipeline, "counts", "falhas_geometria")),
            ("Workers", pipeline.get("workers")),
        ],
        columns=3,
    )

    section_title("Cadeia operacional", "fonte → serviço → tela")
    stages = [
        ("Artefatos oficiais", "recape_clean.csv, notificacoes.csv e cruzamento.csv", "leitura somente"),
        ("Índices operacionais", "Repository · STRtree · normalização GeoSampa", "lazy / cache de recurso"),
        ("Serviços", "StreetLookup · SurfaceLookup · Resurfacing · Protection", "contratos tipados"),
        ("Apresentação", "consulta, proteção, mapa, auditoria e qualidade", "proveniência visível"),
    ]
    for index, (title, description, context_text) in enumerate(stages):
        st.markdown(f"<div class='pipeline-step'><h3>{index + 1:02d} · {title}</h3><p>{description} · {context_text}</p></div>", unsafe_allow_html=True)

    section_title("Artefatos observados", "referências em disco")
    artifacts = [
        ("recape_clean.csv", not context.recapes.empty, "base oficial de recapes"),
        ("geosampa_segmento_logradouro.geojson", bool(context.repository.cache.joinpath("geosampa_segmento_logradouro.geojson").exists()), "lookup de via e faixa numérica"),
        ("route_geometry_quality_shadow.csv", bool(context.repository.shadow_quality_by_id), "qualidade shadow; não promove oficial"),
        ("geometry_validation_shadow.csv", bool(context.repository.validator_by_id), "validação independente"),
        ("consensus_evidence_shadow.csv", bool(context.repository.consensus_by_id), "consenso preservado"),
    ]
    table = "<table class='gf-html-table'><thead><tr><th>Artefato</th><th>Status</th><th>Uso</th></tr></thead><tbody>"
    for name, available, usage in artifacts:
        table += f"<tr><td>{name}</td><td>{status_badge('AVAILABLE' if available else 'UNAVAILABLE', label='disponível' if available else 'ausente')}</td><td>{usage}</td></tr>"
    table += "</tbody></table>"
    st.markdown(table, unsafe_allow_html=True)
