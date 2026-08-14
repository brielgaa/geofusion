"""Mapas Pydeck para investigação de notificações e trechos reais."""
from __future__ import annotations

import json
import re
from typing import Any

import pandas as pd
import pydeck as pdk
import streamlit as st

from dashboard.components.empty_states import empty_state
from dashboard.utils.formatting import data, distancia_km, texto
from dashboard.utils.status import CONCLUIDO, EM_ANDAMENTO, PLANEJADO, REVISAO, SEM_COBERTURA, STATUS_META, status_label, status_recape_label


RECAPE_LAYER_META = {
    "CONCLUIDO_RECENTE": ("Concluídos recentes", [22, 121, 74, 220]),
    "CONCLUIDO_ANTIGO": ("Concluídos antigos", [152, 162, 179, 180]),
    "PLANEJADO": ("Planejados", [210, 139, 0, 220]),
    "EM_EXECUCAO": ("Em execução", [31, 99, 194, 220]),
}
POINT_COLORS = {
    "SGZ_CONVIAS": [37, 99, 235, 190],
    "SGZ_156": [13, 148, 136, 190],
    SEM_COBERTURA: [194, 58, 48, 220],
    REVISAO: [110, 59, 183, 220],
}


def _path_coordinates(value: Any) -> list[list[float]] | None:
    if isinstance(value, list):
        return value if len(value) >= 2 else None
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict) and parsed.get("type") == "LineString":
            parsed = parsed.get("coordinates")
        if isinstance(parsed, list) and len(parsed) >= 2:
            return parsed
    except (TypeError, ValueError):
        pass
    if raw.upper().startswith("LINESTRING"):
        numbers = re.findall(r"(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)", raw)
        coordinates = [[float(lon), float(lat)] for lon, lat in numbers]
        return coordinates if len(coordinates) >= 2 else None
    return None


def _recape_layer_code(status: Any, completed_at: Any) -> str | None:
    normalized = str(status or "").strip().upper()
    if normalized.startswith("CONCLUIDO"):
        date_value = pd.to_datetime(completed_at, errors="coerce")
        if pd.notna(date_value) and (pd.Timestamp.now().normalize() - date_value.normalize()).days <= 365:
            return "CONCLUIDO_RECENTE"
        return "CONCLUIDO_ANTIGO"
    if normalized in {"PLANEJADO", "CONTRATADO", "APENAS_INFRA", "A_CONTRATAR_CURTO_PRAZO"}:
        return "PLANEJADO"
    if normalized in {"EM_EXECUCAO", "EM_ANDAMENTO", "EXECUCAO"}:
        return "EM_EXECUCAO"
    return None


def _tooltip_lines(items: list[tuple[str, Any]]) -> str:
    lines = [f"<b>{label}</b>: {texto(value, '')}" for label, value in items if texto(value, "")]
    return "<br/>".join(lines)


@st.cache_data(show_spinner=False)
def prepare_recape_paths(recapes: pd.DataFrame) -> pd.DataFrame:
    if recapes.empty:
        return recapes.copy()
    result = recapes.copy()
    if "path" not in result.columns:
        return result.iloc[0:0].copy()
    result["path_coords"] = result["path"].map(_path_coordinates)
    result = result[result["path_coords"].notna()].copy()
    if result.empty:
        return result
    statuses = result["status"] if "status" in result.columns else pd.Series("", index=result.index)
    finished_dates = result["data_termino"] if "data_termino" in result.columns else pd.Series(None, index=result.index)
    result["map_layer"] = [
        _recape_layer_code(status, finished)
        for status, finished in zip(statuses, finished_dates)
    ]
    result = result[result["map_layer"].notna()].copy()
    result["tooltip_html"] = [
        _tooltip_lines([
            ("Recape", row.get("rua_raw") or row.get("via")),
            ("Trecho", f"{texto(row.get('de'), '')} até {texto(row.get('ate'), '')}".strip(" até")),
            ("Status", status_recape_label(row.get("status"))),
            ("Extensão", f"{pd.to_numeric(pd.Series([row.get('extensao_m')]), errors='coerce').iloc[0]:.0f} m" if pd.notna(pd.to_numeric(pd.Series([row.get('extensao_m')]), errors='coerce').iloc[0]) else ""),
            ("Subprefeitura", row.get("subprefeitura")),
        ])
        for _, row in result.iterrows()
    ]
    return result


