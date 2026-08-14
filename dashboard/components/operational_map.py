"""PyDeck rendering for operational recape geometry."""
from __future__ import annotations

from typing import Any

import pandas as pd
import pydeck as pdk
import streamlit as st
from pyproj import Transformer
from shapely import wkt
from shapely.ops import transform

from dashboard.components.operational_ui import empty_panel
from dashboard.utils.formatting import escapar


WGS84_FROM_METRIC = Transformer.from_crs("EPSG:31983", "EPSG:4326", always_xy=True)
QUALITY_COLORS = {
    "OFFICIAL": [117, 168, 255, 220],
    "SHADOW_HIGH": [105, 211, 162, 220],
    "SHADOW_MEDIUM": [240, 197, 108, 220],
    "ESTIMATED": [180, 145, 226, 210],
    "UNRESOLVED": [241, 124, 120, 180],
}
PROTECTION_COLORS = {
    "ACTIVE": [105, 211, 162, 230],
    "EXPIRING_SOON": [240, 197, 108, 230],
    "EXPIRED": [143, 161, 183, 170],
    "UNKNOWN_DATE": [241, 124, 120, 230],
}


def _segment_coordinates(value: Any) -> list[list[float]] | None:
    if isinstance(value, list) and len(value) >= 2:
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        geometry = wkt.loads(value)
        geometry = transform(WGS84_FROM_METRIC.transform, geometry)
        return [[float(x), float(y)] for x, y in geometry.coords] if geometry.geom_type == "LineString" else None
    except Exception:
        return None


def _view_state(frame: pd.DataFrame) -> pdk.ViewState:
    points: list[tuple[float, float]] = []
    for path in frame.get("map_path", []):
        if isinstance(path, list):
            points.extend((float(pair[1]), float(pair[0])) for pair in path if len(pair) >= 2)
    if not points:
        return pdk.ViewState(latitude=-23.55, longitude=-46.64, zoom=9.8, pitch=0)
    return pdk.ViewState(
        latitude=sum(point[0] for point in points) / len(points),
        longitude=sum(point[1] for point in points) / len(points),
        zoom=10.2 if len(frame) < 200 else 9.5,
        pitch=0,
    )


def _tooltip(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    result["tooltip_html"] = [
        (
            f"<b>{escapar(row.get('street_display', '—'))}</b><br/>"
            f"Recape: {escapar(row.get('record_id', '—'))}<br/>"
            f"Qualidade: {escapar(row.get('quality_label', '—'))}<br/>"
            f"Proteção: {escapar(row.get('protection_status', '—'))}<br/>"
            f"Superfície: {escapar(row.get('surface_display', '—'))}"
        )
        for _, row in result.iterrows()
    ]
    return result


def render_recape_map(
    frame: pd.DataFrame,
    *,
    show_quality: set[str] | None = None,
    protection_overlay: bool = False,
    selected_id: str | None = None,
    key: str = "operational_map",
) -> None:
    if frame.empty:
        empty_panel("Mapa sem geometria", "Nenhum recape do recorte possui geometria disponível para visualização.")
        return
    visible = frame[frame["map_path"].map(lambda value: isinstance(value, list) and len(value) >= 2)].copy()
    if show_quality is not None:
        visible = visible[visible["quality_code"].isin(show_quality)]
    if visible.empty:
        empty_panel("Nenhum elemento nas camadas ativas", "Ative pelo menos uma camada de geometria ou ajuste os filtros.")
        return
    visible = _tooltip(visible)
    if selected_id:
        visible["line_width"] = visible["record_id"].astype(str).eq(str(selected_id)).map({True: 8, False: 3})
    else:
        visible["line_width"] = 3
    visible["line_color"] = [
        PROTECTION_COLORS.get(str(status), QUALITY_COLORS.get(str(quality), [117, 168, 255, 210]))
        if protection_overlay
        else QUALITY_COLORS.get(str(quality), [117, 168, 255, 210])
        for status, quality in zip(visible["protection_status"], visible["quality_code"])
    ]
    layer = pdk.Layer(
        "PathLayer",
        data=visible,
        get_path="map_path",
        get_color="line_color",
        get_width="line_width",
        width_min_pixels=2,
        pickable=True,
        auto_highlight=True,
    )
    deck = pdk.Deck(
        map_provider="carto",
        map_style="dark",
        initial_view_state=_view_state(visible),
        layers=[layer],
        tooltip={"html": "{tooltip_html}", "style": {"backgroundColor": "#101827", "color": "#E8EEF7", "fontSize": "12px"}},
    )
    st.pydeck_chart(deck, use_container_width=True)
    st.caption(f"{len(visible):,} geometrias visíveis · passe o cursor para inspecionar o recape.")


def render_result_map(geometry_wkt: str | None, *, latitude: float | None = None, longitude: float | None = None, key: str = "result_map") -> None:
    path = _segment_coordinates(geometry_wkt)
    if not path and latitude is None:
        empty_panel("Geometria indisponível", "A consulta não retornou uma geometria exibível.")
        return
    layers: list[pdk.Layer] = []
    if path:
        frame = pd.DataFrame([{ "map_path": path, "line_color": QUALITY_COLORS["OFFICIAL"], "line_width": 8 }])
        layers.append(pdk.Layer("PathLayer", data=frame, get_path="map_path", get_color="line_color", get_width="line_width", width_min_pixels=4))
        center_lat = sum(pair[1] for pair in path) / len(path)
        center_lon = sum(pair[0] for pair in path) / len(path)
    else:
        center_lat, center_lon = float(latitude), float(longitude)
    if latitude is not None and longitude is not None:
        point = pd.DataFrame([{ "latitude": float(latitude), "longitude": float(longitude) }])
        layers.append(pdk.Layer("ScatterplotLayer", data=point, get_position="[longitude, latitude]", get_fill_color=[241, 124, 120, 230], get_radius=30, radius_min_pixels=6))
    st.pydeck_chart(
        pdk.Deck(map_provider="carto", map_style="dark", initial_view_state=pdk.ViewState(latitude=center_lat, longitude=center_lon, zoom=15 if path else 13, pitch=0), layers=layers),
        use_container_width=True,
    )
