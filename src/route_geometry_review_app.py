"""Interface Streamlit para validação humana de geometrias shadow.

O app não inicializa o RoadGraph e não executa auditoria, heurística ou ETL.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

SRC_DIR = Path(__file__).resolve().parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

try:
    import pydeck as pdk
except ImportError:  # pragma: no cover - mensagem exibida em instalação mínima
    pdk = None

try:
    from route_geometry_review import (
        CONFIDENCE_LABELS, DECISIONS, DEFAULT_APPROVED_PATH, DEFAULT_AUDIT_PATH,
        DEFAULT_QUALITY_REPORT_PATH, DEFAULT_QUALITY_SHADOW_PATH, DEFAULT_RECAP_PATH,
        DEFAULT_REJECTED_PATH, DEFAULT_REPORT_PATH, DEFAULT_REVIEW_PATH, DEFAULT_SAME_TRANSVERSAL_PATH,
        TARGET_CONFIDENCES, approve_cases_in_bulk, alternatives_table, batch_approval_preview,
        export_approved, export_rejected, filter_cases, geometry_from_wkt, load_reviews,
        load_review_data, merge_reviews, parse_alternatives, parse_bool, review_metrics, save_decision,
        stratified_sample, text_value, write_report,
    )
except ImportError:  # pragma: no cover - permite ``python -m src...``
    from .route_geometry_review import (
        CONFIDENCE_LABELS, DECISIONS, DEFAULT_APPROVED_PATH, DEFAULT_AUDIT_PATH,
        DEFAULT_QUALITY_REPORT_PATH, DEFAULT_QUALITY_SHADOW_PATH, DEFAULT_RECAP_PATH,
        DEFAULT_REJECTED_PATH, DEFAULT_REPORT_PATH, DEFAULT_REVIEW_PATH, DEFAULT_SAME_TRANSVERSAL_PATH,
        TARGET_CONFIDENCES, approve_cases_in_bulk, alternatives_table, batch_approval_preview,
        export_approved, export_rejected, filter_cases, geometry_from_wkt, load_reviews,
        load_review_data, merge_reviews, parse_alternatives, parse_bool, review_metrics, save_decision,
        stratified_sample, text_value, write_report,
    )


st.set_page_config(page_title="Revisão de geometrias shadow", page_icon="🧭", layout="wide")


def _file_signature(path: Path) -> tuple[int, int]:
    try:
        stat = path.stat()
        return stat.st_mtime_ns, stat.st_size
    except FileNotFoundError:
        return 0, 0


@st.cache_data(show_spinner=False)
def cached_cases(
    quality_path: str, quality_signature: tuple[int, int], audit_path: str,
    audit_signature: tuple[int, int], recape_path: str, recape_signature: tuple[int, int],
    report_path: str, report_signature: tuple[int, int], review_path: str, review_signature: tuple[int, int],
    same_path: str, same_signature: tuple[int, int],
) -> pd.DataFrame:
    cases = load_review_data(quality_path, audit_path, recape_path, report_path, same_path)
    return merge_reviews(cases, load_reviews(review_path))


def get_cases() -> pd.DataFrame:
    return cached_cases(
        str(DEFAULT_QUALITY_SHADOW_PATH), _file_signature(DEFAULT_QUALITY_SHADOW_PATH),
        str(DEFAULT_AUDIT_PATH), _file_signature(DEFAULT_AUDIT_PATH),
        str(DEFAULT_RECAP_PATH), _file_signature(DEFAULT_RECAP_PATH),
        str(DEFAULT_QUALITY_REPORT_PATH), _file_signature(DEFAULT_QUALITY_REPORT_PATH),
        str(DEFAULT_REVIEW_PATH), _file_signature(DEFAULT_REVIEW_PATH),
        str(DEFAULT_SAME_TRANSVERSAL_PATH), _file_signature(DEFAULT_SAME_TRANSVERSAL_PATH),
    )


def _options(frame: pd.DataFrame, column: str) -> list[str]:
    if column not in frame:
        return []
    return sorted({text_value(item) for item in frame[column].dropna() if text_value(item)})


def _numeric_bounds(frame: pd.DataFrame, column: str) -> tuple[float, float] | None:
    if column not in frame:
        return None
    values = pd.to_numeric(frame[column], errors="coerce").dropna()
    if values.empty:
        return None
    low, high = float(values.min()), float(values.max())
    return (low, high)


def _range_filter(label: str, frame: pd.DataFrame, column: str, key: str) -> tuple[float, float] | None:
    bounds = _numeric_bounds(frame, column)
    if bounds is None:
        return None
    low, high = bounds
    if low == high:
        st.sidebar.caption(f"{label}: {low:g}")
        return bounds
    step = 1.0 if max(abs(low), abs(high)) >= 100 else 0.1
    return st.sidebar.slider(label, min_value=low, max_value=high, value=bounds, step=step, key=key)


def sidebar_filters(cases: pd.DataFrame) -> tuple[dict[str, Any], dict[str, Any]]:
    st.sidebar.header("Filtros combináveis")
    include_out_of_scope = st.sidebar.checkbox("Incluir casos fora dos candidatos shadow", value=False)
    only_without_current = st.sidebar.checkbox("Somente casos sem geometria oficial atual", value=True)
    same_only = st.sidebar.checkbox("Somente De = Até / mesma transversal", value=False)
    confidence_options = [*TARGET_CONFIDENCES, "OFFICIAL", "NO_CANDIDATE"]
    confidence = st.sidebar.multiselect("Classe de confiança", confidence_options, default=list(TARGET_CONFIDENCES))
    strategy_options = sorted(set(_options(cases[cases["is_candidate"]], "strategy_selected") + ["SAME_TRANSVERSAL_TWO_INTERSECTIONS"]))
    strategy = st.sidebar.multiselect("Estratégia", strategy_options)
    failure = st.sidebar.multiselect("Categoria de falha original", _options(cases, "original_failure_category"))
    status = st.sidebar.selectbox("Decisão humana", ["Todos", "PENDENTE", *DECISIONS], index=1)
    reviewed_label = st.sidebar.selectbox("Revisão", ["Todos", "Pendentes", "Revisados"])
    filters: dict[str, Any] = {
        "candidate_only": not include_out_of_scope,
        "confidence": confidence,
        "strategy": strategy,
        "failure_category": failure,
        "decision": None if status == "Todos" else status,
        "reviewed": {"Todos": None, "Pendentes": False, "Revisados": True}[reviewed_label],
        "divergent_only": only_without_current,
        "same_transversal": same_only,
    }
    with st.sidebar.expander("Mesma transversal", expanded=False):
        promoted = st.selectbox("Resultado SAME_TRANSVERSAL", ["Todos", "RECONSTRUCTED_HIGH", "RECONSTRUCTED_MEDIUM", "ESTIMATED"])
        intersections = st.selectbox("Interseções distintas", ["Todos", "2", "MORE_THAN_TWO"])
        ambiguous = st.selectbox("Ambiguidade", ["Todos", "Sim", "Não"])
        filters["same_promoted"] = None if promoted == "Todos" else promoted
        filters["same_intersections"] = None if intersections == "Todos" else intersections
        filters["same_ambiguous"] = None if ambiguous == "Todos" else ambiguous == "Sim"
    with st.sidebar.expander("Qualidade geométrica", expanded=True):
        filters["score_range"] = _range_filter("Score", cases, "geometry_score", "review_score")
        filters["extension_range"] = _range_filter("Extensão (m)", cases, "extensao_m", "review_extension")
        filters["deviation_range"] = _range_filter("Desvio de extensão (%)", cases, "extension_deviation_pct", "review_deviation")
        filters["snap_range"] = _range_filter("Snap De (m)", cases, "snap_distance_de_m", "review_snap")
        segment_options = [int(float(value)) for value in _options(cases, "segment_count") if value.replace(".", "", 1).isdigit()]
        component_options = [int(float(value)) for value in _options(cases, "component_count") if value.replace(".", "", 1).isdigit()]
        filters["segment_count"] = st.multiselect("Quantidade de segmentos", sorted(set(segment_options)))
        filters["component_count"] = st.multiselect("Componentes", sorted(set(component_options)))
        loop = st.selectbox("Loop detectado", ["Todos", "Sim", "Não"])
        snap = st.selectbox("Snap utilizado", ["Todos", "Sim", "Não"])
        filters["loop"] = {"Todos": None, "Sim": True, "Não": False}[loop]
        filters["snap"] = {"Todos": None, "Sim": True, "Não": False}[snap]
        filters["warnings"] = st.text_input("Aviso contém")
    with st.sidebar.expander("Busca textual"):
        filters["id"] = st.text_input("ID")
        filters["via"] = st.text_input("Via original")
        filters["codlog"] = st.text_input("CODLOG")
        filters["de"] = st.text_input("De")
        filters["ate"] = st.text_input("Até")
        filters["free_text"] = st.text_input("Texto livre")
    with st.sidebar.expander("Amostragem determinística"):
        sampling = st.checkbox("Aplicar amostra", value=False)
        sample_sizes = {
            "HIGH": st.number_input("HIGH", min_value=0, value=30, step=1),
            "MEDIUM": st.number_input("MEDIUM", min_value=0, value=30, step=1),
            "ESTIMATED": st.number_input("ESTIMATED", min_value=0, value=30, step=1),
        }
        seed = st.number_input("Seed", min_value=0, value=42, step=1)
        sample_strategy = st.selectbox("Estratégia da amostra", ["Todas", *_options(cases[cases["is_candidate"]], "strategy_selected")])
        filters["_sample"] = sampling
        filters["_sample_sizes"] = sample_sizes
        filters["_sample_seed"] = int(seed)
        filters["_sample_strategy"] = None if sample_strategy == "Todas" else sample_strategy
    return filters, {"include_out_of_scope": include_out_of_scope}


def _metric_cards(cases: pd.DataFrame, metrics: dict[str, Any]) -> None:
    coverage = metrics["coverage"]
    cards = st.columns(7)
    cards[0].metric("Total", f"{metrics['total_cases']:,}")
    cards[1].metric("Candidatos", f"{metrics['candidate_cases']:,}")
    cards[2].metric("Pendentes", f"{metrics['pending']:,}")
    cards[3].metric("Revisados", f"{metrics['reviewed']:,}")
    cards[4].metric("Aprovados", f"{metrics['approved']:,}")
    cards[5].metric("Oficial atual", f"{coverage['official_current_pct']:.2f}%")
    cards[6].metric("Shadow projetado", f"{coverage['shadow_projected_pct']:.2f}%")
    st.caption(
        f"Sem aplicar geometria: {coverage['with_human_approved_pct']:.2f}% com aprovações humanas; "
        f"{metrics['approved_for_official_use']} marcadas explicitamente para uso oficial."
    )


def _format_case_table(frame: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "id", "via_original", "via_resolvida", "de", "ate", "confidence_class", "strategy_selected",
        "geometry_score", "extension_deviation_pct", "segment_count", "component_count", "max_gap_m",
        "loop_detected", "warnings", "decision",
    ]
    available = [column for column in columns if column in frame]
    display = frame[available].copy()
    display = display.rename(columns={
        "confidence_class": "classe", "strategy_selected": "estratégia", "geometry_score": "score",
        "extension_deviation_pct": "desvio_%", "segment_count": "segmentos", "component_count": "componentes",
        "max_gap_m": "gap_m", "loop_detected": "loop", "warnings": "avisos",
    })
    return display


def _path_coordinates(geometry: Any, transformer: Any) -> list[list[list[float]]]:
    if geometry is None:
        return []
    lines = list(geometry.geoms) if geometry.geom_type == "MultiLineString" else [geometry]
    result = []
    for line in lines:
        if not hasattr(line, "coords"):
            continue
        result.append([[float(transformer.transform(x, y)[0]), float(transformer.transform(x, y)[1])] for x, y in line.coords])
    return result


def _current_path_coordinates(value: Any, transformer: Any) -> list[list[list[float]]]:
    """Lê o path oficial somente do caso selecionado; o ETL guarda lon/lat JSON."""
    raw = text_value(value)
    if not raw:
        return []
    try:
        payload = json.loads(raw)
        if isinstance(payload, list) and payload and all(isinstance(point, (list, tuple)) and len(point) >= 2 for point in payload):
            return [[[float(point[0]), float(point[1])] for point in payload]]
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    return _path_coordinates(geometry_from_wkt(raw), transformer)


def render_map(case: pd.Series, alternative_index: int | None = None) -> None:
    st.markdown("#### Mapa diagnóstico")
    if pdk is None:
        st.warning("PyDeck não está disponível neste ambiente; os dados e WKT continuam revisáveis.")
        return
    try:
        from pyproj import Transformer
        transformer = Transformer.from_crs("EPSG:31983", "EPSG:4326", always_xy=True)
    except ImportError:
        st.warning("PyProj não está disponível; não foi possível projetar o mapa.")
        return
    candidate = geometry_from_wkt(case.get("geometry_wkt")) if alternative_index is None else None
    if alternative_index is not None:
        alternatives = parse_alternatives(case.get("alternatives_json"))
        if 0 <= alternative_index < len(alternatives):
            candidate = geometry_from_wkt(alternatives[alternative_index].get("geometry_wkt"))
    path_rows = []
    for path in _current_path_coordinates(case.get("current_geometry_wkt"), transformer):
        path_rows.append({"path": path, "label": "Geometria atual", "color": [120, 120, 120], "width": 5})
    for path in _path_coordinates(candidate, transformer):
        path_rows.append({"path": path, "label": "Candidato selecionado", "color": [0, 95, 190], "width": 9})
    alternatives = parse_alternatives(case.get("alternatives_json"))
    for index, alternative in enumerate(alternatives):
        if alternative_index is not None and index == alternative_index:
            continue
        geometry = geometry_from_wkt(alternative.get("geometry_wkt"))
        for path in _path_coordinates(geometry, transformer):
            path_rows.append({"path": path, "label": f"Alternativa {index}", "color": [230, 140, 40], "width": 3})
    same_selected = geometry_from_wkt(case.get("same_transversal_geometry_wkt"))
    same_main = geometry_from_wkt(case.get("same_transversal_main_geometry_wkt"))
    same_transversal = geometry_from_wkt(case.get("same_transversal_transversal_geometry_wkt"))
    for path in _path_coordinates(same_main, transformer):
        path_rows.append({"path": path, "label": "SAME: via principal", "color": [70, 70, 70], "width": 4})
    for path in _path_coordinates(same_transversal, transformer):
        path_rows.append({"path": path, "label": "SAME: transversal comum", "color": [115, 50, 180], "width": 5})
    for path in _path_coordinates(same_selected, transformer):
        path_rows.append({"path": path, "label": "SAME: par selecionado", "color": [0, 165, 85], "width": 10})
    try:
        same_alternatives = parse_alternatives(case.get("same_transversal_alternatives_json"))
    except (TypeError, ValueError, json.JSONDecodeError):
        same_alternatives = []
    for index, alternative in enumerate(same_alternatives):
        geometry = geometry_from_wkt(alternative.get("geometry_wkt"))
        for path in _path_coordinates(geometry, transformer):
            path_rows.append({"path": path, "label": f"SAME: par alternativo {index}", "color": [245, 180, 30], "width": 3})
    points = []
    latitude = pd.to_numeric(pd.Series([case.get("latitude")]), errors="coerce").iloc[0]
    longitude = pd.to_numeric(pd.Series([case.get("longitude")]), errors="coerce").iloc[0]
    if pd.notna(latitude) and pd.notna(longitude):
        points.append({"position": [float(longitude), float(latitude)], "label": "Ponto GPS", "color": [180, 0, 0], "radius": 35})
    if candidate is not None and hasattr(candidate, "coords"):
        start = candidate.coords[0]
        end = candidate.coords[-1]
        points.extend([
            {"position": list(transformer.transform(*start)), "label": "Início candidato", "color": [0, 150, 0], "radius": 28},
            {"position": list(transformer.transform(*end)), "label": "Fim candidato", "color": [120, 0, 180], "radius": 28},
        ])
    try:
        same_points = json.loads(text_value(case.get("same_transversal_intersection_points_json"), "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        same_points = []
    if isinstance(same_points, list):
        for index, point in enumerate(same_points, start=1):
            if not isinstance(point, dict):
                continue
            try:
                x, y = float(point["x"]), float(point["y"])
            except (KeyError, TypeError, ValueError):
                continue
            longitude_point, latitude_point = transformer.transform(x, y)
            points.append({
                "position": [longitude_point, latitude_point], "label": f"Interseção SAME {index}",
                "color": [245, 115, 0], "radius": 42,
            })
    for field, label, color in (
        ("same_transversal_selected_start_point", "SAME início A", [0, 170, 70]),
        ("same_transversal_selected_end_point", "SAME fim B", [0, 70, 210]),
    ):
        try:
            point = json.loads(text_value(case.get(field), "{}"))
            x, y = float(point["x"]), float(point["y"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        longitude_point, latitude_point = transformer.transform(x, y)
        points.append({"position": [longitude_point, latitude_point], "label": label, "color": color, "radius": 50})
    if not path_rows and not points:
        st.info("Este caso não possui geometria visualizável ou coordenada GPS.")
        return
    if points:
        center = points[0]["position"]
    else:
        center = path_rows[0]["path"][len(path_rows[0]["path"]) // 2]
    layers = []
    if path_rows:
        layers.append(pdk.Layer("PathLayer", data=path_rows, get_path="path", get_color="color", get_width="width", width_min_pixels=2, pickable=True))
    if points:
        layers.append(pdk.Layer("ScatterplotLayer", data=points, get_position="position", get_fill_color="color", get_radius="radius", pickable=True))
    deck = pdk.Deck(
        layers=layers,
        initial_view_state=pdk.ViewState(latitude=center[1], longitude=center[0], zoom=14),
        tooltip={"text": "{label}"}, map_style=None,
    )
    st.pydeck_chart(deck, use_container_width=True)
    st.caption("SAME_TRANSVERSAL: cinza = via principal; roxo = transversal; verde = par selecionado; amarelo = pares alternativos; laranja = interseções; A/B = extremos inferidos.")
    st.caption("Legenda: cinza = atual; azul = candidato selecionado; laranja = alternativas; vermelho = GPS; verde/roxo = início/fim do candidato.")


def _show_fields(case: pd.Series) -> None:
    st.markdown("#### Identificação e evidências")
    left, middle, right = st.columns(3)
    for column, pairs in (
        (left, (("ID", "id"), ("Via original", "via_original"), ("Via resolvida", "via_resolvida"), ("CODLOG", "codlog"), ("De", "de"), ("Até", "ate"))),
        (middle, (("Latitude", "latitude"), ("Longitude", "longitude"), ("Extensão (m)", "extensao_m"), ("Falha original", "original_failure_category"), ("Causa raiz", "root_cause_primary"), ("Causas", "root_causes"))),
        (right, (("Status atual", "status_atual"), ("Score", "geometry_score"), ("Estratégia", "strategy_selected"), ("Componentes", "component_count"), ("Snap De / Até", "snap_distance_de_m"), ("Gap máximo", "max_gap_m"), ("Loop", "loop_detected"), ("Topologia", "topology_status"), ("Avisos", "warnings"))),
    ):
        with column:
            for label, name in pairs:
                st.write(f"**{label}:** {text_value(case.get(name), '—')}")
    st.markdown("#### Baseline × shadow")
    comparison = pd.DataFrame([
        {"campo": "Classe", "baseline": text_value(case.get("baseline_confidence"), "—"), "shadow": text_value(case.get("geometry_confidence"), "—")},
        {"campo": "Estratégia", "baseline": text_value(case.get("baseline_strategy"), "—"), "shadow": text_value(case.get("strategy_selected"), "—")},
        {"campo": "Comprimento (m)", "baseline": text_value(case.get("comprimento_path_m"), "—"), "shadow": text_value(case.get("path_length_m"), "—")},
        {"campo": "Desvio (%)", "baseline": text_value(case.get("desvio_extensao_pct"), "—"), "shadow": text_value(case.get("extension_deviation_pct"), "—")},
    ])
    st.dataframe(comparison, use_container_width=True, hide_index=True)


def _selected_saved_alternative(case: pd.Series) -> int | None:
    if text_value(case.get("decision")) != "ESCOLHER_ALTERNATIVA":
        return None
    try:
        index = int(float(case.get("selected_candidate_index")))
    except (TypeError, ValueError):
        return None
    return index if 0 <= index < len(parse_alternatives(case.get("alternatives_json"))) else None


def review_editor(case: pd.Series) -> str | None:
    alternatives = parse_alternatives(case.get("alternatives_json"))
    saved_decision = text_value(case.get("decision"), "ADIAR_REVISAO")
    if saved_decision not in DECISIONS:
        saved_decision = "ADIAR_REVISAO"
    saved_index = _selected_saved_alternative(case)
    with st.form(f"geometry_review_{case['review_key']}", clear_on_submit=False):
        st.markdown("#### Decisão humana")
        decision = st.selectbox("Decisão", list(DECISIONS), index=list(DECISIONS).index(saved_decision))
        alternative_options = ["Candidato principal", *[f"Alternativa {index}" for index in range(len(alternatives))]]
        default_choice = saved_index + 1 if saved_index is not None and saved_index + 1 < len(alternative_options) else 0
        choice = st.selectbox("Geometria a selecionar", alternative_options, index=default_choice)
        notes = st.text_area("Notas/justificativa", value=text_value(case.get("review_notes")), height=110)
        reviewed_by = st.text_input("Revisado por", value=text_value(case.get("reviewed_by")))
        confirm_estimated = False
        if text_value(case.get("confidence_class")) == "ESTIMATED":
            st.warning("ESTIMATED nunca é tratado como confirmado automaticamente. A aprovação individual exige confirmação explícita.")
            confirm_estimated = st.checkbox("Confirmo a aprovação explícita desta estimativa", value=False)
        approved_for_official_use = st.checkbox(
            "Marcar approved_for_official_use (não aplica no ETL)",
            value=parse_bool(case.get("approved_for_official_use", False)),
        )
        save, advance = st.columns(2)
        submitted = save.form_submit_button("Salvar decisão", type="primary")
        save_and_advance = advance.form_submit_button("Salvar e avançar")
    if not submitted and not save_and_advance:
        return None
    selected_index = None if choice == "Candidato principal" else int(choice.split()[-1])
    payload = {
        "decision": decision, "selected_candidate_index": 0 if selected_index is None else selected_index,
        "review_notes": notes, "reviewed_by": reviewed_by, "approved_for_official_use": approved_for_official_use,
    }
    try:
        save_decision(case, payload, DEFAULT_REVIEW_PATH, allow_estimated=confirm_estimated)
        cached_cases.clear()
        st.success("Decisão salva somente na camada de revisão humana.")
        return "next" if save_and_advance else "saved"
    except Exception as error:  # Streamlit deve transformar validação em mensagem, não derrubar a tela
        st.error(str(error))
        return None


def _batch_controls(filtered: pd.DataFrame) -> None:
    st.markdown("#### Aprovação em lote")
    include_medium = st.checkbox("Incluir MEDIUM", value=False, key="batch_medium")
    include_estimated = st.checkbox("Incluir ESTIMATED (risco alto; exige confirmação)", value=False, key="batch_estimated")
    score_min = st.number_input("Score mínimo do lote", min_value=0.0, max_value=100.0, value=75.0, step=1.0)
    max_gap = st.number_input("Gap topológico máximo (m)", min_value=0.0, max_value=100.0, value=2.0, step=0.5)
    preview = batch_approval_preview(filtered, include_medium=include_medium, include_estimated=include_estimated, score_min=score_min, max_gap_m=max_gap)
    reviewed = int(filtered.get("decision", pd.Series(pd.NA, index=filtered.index)).fillna("").astype(str).str.strip().ne("").sum())
    score = pd.to_numeric(filtered.get("geometry_score", pd.Series(dtype=float)), errors="coerce").dropna()
    deviation = pd.to_numeric(filtered.get("extension_deviation_pct", pd.Series(dtype=float)), errors="coerce").dropna()
    st.caption(
        f"Filtro: {len(filtered)} casos · já revisados: {reviewed} · média score: {score.mean():.2f} · "
        f"média desvio: {deviation.mean():.2f}% · elegíveis após bloqueios: {preview['approved']}"
    )
    if preview["distribution"]:
        st.write("Distribuição elegível: " + " · ".join(f"{name}: {count}" for name, count in preview["distribution"].items()))
    if preview["ignored_reasons"]:
        st.warning("Bloqueios: " + " · ".join(f"{name}: {count}" for name, count in preview["ignored_reasons"].items()))
    if include_estimated:
        st.error("Aprovação em lote de ESTIMATED é deliberadamente opt-in e continua sem aplicação oficial.")
    confirm = st.checkbox("Confirmo a aprovação apenas dos casos elegíveis", value=False, key="batch_confirm")
    if st.button("Aprovar todos do filtro", type="secondary", disabled=not confirm or preview["approved"] == 0):
        result = approve_cases_in_bulk(
            filtered, DEFAULT_REVIEW_PATH, include_medium=include_medium, include_estimated=include_estimated,
            score_min=score_min, max_gap_m=max_gap,
        )
        cached_cases.clear()
        st.success(f"{result['approved']} decisões salvas; {result['ignored']} casos bloqueados/fora do lote.")
        st.rerun()


def _navigation(filtered: pd.DataFrame, selected_index: int) -> None:
    keys = filtered["review_key"].tolist()
    pending = filtered["decision"].fillna("").astype(str).str.strip().eq("")
    buttons = st.columns(7)
    if buttons[0].button("Início", disabled=selected_index == 0):
        st.session_state.selected_review_key = keys[0]
        st.rerun()
    if buttons[1].button("← Anterior", disabled=selected_index == 0):
        st.session_state.selected_review_key = keys[selected_index - 1]
        st.rerun()
    if buttons[2].button("Próximo →", disabled=selected_index == len(keys) - 1):
        st.session_state.selected_review_key = keys[selected_index + 1]
        st.rerun()
    for position, label, confidence in ((3, "Próximo pendente", None), (4, "Pendente HIGH", "HIGH"), (5, "Pendente MEDIUM", "MEDIUM")):
        mask = pending.copy()
        if confidence:
            mask &= filtered["confidence_class"].eq(confidence)
        candidates = [index for index, value in enumerate(mask) if value]
        if buttons[position].button(label, disabled=not candidates):
            target = next((index for index in candidates if index > selected_index), candidates[0])
            st.session_state.selected_review_key = keys[target]
            st.rerun()
    with buttons[6]:
        target = st.number_input("Ir para", min_value=1, max_value=len(keys), value=selected_index + 1, step=1, label_visibility="collapsed")
        if st.button("Ir", key="go_case"):
            st.session_state.selected_review_key = keys[int(target) - 1]
            st.rerun()


def _export_controls(cases: pd.DataFrame) -> None:
    st.markdown("#### Exportações diagnósticas")
    columns = st.columns(4)
    if columns[0].button("Exportar aprovadas"):
        exported = export_approved(cases, DEFAULT_APPROVED_PATH)
        st.success(f"{len(exported)} registros em {DEFAULT_APPROVED_PATH.name}.")
    if columns[1].button("Exportar rejeitadas"):
        exported = export_rejected(cases, DEFAULT_REJECTED_PATH)
        st.success(f"{len(exported)} registros em {DEFAULT_REJECTED_PATH.name}.")
    if columns[2].button("Gerar relatório JSON"):
        report = write_report(cases, DEFAULT_REPORT_PATH)
        st.success(f"Relatório salvo com cobertura de revisão {report['coverage_new']['including_estimated_approved_pct']:.2f}%.")
    if columns[3].button("Recarregar fontes"):
        cached_cases.clear()
        st.rerun()


def main() -> None:
    st.title("Revisão humana de geometrias shadow")
    st.caption("Camada diagnóstica isolada · nenhum candidato é aplicado ao ETL ou ao RoadGraph.")
    try:
        cases = get_cases()
    except Exception as error:
        st.error(str(error))
        st.stop()
    metrics = review_metrics(cases)
    _metric_cards(cases, metrics)
    filters, scope = sidebar_filters(cases)
    filtered = filter_cases(cases, filters)
    if filters.get("_sample"):
        filtered = stratified_sample(filtered, filters["_sample_sizes"], filters["_sample_seed"], filters["_sample_strategy"], pending_only=False)
    st.sidebar.caption(f"{len(filtered):,} casos no filtro")
    st.info("Classes reais: HIGH/MEDIUM são reconstruções; ESTIMATED continua estimada e exige validação humana explícita.")
    _batch_controls(filtered)
    if filtered.empty:
        st.warning("Nenhum caso atende aos filtros atuais.")
        _export_controls(cases)
        return
    keys = filtered["review_key"].tolist()
    if st.session_state.get("selected_review_key") not in keys:
        st.session_state.selected_review_key = keys[0]
    selected_index = keys.index(st.session_state.selected_review_key)
    st.caption(f"Caso {selected_index + 1} de {len(filtered):,} · ID {text_value(filtered.iloc[selected_index].get('id'))}")
    _navigation(filtered, selected_index)
    mode = st.radio("Modo", ["Revisão detalhada", "Tabela geral"], horizontal=True)
    if mode == "Tabela geral":
        st.dataframe(_format_case_table(filtered), use_container_width=True, hide_index=True)
        choices = [f"{text_value(row.id)} — {text_value(row.via_original)}" for row in filtered.itertuples()]
        picked = st.selectbox("Abrir caso", range(len(choices)), format_func=lambda index: choices[index])
        if st.button("Abrir detalhe"):
            st.session_state.selected_review_key = keys[picked]
            st.rerun()
    else:
        case = filtered.iloc[selected_index]
        label = CONFIDENCE_LABELS.get(text_value(case.get("confidence_class")), text_value(case.get("geometry_confidence"), "Sem classe"))
        st.subheader(f"{text_value(case.get('confidence_class'), 'SEM_CLASSE')} · {label}")
        _show_fields(case)
        alternatives = alternatives_table(case.get("alternatives_json"))
        if not alternatives.empty:
            st.markdown("#### Ranking de alternativas")
            st.dataframe(alternatives, use_container_width=True, hide_index=True)
        render_map(case, _selected_saved_alternative(case))
        saved = review_editor(case)
        if saved == "next":
            st.session_state.selected_review_key = keys[min(selected_index + 1, len(keys) - 1)]
            st.rerun()
        if saved == "saved":
            st.rerun()
    _export_controls(cases)


if __name__ == "__main__":
    main()