def prepare_notification_points(records: pd.DataFrame) -> pd.DataFrame:
    required = {"latitude", "longitude"}
    if records.empty or not required.issubset(records.columns):
        return pd.DataFrame()
    result = records.copy()
    result["latitude"] = pd.to_numeric(result["latitude"], errors="coerce")
    result["longitude"] = pd.to_numeric(result["longitude"], errors="coerce")
    result = result[
        result["latitude"].between(-24.1, -23.3) & result["longitude"].between(-47.0, -46.3)
    ].copy()
    if result.empty:
        return result
    result["tooltip_html"] = [
        _tooltip_lines([
            ("OS", row.get("numero_os")),
            ("Fonte", row.get("fonte_notif")),
            ("Endereço", row.get("rua_notif")),
            ("Regional", row.get("prefeitura_regional")),
            ("Situação", status_label(row.get("situacao_auditoria", EM_ANDAMENTO))),
            ("Recape", row.get("rua_recape")),
            ("Método", row.get("metodo_match")),
            ("Confiança", f"{row.get('score_confianca', 0):.0f}%" if pd.notna(row.get("score_confianca")) else ""),
            ("Distância", distancia_km(row.get("dist_recape_km"))),
            ("Recebimento", data(row.get("data_recebimento"))),
        ])
        for _, row in result.iterrows()
    ]
    return result


def _sample(points: pd.DataFrame, maximum: int = 15000) -> tuple[pd.DataFrame, int]:
    if len(points) <= maximum:
        return points, len(points)
    return points.sample(n=maximum, random_state=42), len(points)


def _view_state(points: pd.DataFrame) -> pdk.ViewState:
    if points.empty:
        return pdk.ViewState(latitude=-23.55, longitude=-46.64, zoom=9.5, pitch=0)
    return pdk.ViewState(
        latitude=float(points["latitude"].mean()),
        longitude=float(points["longitude"].mean()),
        zoom=10.2 if len(points) < 800 else 9.5,
        pitch=0,
    )


def render_case_map(record: pd.Series, recapes: pd.DataFrame) -> None:
    points = prepare_notification_points(pd.DataFrame([record]))
    if points.empty:
        empty_state("Mapa indisponível", "Este caso não possui coordenadas válidas da notificação.")
        return
    layers: list[pdk.Layer] = []
    recape_paths = prepare_recape_paths(recapes)
    recape_id = str(record.get("id_recape") or "")
    if recape_id and "id" in recape_paths.columns:
        selected = recape_paths[recape_paths["id"].astype(str) == recape_id]
        if not selected.empty:
            layers.append(pdk.Layer("PathLayer", data=selected, get_path="path_coords", get_color=[31, 99, 194, 230], get_width=6, width_min_pixels=3, pickable=True))
    layers.append(pdk.Layer("ScatterplotLayer", data=points, get_position="[longitude, latitude]", get_fill_color=POINT_COLORS.get(record.get("situacao_auditoria"), [37, 99, 235, 220]), get_line_color=[255, 255, 255, 255], get_radius=42, radius_min_pixels=6, radius_max_pixels=12, stroked=True, line_width_min_pixels=2, pickable=True))
    st.pydeck_chart(
        pdk.Deck(
            map_provider="carto", map_style="dark", initial_view_state=_view_state(points), layers=layers,
            tooltip={"html": "{tooltip_html}", "style": {"backgroundColor": "#111827", "color": "#E7EEF8", "fontSize": "12px"}},
        ),
        use_container_width=True,
    )


def render_operational_map(records: pd.DataFrame, recapes: pd.DataFrame, key_prefix: str = "map") -> str | None:
    """Renderiza mapa e painel de camadas; retorna o case_id selecionado."""
    points = prepare_notification_points(records)
    recape_paths = prepare_recape_paths(recapes)
    if points.empty and recape_paths.empty:
        empty_state("Sem elementos geográficos", "O recorte atual não possui coordenadas válidas ou trechos roteados.")
        return None

    map_column, panel = st.columns([7, 3], gap="large")
    selected_case_id: str | None = None
    with panel:
        st.markdown("<div class='section-kicker'>Camadas e investigação</div>", unsafe_allow_html=True)
        show_convias = st.checkbox("Notificações — fonte A", value=True, key=f"{key_prefix}_convias")
        show_156 = st.checkbox("Notificações — fonte B", value=True, key=f"{key_prefix}_156")
        show_recent = st.checkbox("Recapes concluídos recentes", value=True, key=f"{key_prefix}_recent")
        show_old = st.checkbox("Recapes concluídos antigos", value=False, key=f"{key_prefix}_old")
        show_planned = st.checkbox("Recapes planejados", value=True, key=f"{key_prefix}_planned")
        show_running = st.checkbox("Recapes em execução", value=True, key=f"{key_prefix}_running")
        show_uncovered = st.checkbox("Sem cobertura", value=True, key=f"{key_prefix}_uncovered")
        show_review = st.checkbox("Baixa confiança ou revisão", value=True, key=f"{key_prefix}_review")
        aggregate = st.checkbox("Agregação de pontos", value=False, key=f"{key_prefix}_aggregate", help="Exibe densidade quando há muitos pontos e desativa a inspeção individual.")

        cases = points.head(500)
        if not cases.empty:
            case_lookup = {
                row.case_id: f"OS {texto(row.numero_os)} · {texto(row.rua_notif)}"
                for row in cases.itertuples(index=False)
            }
            choices = [""] + list(case_lookup)
            chosen = st.selectbox("Abrir caso no painel", choices, key=f"{key_prefix}_case", format_func=lambda value: "Selecionar caso" if not value else case_lookup[value])
            selected_case_id = chosen or None
        st.caption(f"{len(points):,} notificações com coordenadas válidas · {len(recape_paths):,} trechos roteados disponíveis.")
        if aggregate:
            st.caption("Agregação ativa: use os filtros para reduzir o recorte e voltar à inspeção individual.")

    source_points = points.iloc[0:0].copy()
    if show_convias and "fonte_notif" in points.columns:
        source_points = pd.concat([source_points, points[points["fonte_notif"].eq("SGZ_CONVIAS")]])
    if show_156 and "fonte_notif" in points.columns:
        source_points = pd.concat([source_points, points[points["fonte_notif"].eq("SGZ_156")]])
    if source_points.empty and "fonte_notif" not in points.columns:
        source_points = points
    shown_points, total_points = _sample(source_points)
    if not shown_points.empty:
        shown_points = shown_points.copy()
        source_series = shown_points["fonte_notif"] if "fonte_notif" in shown_points.columns else pd.Series("", index=shown_points.index)
        shown_points["point_color"] = [POINT_COLORS.get(source, [37, 99, 235, 190]) for source in source_series]
    layers: list[pdk.Layer] = []
    layer_flags = {
        "CONCLUIDO_RECENTE": show_recent,
        "CONCLUIDO_ANTIGO": show_old,
        "PLANEJADO": show_planned,
        "EM_EXECUCAO": show_running,
    }
    for code, (_, color) in RECAPE_LAYER_META.items():
        if layer_flags[code] and not recape_paths.empty:
            subset = recape_paths[recape_paths["map_layer"].eq(code)]
            if not subset.empty:
                layers.append(pdk.Layer("PathLayer", data=subset, get_path="path_coords", get_color=color, get_width=5, width_min_pixels=2, pickable=True, auto_highlight=True))
    if aggregate and not shown_points.empty:
        layers.append(pdk.Layer("HexagonLayer", data=shown_points, get_position="[longitude, latitude]", radius=250, elevation_scale=4, elevation_range=[0, 700], extruded=True, coverage=0.88, pickable=True))
    else:
        standard = shown_points[~shown_points["situacao_auditoria"].isin([SEM_COBERTURA, REVISAO])]
        if not standard.empty:
            layers.append(pdk.Layer("ScatterplotLayer", data=standard, get_position="[longitude, latitude]", get_fill_color="point_color", get_radius=24, radius_min_pixels=3, radius_max_pixels=7, pickable=True, auto_highlight=True))
        if show_uncovered:
            uncovered = shown_points[shown_points["situacao_auditoria"].eq(SEM_COBERTURA)]
            if not uncovered.empty:
                layers.append(pdk.Layer("ScatterplotLayer", data=uncovered, get_position="[longitude, latitude]", get_fill_color=POINT_COLORS[SEM_COBERTURA], get_radius=30, radius_min_pixels=4, radius_max_pixels=8, pickable=True, auto_highlight=True))
        if show_review:
            review = shown_points[shown_points["situacao_auditoria"].eq(REVISAO)]
            if not review.empty:
                layers.append(pdk.Layer("ScatterplotLayer", data=review, get_position="[longitude, latitude]", get_fill_color=POINT_COLORS[REVISAO], get_radius=30, radius_min_pixels=4, radius_max_pixels=8, pickable=True, auto_highlight=True))

    with map_column:
        st.pydeck_chart(
            pdk.Deck(
                map_provider="carto", map_style="dark", initial_view_state=_view_state(shown_points), layers=layers,
                tooltip={"html": "{tooltip_html}", "style": {"backgroundColor": "#111827", "color": "#E7EEF8", "fontSize": "12px"}},
            ),
            use_container_width=True,
        )
        if total_points > len(shown_points):
            st.caption(f"Exibindo amostra determinística de {len(shown_points):,} de {total_points:,} pontos. KPIs, filtros e tabelas usam todos os registros.")
        else:
            st.caption(f"{len(shown_points):,} elementos de notificação visíveis no recorte atual.")
    return selected_case_id
