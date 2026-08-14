"""Camada de revisao humana para candidatos de geometria em modo shadow.

Este modulo e deliberadamente independente do motor de auditoria. Ele somente le
CSVs/JSON ja produzidos, faz validacoes locais sob demanda e grava decisoes em
arquivos novos. Nao importa RoadGraph, StreetResolver ou transform.py.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_QUALITY_SHADOW_PATH = ROOT / "data" / "processed" / "route_geometry_quality_shadow.csv"
DEFAULT_AUDIT_PATH = ROOT / "data" / "processed" / "route_geometry_audit.csv"
DEFAULT_RECAP_PATH = ROOT / "data" / "processed" / "recape_clean.csv"
DEFAULT_QUALITY_REPORT_PATH = ROOT / "data" / "processed" / "route_geometry_quality_report.json"
DEFAULT_SAME_TRANSVERSAL_PATH = ROOT / "data" / "processed" / "route_geometry_same_transversal_audit.csv"
DEFAULT_REVIEW_PATH = ROOT / "data" / "processed" / "route_geometry_human_review.csv"
DEFAULT_APPROVED_PATH = ROOT / "data" / "processed" / "route_geometry_approved.csv"
DEFAULT_REJECTED_PATH = ROOT / "data" / "processed" / "route_geometry_rejected.csv"
DEFAULT_REPORT_PATH = ROOT / "data" / "processed" / "route_geometry_human_review_report.json"

DECISIONS = (
    "APROVAR_GEOMETRIA",
    "REJEITAR_GEOMETRIA",
    "ESCOLHER_ALTERNATIVA",
    "MANTER_SEM_GEOMETRIA",
    "ADIAR_REVISAO",
)
TARGET_CONFIDENCES = ("HIGH", "MEDIUM", "ESTIMATED")
CONFIDENCE_ORDER = {"HIGH": 0, "MEDIUM": 1, "ESTIMATED": 2, "UNRESOLVED": 3, "NO_CANDIDATE": 4, "OFFICIAL": 5}
CONFIDENCE_LABELS = {
    "HIGH": "Reconstrução com evidência forte",
    "MEDIUM": "Reconstrução que requer validação",
    "ESTIMATED": "Geometria estimada por dados parciais",
    "UNRESOLVED": "Sem geometria resolvida",
    "NO_CANDIDATE": "Sem candidato shadow",
    "OFFICIAL": "Geometria oficial atual",
}

REVIEW_COLUMNS = [
    "review_key", "id", "decision", "selected_strategy", "selected_candidate_index",
    "geometry_confidence", "confidence_class", "geometry_score", "manual_geometry_wkt",
    "manual_geometry_geojson", "selected_candidate_length_m", "selected_candidate_deviation_pct",
    "selected_segment_count", "selected_component_count", "selected_snap_used", "selected_max_gap_m",
    "review_notes", "reviewed_at", "reviewed_by", "approved", "approved_for_official_use",
    "source_audit_version", "source_geometry_signature",
]

OUTPUT_COLUMNS = [
    "id", "review_key", "selected_strategy", "confidence_class", "geometry_confidence", "geometry_score",
    "geometry_wkt", "geometry_geojson", "candidate_length_m", "extension_deviation_pct", "segment_count",
    "component_count", "snap_used", "max_gap_m", "decision", "review_notes", "reviewed_at", "reviewed_by",
    "approved", "approved_for_official_use",
]

DEFAULT_BATCH_SCORE_MIN = 75.0
DEFAULT_BATCH_MAX_GAP_M = 2.0
CRITICAL_WARNING_TOKENS = (
    "LOOP",
    "NAO_APLICAR",
    "DESVIO_EXTENSAO_ACIMA_50",
    "SEM_LIMITE",
    "COMPONENTE_DESCONECTADO",
)


class ReviewDataError(ValueError):
    """Dados insuficientes, inconsistentes ou invalidos para revisar um caso."""


class ReviewPersistenceError(RuntimeError):
    """Uma decisao nao pode ser persistida sem arriscar o arquivo anterior."""


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    try:
        result = pd.isna(value)
    except (TypeError, ValueError):
        return False
    try:
        return bool(result)
    except (TypeError, ValueError):
        return False


def text_value(value: Any, default: str = "") -> str:
    if _is_missing(value):
        return default
    rendered = str(value).strip()
    if rendered.casefold() in {"", "nan", "none", "null", "<na>", "[]"}:
        return default
    return rendered


def parse_bool(value: Any) -> bool:
    return text_value(value).casefold() in {"true", "1", "yes", "sim", "y", "t"}


def _number(value: Any) -> float | None:
    try:
        converted = float(value)
    except (TypeError, ValueError):
        return None
    return converted if pd.notna(converted) else None


def _int_number(value: Any) -> int | None:
    converted = _number(value)
    return int(converted) if converted is not None else None


def _read_csv(path: Path | str) -> pd.DataFrame:
    source = Path(path)
    if not source.exists():
        raise FileNotFoundError(f"Arquivo de entrada não encontrado: {source}")
    return pd.read_csv(source, encoding="utf-8-sig", dtype=str, keep_default_na=True, na_values=[""])


def _empty_series(frame: pd.DataFrame) -> pd.Series:
    return pd.Series(pd.NA, index=frame.index, dtype="object")


def _source_series(frame: pd.DataFrame, names: Sequence[str]) -> pd.Series:
    found = [frame[name] for name in names if name in frame.columns]
    return _coalesce(*found) if found else _empty_series(frame)


def _coalesce(*series: pd.Series) -> pd.Series:
    if not series:
        return pd.Series(dtype="object")
    result = series[0].copy()
    for candidate in series[1:]:
        result = result.where(result.map(lambda value: bool(text_value(value))), candidate)
    return result


def _normal_confidence(value: Any) -> str:
    value = text_value(value).upper()
    if "RECONSTRUCTED_HIGH" in value or value in {"HIGH", "ALTO"}:
        return "HIGH"
    if "RECONSTRUCTED_MEDIUM" in value or value in {"MEDIUM", "MÉDIO", "MEDIO"}:
        return "MEDIUM"
    if value == "ESTIMATED" or value in {"LOW", "BAIXA", "ESTIMADA"}:
        return "ESTIMATED"
    if "UNRESOLVED" in value or value in {"NAO_RESOLVIDO", "NÃO_RESOLVIDO"}:
        return "UNRESOLVED"
    return value


def _has_value(value: Any) -> bool:
    return bool(text_value(value))


def _first_present(*values: Any) -> Any:
    for value in values:
        if not _is_missing(value) and text_value(value):
            return value
    return pd.NA


def geometry_signature(value: Any) -> str:
    """Assinatura estável da geometria sem desserializar toda a auditoria."""
    return hashlib.sha256(text_value(value).encode("utf-8")).hexdigest()[:32]


def build_review_key(row: Mapping[str, Any] | pd.Series) -> str:
    """Chave determinística independente da ordem das colunas e das linhas."""
    identifier = text_value(row.get("id"))
    raw_geometry = text_value(row.get("geometry_wkt")) or text_value(row.get("quality_geometry_wkt"))
    signature = text_value(row.get("source_geometry_signature")) or geometry_signature(raw_geometry)
    version = text_value(row.get("source_audit_version")) or text_value(row.get("shadow_version"))
    strategy = text_value(row.get("strategy_selected"))
    payload = "|".join((identifier, signature, version, strategy))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _ensure_id(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    if "id" not in result.columns:
        raise ReviewDataError("As fontes de geometria precisam conter a coluna id.")
    result["id"] = result["id"].map(lambda value: text_value(value))
    result = result[result["id"].ne("")].copy()
    return result


def _prepare_quality(frame: pd.DataFrame, prefix: str) -> pd.DataFrame:
    result = _ensure_id(frame)
    result = result.drop_duplicates("id", keep="last").copy()
    result.columns = [f"{prefix}_{column}" if column != "id" else "id" for column in result.columns]
    return result


def load_review_data(
    quality_path: Path | str = DEFAULT_QUALITY_SHADOW_PATH,
    audit_path: Path | str = DEFAULT_AUDIT_PATH,
    recape_path: Path | str = DEFAULT_RECAP_PATH,
    report_path: Path | str = DEFAULT_QUALITY_REPORT_PATH,
    same_transversal_path: Path | str = DEFAULT_SAME_TRANSVERSAL_PATH,
) -> pd.DataFrame:
    """Une as fontes read-only e devolve os 5.022 casos sem parsear geometria."""
    recape = _ensure_id(_read_csv(recape_path)).drop_duplicates("id", keep="last")
    quality = _prepare_quality(_read_csv(quality_path), "quality")
    audit = _prepare_quality(_read_csv(audit_path), "audit")
    result = recape.merge(quality, on="id", how="left").merge(audit, on="id", how="left")
    same_path = Path(same_transversal_path)
    if same_path.exists():
        same = _prepare_quality(_read_csv(same_path), "same_transversal")
        result = result.merge(same, on="id", how="left")

    quality_present = result["quality_strategy_selected"].map(_has_value) if "quality_strategy_selected" in result else pd.Series(False, index=result.index)
    current_path = _source_series(result, ("path",)).map(text_value)
    candidate_wkt = _source_series(result, ("quality_geometry_wkt", "audit_geometry_wkt")).map(text_value)
    raw_confidence = _source_series(result, ("quality_geometry_confidence", "audit_geometry_confidence")).map(text_value)
    audit_confidence = _source_series(result, ("audit_geometry_confidence",)).map(_normal_confidence)
    baseline_present = (~quality_present) & audit_confidence.isin({"HIGH", "MEDIUM"})
    result["geometry_confidence"] = raw_confidence.where(raw_confidence.ne(""), "").where(quality_present | baseline_present, current_path.map(lambda value: "OFFICIAL" if value else "NO_CANDIDATE"))
    result["confidence_class"] = result["geometry_confidence"].map(_normal_confidence)
    result.loc[~quality_present & ~baseline_present & current_path.ne(""), "confidence_class"] = "OFFICIAL"
    result.loc[~quality_present & ~baseline_present & current_path.eq(""), "confidence_class"] = "NO_CANDIDATE"

    aliases: dict[str, Sequence[str]] = {
        "via_original": ("quality_via", "audit_via", "via", "rua_raw"),
        "via_resolvida": ("quality_via_resolvida", "audit_via_resolvida", "resolucao_via", "rua_norm"),
        "codlog": ("quality_codlog", "audit_codlog", "codlog"),
        "de": ("quality_de", "audit_de", "de"),
        "ate": ("quality_ate", "audit_ate", "ate"),
        "latitude": ("quality_latitude", "audit_latitude", "latitude"),
        "longitude": ("quality_longitude", "audit_longitude", "longitude"),
        "extensao_m": ("quality_extensao_m", "audit_extensao_m", "extensao_m"),
        "status_atual": ("quality_status_atual", "audit_status_atual", "status_path"),
        "categoria_falha_atual": ("quality_categoria_falha_atual", "audit_categoria_falha_atual", "categoria_falha"),
        "strategy_selected": ("quality_strategy_selected", "audit_strategy_selected"),
        "geometry_score": ("quality_geometry_score", "audit_geometry_score"),
        "candidate_count": ("quality_candidate_count", "audit_candidate_count"),
        "ambiguous_candidates": ("quality_ambiguous_candidates", "audit_ambiguous_candidates"),
        "geometry_wkt": ("quality_geometry_wkt", "audit_geometry_wkt"),
        "geometry_geojson": ("quality_geometry_geojson", "audit_geometry_geojson"),
        "reason": ("quality_reason", "audit_reason"),
        "warnings": ("quality_warnings", "audit_warnings"),
        "alternatives_json": ("quality_alternatives_json", "audit_alternatives_json"),
        "root_cause_primary": ("quality_root_cause_primary",),
        "root_causes": ("quality_root_causes",),
        "shadow_version": ("quality_shadow_version",),
        "baseline_confidence": ("quality_before_geometry_confidence", "audit_geometry_confidence"),
        "baseline_strategy": ("quality_before_strategy", "audit_strategy_selected"),
        "component_status": ("quality_component_status", "audit_component_status"),
        "topology_status": ("quality_topology_status", "audit_topology_status"),
        "snap_used": ("quality_snap_used", "audit_snap_used"),
        "snap_distance_de_m": ("quality_snap_distance_de_m", "audit_snap_distance_de_m"),
        "snap_distance_ate_m": ("quality_snap_distance_ate_m", "audit_snap_distance_ate_m"),
        "path_length_m": ("quality_path_length_m", "audit_path_length_m"),
        "extension_deviation_pct": ("quality_extension_deviation_pct", "audit_extension_deviation_pct"),
        "segment_count": ("quality_segment_count", "audit_segment_count"),
        "component_count": ("quality_component_count", "audit_component_count"),
        "max_gap_m": ("quality_max_gap_m", "audit_max_gap_m"),
        "loop_detected": ("quality_loop_detected", "audit_loop_detected"),
        "main_street": ("quality_main_street",),
        "main_reference_distance_m": ("quality_main_reference_distance_m",),
    }
    for target, names in aliases.items():
        result[target] = _source_series(result, names)
    result["current_geometry_wkt"] = current_path
    result["has_current_geometry"] = current_path.map(bool)
    result["candidate_available"] = result["geometry_wkt"].map(_has_value)
    result["is_candidate"] = quality_present
    result["is_baseline_reconstructed"] = baseline_present
    same_present = result.get("same_transversal_intersection_count_distinct", _empty_series(result)).map(_has_value)
    result["same_transversal_eligible"] = same_present
    result["same_transversal_de_eq_ate"] = same_present
    result["same_transversal_promoted"] = _source_series(result, ("same_transversal_promoted",)).map(parse_bool)
    result["same_transversal_ambiguous"] = _source_series(result, ("same_transversal_same_transversal_ambiguous", "same_transversal_ambiguous")).map(parse_bool)
    result["same_transversal_after_confidence"] = _source_series(result, ("same_transversal_after_confidence",))
    result["same_transversal_strategy"] = _source_series(result, ("same_transversal_after_strategy", "same_transversal_strategy"))
    result["same_transversal_intersection_count_distinct"] = _source_series(result, ("same_transversal_intersection_count_distinct",))
    result["same_transversal_intersection_count_raw"] = _source_series(result, ("same_transversal_intersection_count_raw",))
    result["same_transversal_geometry_wkt"] = _source_series(result, ("same_transversal_geometry_wkt",))
    result["same_transversal_main_geometry_wkt"] = _source_series(result, ("same_transversal_main_geometry_wkt",))
    result["same_transversal_transversal_geometry_wkt"] = _source_series(result, ("same_transversal_transversal_geometry_wkt",))
    result["same_transversal_intersection_points_json"] = _source_series(result, ("same_transversal_intersection_points_json", "same_transversal_intersection_points_json"))
    result["same_transversal_alternatives_json"] = _source_series(result, ("same_transversal_alternatives_json", "same_transversal_alternatives_json"))
    result["original_failure_category"] = _coalesce(
        result["quality_categoria_falha_atual"] if "quality_categoria_falha_atual" in result else _empty_series(result),
        result["audit_categoria_falha_atual"] if "audit_categoria_falha_atual" in result else _empty_series(result),
        _source_series(result, ("categoria_falha",)),
    )
    report_version = ""
    report_file = Path(report_path)
    if report_file.exists():
        try:
            report_version = text_value(json.loads(report_file.read_text(encoding="utf-8-sig")).get("version"))
        except (OSError, ValueError, TypeError):
            report_version = ""
    result["source_audit_version"] = result["shadow_version"].map(text_value).where(result["shadow_version"].map(text_value).ne(""), report_version)
    result["source_geometry_signature"] = result["geometry_wkt"].map(geometry_signature)
    result["review_key"] = result.apply(build_review_key, axis=1)
    return result.reset_index(drop=True)


def load_quality_data(*args: Any, **kwargs: Any) -> pd.DataFrame:
    """Alias explícito para facilitar integração com a aplicação."""
    return load_review_data(*args, **kwargs)


def _normalise_review_frame(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    for column in REVIEW_COLUMNS:
        if column not in result.columns:
            result[column] = pd.NA
    if "approved" not in frame.columns and "decision" in result:
        result["approved"] = result["decision"].isin(["APROVAR_GEOMETRIA", "ESCOLHER_ALTERNATIVA"])
    result["approved"] = result["approved"].map(parse_bool)
    result["approved_for_official_use"] = result["approved_for_official_use"].map(parse_bool)
    result["selected_snap_used"] = result["selected_snap_used"].map(parse_bool)
    return result[REVIEW_COLUMNS]


def load_reviews(path: Path | str = DEFAULT_REVIEW_PATH) -> pd.DataFrame:
    source = Path(path)
    if not source.exists():
        return pd.DataFrame(columns=REVIEW_COLUMNS)
    frame = _read_csv(source)
    if "review_key" not in frame.columns:
        raise ReviewDataError("Arquivo de revisão sem review_key; não é seguro mesclar decisões.")
    return _normalise_review_frame(frame).drop_duplicates("review_key", keep="last")


def merge_reviews(cases: pd.DataFrame, reviews: pd.DataFrame) -> pd.DataFrame:
    result = cases.copy()
    reviews = _normalise_review_frame(reviews) if not reviews.empty else pd.DataFrame(columns=REVIEW_COLUMNS)
    for column in REVIEW_COLUMNS:
        if column != "review_key" and column not in result.columns:
            result[column] = pd.NA
    if reviews.empty:
        return result
    right = reviews.drop_duplicates("review_key", keep="last").set_index("review_key")
    for column in REVIEW_COLUMNS:
        if column == "review_key":
            continue
        mapped = result["review_key"].map(right[column])
        result[column] = result[column].where(mapped.map(_is_missing), mapped)
    result["approved"] = result["approved"].map(parse_bool)
    result["approved_for_official_use"] = result["approved_for_official_use"].map(parse_bool)
    return result


def _warning_tokens(value: Any) -> list[str]:
    raw = text_value(value)
    if not raw:
        return []
    return [part.strip().upper() for part in raw.replace(",", "|").split("|") if part.strip()]


def _warning_reason(value: Any, critical_tokens: Iterable[str] = CRITICAL_WARNING_TOKENS) -> str:
    tokens = _warning_tokens(value)
    hits = [token for token in tokens if any(critical.upper() in token for critical in critical_tokens)]
    return "|".join(hits)


def is_valid_wkt(value: Any) -> bool:
    raw = text_value(value)
    if not raw:
        return False
    try:
        from shapely import wkt
        geometry = wkt.loads(raw)
        return not geometry.is_empty and geometry.is_valid
    except Exception:
        return False


def geometry_from_wkt(value: Any) -> Any:
    raw = text_value(value)
    if not raw:
        return None
    try:
        from shapely import wkt
        geometry = wkt.loads(raw)
        return geometry if not geometry.is_empty else None
    except Exception:
        return None


def geometry_geojson(value: Any) -> str:
    geometry = geometry_from_wkt(value)
    if geometry is None:
        return ""
    try:
        from shapely.geometry import mapping
        return json.dumps(mapping(geometry), ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return ""


def parse_alternatives(raw: Any) -> list[dict[str, Any]]:
    value = text_value(raw)
    if not value:
        return []
    try:
        payload = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if isinstance(payload, dict):
        payload = payload.get("alternatives", [payload])
    return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []


def alternatives_table(raw: Any) -> pd.DataFrame:
    rows = []
    for index, alternative in enumerate(parse_alternatives(raw)):
        rows.append({
            "index": index,
            "strategy": alternative.get("strategy") or alternative.get("method"),
            "confidence": alternative.get("confidence") or alternative.get("geometry_confidence"),
            "score": alternative.get("score") or alternative.get("geometry_score"),
            "candidate_length_m": alternative.get("length_m") or alternative.get("path_length_m"),
            "deviation_pct": alternative.get("deviation_pct") or alternative.get("extension_deviation_pct"),
            "segment_count": alternative.get("segment_count"),
            "component_count": alternative.get("component_count"),
            "max_gap_m": alternative.get("max_gap_m"),
            "loop_detected": alternative.get("loop_detected"),
            "warnings": " | ".join(map(str, alternative.get("warnings", []))) if isinstance(alternative.get("warnings"), list) else alternative.get("warnings"),
        })
    return pd.DataFrame(rows)


def selected_candidate(case: Mapping[str, Any], alternative_index: int | None = None) -> dict[str, Any]:
    if alternative_index is not None:
        alternatives = parse_alternatives(case.get("alternatives_json"))
        if alternative_index < 0 or alternative_index >= len(alternatives):
            raise ReviewDataError("Índice de alternativa inválido para este caso.")
        item = dict(alternatives[alternative_index])
    else:
        item = {}
    item["strategy"] = _first_present(item.get("strategy"), case.get("strategy_selected"))
    item["confidence"] = _first_present(item.get("confidence"), case.get("geometry_confidence"))
    item["score"] = _first_present(item.get("score"), case.get("geometry_score"))
    item["geometry_wkt"] = _first_present(item.get("geometry_wkt"), case.get("geometry_wkt"))
    item["geometry_geojson"] = _first_present(item.get("geometry_geojson"), case.get("geometry_geojson"))
    item["length_m"] = _first_present(item.get("length_m"), case.get("path_length_m"))
    item["deviation_pct"] = _first_present(item.get("deviation_pct"), case.get("extension_deviation_pct"))
    item["segment_count"] = _first_present(item.get("segment_count"), case.get("segment_count"))
    item["component_count"] = _first_present(item.get("component_count"), case.get("component_count"))
    item["snap_used"] = _first_present(item.get("snap_used"), case.get("snap_used"))
    item["max_gap_m"] = _first_present(item.get("max_gap_m"), case.get("max_gap_m"))
    item["geometry_wkt"] = text_value(item.get("geometry_wkt"))
    if not text_value(item.get("geometry_geojson")) and item["geometry_wkt"]:
        item["geometry_geojson"] = geometry_geojson(item["geometry_wkt"])
    return item


def ordered_cases(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    if "confidence_class" not in result:
        result["confidence_class"] = result.get("geometry_confidence", pd.Series(index=result.index)).map(_normal_confidence)
    result["_confidence_order"] = result["confidence_class"].map(CONFIDENCE_ORDER).fillna(99)
    score = pd.to_numeric(result.get("geometry_score", pd.Series(index=result.index)), errors="coerce")
    deviation = pd.to_numeric(result.get("extension_deviation_pct", pd.Series(index=result.index)), errors="coerce")
    snap = pd.to_numeric(result.get("snap_distance_de_m", pd.Series(index=result.index)), errors="coerce").fillna(0)
    result["_sort_score"] = score
    result["_sort_deviation"] = deviation
    result["_sort_snap"] = snap
    result["_warning_count"] = result.get("warnings", pd.Series("", index=result.index)).map(_warning_tokens).map(len)
    return result.sort_values(
        ["_confidence_order", "_sort_score", "_sort_deviation", "_sort_snap", "_warning_count", "id"],
        ascending=[True, False, True, True, True, True], na_position="last", kind="stable",
    ).drop(columns=["_confidence_order", "_sort_score", "_sort_deviation", "_sort_snap", "_warning_count"])


def _contains(frame: pd.DataFrame, column: str, needle: Any) -> pd.Series:
    if not needle or column not in frame:
        return pd.Series(True, index=frame.index)
    return frame[column].fillna("").astype(str).str.casefold().str.contains(str(needle).casefold(), regex=False)


def _range_mask(frame: pd.DataFrame, column: str, bounds: Any) -> pd.Series:
    if bounds is None or column not in frame:
        return pd.Series(True, index=frame.index)
    numeric = pd.to_numeric(frame[column], errors="coerce")
    low, high = bounds
    return numeric.between(float(low), float(high), inclusive="both")


def filter_cases(frame: pd.DataFrame, filters: Mapping[str, Any] | None = None) -> pd.DataFrame:
    """Aplica os filtros combinados sem desserializar WKT/alternativas."""
    filters = dict(filters or {})
    result = frame.copy()
    if filters.get("candidate_only", True):
        result = result[result["is_candidate"] if "is_candidate" in result else result["confidence_class"].isin(TARGET_CONFIDENCES)]
    if filters.get("confidence"):
        requested = {_normal_confidence(item) for item in filters["confidence"]}
        result = result[result["confidence_class"].isin(requested)]
    if filters.get("strategy"):
        requested_strategies = {str(item) for item in filters["strategy"]}
        normal_strategy = result["strategy_selected"].fillna("").astype(str).isin(requested_strategies)
        same_strategy = result.get("same_transversal_strategy", pd.Series("", index=result.index)).fillna("").astype(str)
        if "SAME_TRANSVERSAL_TWO_INTERSECTIONS" in requested_strategies:
            normal_strategy |= same_strategy.eq("SAME_TRANSVERSAL_TWO_INTERSECTIONS")
        result = result[normal_strategy]
    if filters.get("same_transversal"):
        result = result[result.get("same_transversal_eligible", pd.Series(False, index=result.index)).map(parse_bool)]
    if filters.get("same_promoted"):
        promoted_confidence = str(filters["same_promoted"])
        result = result[result.get("same_transversal_after_confidence", pd.Series("", index=result.index)).fillna("").astype(str).eq(promoted_confidence)]
    if filters.get("same_intersections"):
        count = pd.to_numeric(result.get("same_transversal_intersection_count_distinct", pd.Series(0, index=result.index)), errors="coerce").fillna(0)
        result = result[count.gt(2) if filters["same_intersections"] == "MORE_THAN_TWO" else count.eq(2)]
    if filters.get("same_ambiguous") is not None:
        result = result[result.get("same_transversal_ambiguous", pd.Series(False, index=result.index)).map(parse_bool).eq(bool(filters["same_ambiguous"]))]
    decision = filters.get("decision")
    if decision:
        current = result.get("decision", pd.Series(pd.NA, index=result.index)).fillna("").astype(str).str.strip()
        result = result[current.eq("") if decision == "PENDENTE" else current.eq(str(decision))]
    reviewed = filters.get("reviewed")
    if reviewed is not None:
        current = result.get("decision", pd.Series(pd.NA, index=result.index)).fillna("").astype(str).str.strip().ne("")
        result = result[current.eq(bool(reviewed))]
    for column, bounds in (
        ("geometry_score", filters.get("score_range")), ("extensao_m", filters.get("extension_range")),
        ("extension_deviation_pct", filters.get("deviation_range")), ("snap_distance_de_m", filters.get("snap_range")),
    ):
        result = result[_range_mask(result, column, bounds)]
    if filters.get("failure_category"):
        result = result[result["original_failure_category"].fillna("").astype(str).isin(set(filters["failure_category"]))]
    for column, values in (("segment_count", filters.get("segment_count")), ("component_count", filters.get("component_count"))):
        if values:
            result = result[pd.to_numeric(result[column], errors="coerce").isin({float(value) for value in values})]
    for column, flag in (("loop_detected", filters.get("loop")), ("snap_used", filters.get("snap"))):
        if flag is not None:
            result = result[result[column].map(parse_bool).eq(bool(flag))]
    if filters.get("warnings"):
        result = result[_contains(result, "warnings", filters["warnings"])]
    for column, needle in (
        ("id", filters.get("id")), ("via_original", filters.get("via")), ("codlog", filters.get("codlog")),
        ("de", filters.get("de")), ("ate", filters.get("ate")),
    ):
        result = result[_contains(result, column, needle)]
    if filters.get("free_text"):
        searchable = result.fillna("").astype(str).agg(" ".join, axis=1)
        result = result[searchable.str.casefold().str.contains(str(filters["free_text"]).casefold(), regex=False)]
    if filters.get("divergent_only"):
        result = result[result["has_current_geometry"].map(lambda value: not parse_bool(value))]
    return ordered_cases(result)


def _review_record(case: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    record = {column: pd.NA for column in REVIEW_COLUMNS}
    record.update({"review_key": case.get("review_key"), "id": case.get("id")})
    record.update({key: value for key, value in payload.items() if key in REVIEW_COLUMNS})
    record["approved"] = parse_bool(record.get("approved"))
    record["approved_for_official_use"] = parse_bool(record.get("approved_for_official_use"))
    record["selected_snap_used"] = parse_bool(record.get("selected_snap_used"))
    return record


def validate_decision(
    case: Mapping[str, Any],
    decision: Mapping[str, Any],
    *,
    allow_estimated: bool = False,
) -> dict[str, Any]:
    payload = dict(decision)
    kind = text_value(payload.get("decision"))
    if kind not in DECISIONS:
        raise ReviewDataError("Selecione uma decisão humana válida.")
    notes = text_value(payload.get("review_notes"))
    confidence = _normal_confidence(_first_present(case.get("confidence_class"), case.get("geometry_confidence")))
    if kind in {"APROVAR_GEOMETRIA", "ESCOLHER_ALTERNATIVA"}:
        if confidence == "ESTIMATED" and not allow_estimated:
            raise ReviewDataError("Casos ESTIMATED exigem confirmação explícita para aprovação individual.")
        candidate = selected_candidate(case, _int_number(payload.get("selected_candidate_index")) if kind == "ESCOLHER_ALTERNATIVA" else None)
        if not is_valid_wkt(candidate.get("geometry_wkt")):
            raise ReviewDataError("A geometria selecionada está ausente ou possui WKT inválido.")
        if kind == "ESCOLHER_ALTERNATIVA" and not notes:
            raise ReviewDataError("Escolher uma alternativa exige uma nota de revisão.")
        payload.update({
            "selected_strategy": _first_present(candidate.get("strategy"), case.get("strategy_selected")),
            "geometry_confidence": _first_present(candidate.get("confidence"), case.get("geometry_confidence")),
            "confidence_class": _normal_confidence(_first_present(candidate.get("confidence"), confidence)),
            "geometry_score": _first_present(candidate.get("score"), case.get("geometry_score")),
            "manual_geometry_wkt": candidate.get("geometry_wkt"),
            "manual_geometry_geojson": candidate.get("geometry_geojson") or geometry_geojson(candidate.get("geometry_wkt")),
            "selected_candidate_length_m": candidate.get("length_m"),
            "selected_candidate_deviation_pct": candidate.get("deviation_pct"),
            "selected_segment_count": candidate.get("segment_count"),
            "selected_component_count": candidate.get("component_count"),
            "selected_snap_used": candidate.get("snap_used"),
            "selected_max_gap_m": candidate.get("max_gap_m"),
            "approved": True,
        })
        payload.setdefault("selected_candidate_index", 0 if kind == "APROVAR_GEOMETRIA" else payload.get("selected_candidate_index"))
    elif kind in {"REJEITAR_GEOMETRIA", "MANTER_SEM_GEOMETRIA"}:
        if not notes:
            raise ReviewDataError("Rejeitar ou manter sem geometria exige justificativa.")
        payload.update({"manual_geometry_wkt": pd.NA, "manual_geometry_geojson": pd.NA, "approved": False, "approved_for_official_use": False})
    else:
        payload.update({"approved": False, "approved_for_official_use": False})
    payload["review_notes"] = notes or pd.NA
    payload.setdefault("reviewed_at", datetime.now(timezone.utc).isoformat())
    payload["approved_for_official_use"] = parse_bool(payload.get("approved_for_official_use")) and parse_bool(payload.get("approved"))
    return payload


def atomic_write_csv(frame: pd.DataFrame, path: Path | str) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8-sig", newline="", suffix=".tmp", dir=target.parent, delete=False) as handle:
            temporary_name = handle.name
            frame.to_csv(handle, index=False)
        os.replace(temporary_name, target)
        return target
    except PermissionError as error:
        fallback = target.with_name(f"{target.stem}.pending.{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')}{target.suffix}")
        if temporary_name and Path(temporary_name).exists():
            os.replace(temporary_name, fallback)
        raise ReviewPersistenceError(f"Não foi possível substituir {target.name}; o arquivo original foi preservado. Cópia pendente: {fallback.name}") from error
    finally:
        if temporary_name and Path(temporary_name).exists():
            Path(temporary_name).unlink(missing_ok=True)


def save_decision(
    case: Mapping[str, Any],
    decision: Mapping[str, Any],
    path: Path | str = DEFAULT_REVIEW_PATH,
    *,
    allow_estimated: bool = False,
) -> pd.DataFrame:
    validated = validate_decision(case, decision, allow_estimated=allow_estimated)
    if not text_value(case.get("review_key")):
        raise ReviewDataError("Caso sem review_key; não é seguro persistir a decisão.")
    record = _review_record(case, validated)
    existing = load_reviews(path)
    previous = existing[existing["review_key"] != record["review_key"]]
    update = pd.DataFrame([record], columns=REVIEW_COLUMNS)
    combined = update if previous.empty else pd.concat([previous, update], ignore_index=True)[REVIEW_COLUMNS]
    atomic_write_csv(combined.drop_duplicates("review_key", keep="last"), path)
    return combined


def _batch_block_reason(case: Mapping[str, Any], score_min: float, max_gap_m: float, critical_tokens: Iterable[str]) -> str:
    if not _has_value(case.get("geometry_wkt")):
        return "WKT_AUSENTE"
    if not is_valid_wkt(case.get("geometry_wkt")):
        return "WKT_INVALIDO"
    if parse_bool(case.get("loop_detected")):
        return "LOOP_DETECTADO"
    if (_int_number(case.get("component_count")) or 0) > 1:
        return "MULTIPLOS_COMPONENTES"
    gap = _number(case.get("max_gap_m"))
    if gap is not None and gap > max_gap_m:
        return "MAX_GAP_ACIMA_DO_LIMITE"
    score = _number(case.get("geometry_score"))
    if score is None or score < score_min:
        return "SCORE_ABAIXO_DO_LIMITE"
    warning = _warning_reason(case.get("warnings"), critical_tokens)
    if warning:
        return f"WARNING_CRITICO:{warning}"
    return ""


def batch_approval_preview(
    cases: pd.DataFrame,
    *,
    include_medium: bool = False,
    include_estimated: bool = False,
    score_min: float = DEFAULT_BATCH_SCORE_MIN,
    max_gap_m: float = DEFAULT_BATCH_MAX_GAP_M,
    skip_reviewed: bool = True,
    critical_tokens: Iterable[str] = CRITICAL_WARNING_TOKENS,
) -> dict[str, Any]:
    allowed = {"HIGH"}
    if include_medium:
        allowed.add("MEDIUM")
    if include_estimated:
        allowed.add("ESTIMATED")
    reasons: dict[str, int] = {}
    eligible: list[str] = []
    distribution: dict[str, int] = {}
    for _, case in cases.iterrows():
        confidence = _normal_confidence(_first_present(case.get("confidence_class"), case.get("geometry_confidence")))
        if confidence not in allowed:
            reason = "CONFIANCA_NAO_INCLUIDA"
        elif skip_reviewed and text_value(case.get("decision")):
            reason = "JA_REVISADO"
        else:
            reason = _batch_block_reason(case, score_min, max_gap_m, critical_tokens)
        if reason:
            reasons[reason] = reasons.get(reason, 0) + 1
        else:
            key = text_value(case.get("review_key"))
            eligible.append(key)
            distribution[confidence] = distribution.get(confidence, 0) + 1
    return {
        "eligible_review_keys": eligible,
        "approved": len(eligible), "changed": len(eligible), "ignored": len(cases) - len(eligible),
        "distribution": distribution, "ignored_reasons": reasons,
        "score_min": float(score_min), "max_gap_m": float(max_gap_m),
    }


def approve_cases_in_bulk(
    cases: pd.DataFrame,
    path: Path | str = DEFAULT_REVIEW_PATH,
    *,
    include_medium: bool = False,
    include_estimated: bool = False,
    score_min: float = DEFAULT_BATCH_SCORE_MIN,
    max_gap_m: float = DEFAULT_BATCH_MAX_GAP_M,
    reviewed_by: str = "batch",
    skip_reviewed: bool = True,
) -> dict[str, Any]:
    started = time.perf_counter()
    existing = load_reviews(path)
    working_cases = merge_reviews(cases, existing)
    preview = batch_approval_preview(
        working_cases, include_medium=include_medium, include_estimated=include_estimated,
        score_min=score_min, max_gap_m=max_gap_m, skip_reviewed=skip_reviewed,
    )
    selected = set(preview["eligible_review_keys"])
    timestamp = datetime.now(timezone.utc).isoformat()
    records = []
    for _, case in working_cases.iterrows():
        if text_value(case.get("review_key")) not in selected:
            continue
        payload = validate_decision(case, {
            "decision": "APROVAR_GEOMETRIA", "review_notes": "Aprovação em lote após validações de segurança.",
            "reviewed_at": timestamp, "reviewed_by": reviewed_by, "approved_for_official_use": False,
        }, allow_estimated=include_estimated)
        records.append(_review_record(case, payload))
    if records:
        changed_keys = {record["review_key"] for record in records}
        previous = existing[~existing["review_key"].isin(changed_keys)]
        update = pd.DataFrame(records, columns=REVIEW_COLUMNS)
        combined = update if previous.empty else pd.concat([previous, update], ignore_index=True)[REVIEW_COLUMNS]
        atomic_write_csv(combined.drop_duplicates("review_key", keep="last"), path)
    return {
        "changed": len(records), "approved": len(records), "ignored": int(preview["ignored"]),
        "ignored_reasons": preview["ignored_reasons"], "elapsed_seconds": time.perf_counter() - started,
    }


def _is_human_approved(frame: pd.DataFrame) -> pd.Series:
    return frame.get("decision", pd.Series(pd.NA, index=frame.index)).fillna("").astype(str).isin({"APROVAR_GEOMETRIA", "ESCOLHER_ALTERNATIVA"})


def _mean_or_none(values: Any) -> float | None:
    if values is None:
        return None
    numeric = pd.to_numeric(values, errors="coerce").dropna()
    return round(float(numeric.mean()), 4) if not numeric.empty else None


def _pct(value: int | float, total: int) -> float:
    return round(float(value) * 100 / total, 6) if total else 0.0


def review_metrics(cases: pd.DataFrame, quality_report: Mapping[str, Any] | None = None) -> dict[str, Any]:
    total = int(len(cases))
    candidate = cases[cases["confidence_class"].isin(TARGET_CONFIDENCES)].copy()
    shadow_candidate = cases[cases.get("is_candidate", pd.Series(False, index=cases.index)).map(parse_bool)].copy()
    baseline = cases[cases.get("is_baseline_reconstructed", pd.Series(False, index=cases.index)).map(parse_bool)].copy()
    decision = cases.get("decision", pd.Series(pd.NA, index=cases.index)).fillna("").astype(str).str.strip()
    reviewed = decision.ne("")
    human_approved = _is_human_approved(cases)
    official = cases.get("has_current_geometry", pd.Series(False, index=cases.index)).map(parse_bool)
    approved_candidates = human_approved & cases.get("is_candidate", pd.Series(False, index=cases.index)).map(parse_bool) & cases.get("candidate_available", pd.Series(False, index=cases.index)).map(parse_bool) & ~official
    approved_high_medium = approved_candidates & cases["confidence_class"].isin(["HIGH", "MEDIUM"])
    approved_estimated = approved_candidates & cases["confidence_class"].eq("ESTIMATED")
    by_confidence: dict[str, dict[str, Any]] = {}
    for confidence in (*TARGET_CONFIDENCES, "UNRESOLVED"):
        subset = cases[cases["confidence_class"].eq(confidence)]
        subset_decision = subset.get("decision", pd.Series(pd.NA, index=subset.index)).fillna("").astype(str).str.strip()
        subset_approved = subset_decision.isin({"APROVAR_GEOMETRIA", "ESCOLHER_ALTERNATIVA"})
        by_confidence[confidence] = {
            "total": int(len(subset)), "reviewed": int(subset_decision.ne("").sum()),
            "pending": int(subset_decision.eq("").sum()), "approved": int(subset_approved.sum()),
            "approval_rate": round(float(subset_approved.mean()), 6) if len(subset) else 0.0,
        }
    by_strategy: dict[str, dict[str, Any]] = {}
    strategy_values = shadow_candidate["strategy_selected"].fillna("SEM_ESTRATEGIA").astype(str)
    for strategy in sorted(strategy_values.unique()):
        subset = shadow_candidate[strategy_values.eq(strategy)]
        subset_decision = subset.get("decision", pd.Series(pd.NA, index=subset.index)).fillna("").astype(str)
        subset_approved = subset_decision.isin({"APROVAR_GEOMETRIA", "ESCOLHER_ALTERNATIVA"})
        unresolved = int((subset["confidence_class"] == "UNRESOLVED").sum())
        by_strategy[strategy] = {
            "cases": int(len(subset)), "resolved": int(len(subset) - unresolved), "resolved_shadow": int(len(subset) - unresolved),
            "human_approved": int(subset_approved.sum()),
            "continued_estimated": int((subset["confidence_class"] == "ESTIMATED").sum()),
            "passed_high": int((subset["confidence_class"] == "HIGH").sum()),
            "passed_medium": int((subset["confidence_class"] == "MEDIUM").sum()),
            "remained_unresolved": unresolved,
            "approved_for_official_use": int((subset_approved & subset.get("approved_for_official_use", pd.Series(False, index=subset.index)).map(parse_bool)).sum()),
        }
    ranked = sorted(by_strategy.items(), key=lambda item: (item[1]["resolved_shadow"], item[1]["human_approved"], item[1]["cases"]), reverse=True)
    base_resolved = official | cases.get("is_baseline_reconstructed", pd.Series(False, index=cases.index)).map(parse_bool)
    shadow_resolved = base_resolved | cases.get("is_candidate", pd.Series(False, index=cases.index)).map(parse_bool)
    coverage = {
        "official_current_cases": int(official.sum()), "official_current_pct": _pct(int(official.sum()), total),
        "baseline_reconstructed_cases": int(baseline.shape[0]), "shadow_candidate_cases": int(len(shadow_candidate)),
        "shadow_projected_cases": int(shadow_resolved.sum()), "shadow_projected_pct": _pct(int(shadow_resolved.sum()), total),
        "with_human_approved_cases": int(base_resolved.sum() + approved_candidates.sum()),
        "with_human_approved_pct": _pct(int(base_resolved.sum() + approved_candidates.sum()), total),
        "without_estimated_approved_cases": int(base_resolved.sum() + approved_high_medium.sum()),
        "without_estimated_approved_pct": _pct(int(base_resolved.sum() + approved_high_medium.sum()), total),
        "including_estimated_approved_cases": int(base_resolved.sum() + approved_high_medium.sum() + approved_estimated.sum()),
        "including_estimated_approved_pct": _pct(int(base_resolved.sum() + approved_high_medium.sum() + approved_estimated.sum()), total),
    }
    transitions = {"pending_to_approved": int(approved_candidates.sum()), "pending_to_rejected": int(decision.isin({"REJEITAR_GEOMETRIA", "MANTER_SEM_GEOMETRIA"}).sum())}
    return {
        "total_cases": total, "candidate_cases": int(len(shadow_candidate)), "baseline_reconstructed_cases": int(len(baseline)), "pending": int((~reviewed.loc[shadow_candidate.index]).sum()),
        "reviewed": int(reviewed.sum()), "approved": int(human_approved.sum()),
        "rejected": int(decision.isin({"REJEITAR_GEOMETRIA", "MANTER_SEM_GEOMETRIA"}).sum()),
        "deferred": int((decision == "ADIAR_REVISAO").sum()),
        "approved_for_official_use": int((human_approved & cases.get("approved_for_official_use", pd.Series(False, index=cases.index)).map(parse_bool)).sum()),
        "by_decision": {kind: int((decision == kind).sum()) for kind in DECISIONS},
        "by_confidence": by_confidence, "by_strategy": by_strategy,
        "strategy_ranking": [{"strategy": name, **values} for name, values in ranked],
        "approval_rate_by_confidence": {name: values["approval_rate"] for name, values in by_confidence.items()},
        "approved_average_score": _mean_or_none(cases.loc[approved_candidates, "geometry_score"]),
        "approved_average_deviation_pct": _mean_or_none(cases.loc[approved_candidates, "extension_deviation_pct"]),
        "approved_snap_cases": int((approved_candidates & cases["snap_used"].map(parse_bool)).sum()),
        "approved_estimated_cases": int(approved_estimated.sum()),
        "approved_loop_cases": int((approved_candidates & cases["loop_detected"].map(parse_bool)).sum()),
        "approved_multiple_component_cases": int((approved_candidates & (pd.to_numeric(cases["component_count"], errors="coerce") > 1)).sum()),
        "coverage": coverage, "transitions": transitions,
        "source_quality_report_version": text_value((quality_report or {}).get("version")),
    }


def _export_rows(cases: pd.DataFrame, approved: bool) -> pd.DataFrame:
    mask = _is_human_approved(cases) if approved else cases.get("decision", pd.Series(pd.NA, index=cases.index)).fillna("").astype(str).isin({"REJEITAR_GEOMETRIA", "MANTER_SEM_GEOMETRIA"})
    selected = cases[mask].copy()
    if selected.empty:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)
    rows = []
    for _, case in selected.iterrows():
        wkt = text_value(case.get("manual_geometry_wkt")) or text_value(case.get("geometry_wkt"))
        geojson = text_value(case.get("manual_geometry_geojson")) or text_value(case.get("geometry_geojson")) or geometry_geojson(wkt)
        rows.append({
            "id": case.get("id"), "review_key": case.get("review_key"), "selected_strategy": _first_present(case.get("selected_strategy"), case.get("strategy_selected")),
            "confidence_class": case.get("confidence_class"), "geometry_confidence": case.get("geometry_confidence"), "geometry_score": case.get("geometry_score"),
            "geometry_wkt": wkt if approved else pd.NA, "geometry_geojson": geojson if approved else pd.NA,
            "candidate_length_m": _first_present(case.get("selected_candidate_length_m"), case.get("path_length_m")),
            "extension_deviation_pct": _first_present(case.get("selected_candidate_deviation_pct"), case.get("extension_deviation_pct")),
            "segment_count": _first_present(case.get("selected_segment_count"), case.get("segment_count")),
            "component_count": _first_present(case.get("selected_component_count"), case.get("component_count")),
            "snap_used": case.get("selected_snap_used") if _has_value(case.get("selected_snap_used")) else case.get("snap_used"),
            "max_gap_m": _first_present(case.get("selected_max_gap_m"), case.get("max_gap_m")), "decision": case.get("decision"),
            "review_notes": case.get("review_notes"), "reviewed_at": case.get("reviewed_at"), "reviewed_by": case.get("reviewed_by"),
            "approved": bool(_is_human_approved(pd.DataFrame([case])).iloc[0]), "approved_for_official_use": parse_bool(case.get("approved_for_official_use")),
        })
    return pd.DataFrame(rows, columns=OUTPUT_COLUMNS)


def export_approved(cases: pd.DataFrame, path: Path | str = DEFAULT_APPROVED_PATH) -> pd.DataFrame:
    exported = _export_rows(cases, True)
    atomic_write_csv(exported, path)
    return exported


def export_rejected(cases: pd.DataFrame, path: Path | str = DEFAULT_REJECTED_PATH) -> pd.DataFrame:
    exported = _export_rows(cases, False)
    atomic_write_csv(exported, path)
    return exported


def write_report(
    cases: pd.DataFrame,
    path: Path | str = DEFAULT_REPORT_PATH,
    *,
    quality_report_path: Path | str = DEFAULT_QUALITY_REPORT_PATH,
) -> dict[str, Any]:
    source: dict[str, Any] = {}
    source_file = Path(quality_report_path)
    if source_file.exists():
        try:
            source = json.loads(source_file.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, TypeError):
            source = {}
    metrics = review_metrics(cases, source)
    before = source.get("before", {}) if isinstance(source, dict) else {}
    after = source.get("after", {}) if isinstance(source, dict) else {}
    old_coverage = _number(before.get("projected_coverage_with_estimated_pct"))
    new_coverage = metrics["coverage"]["shadow_projected_pct"]
    report = {
        "version": "route-geometry-human-review-v1",
        "mode": "shadow_diagnostic_only",
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "scope": {"total_cases": metrics["total_cases"], "candidate_cases": metrics["candidate_cases"]},
        "coverage_old": {"projected_with_estimated_pct": old_coverage, "shadow_after_pct": _number(after.get("projected_coverage_with_estimated_pct"))},
        "coverage_new": {**metrics["coverage"], "headline_projected_pct": new_coverage},
        "coverage_after_human_review": metrics["coverage"],
        "absolute_gain_percentage_points": round(new_coverage - (old_coverage or 0.0), 6) if old_coverage is not None else None,
        "relative_gain_percent": round((new_coverage - old_coverage) * 100 / old_coverage, 6) if old_coverage not in (None, 0) else None,
        "metrics": metrics,
        "strategy_results": metrics["by_strategy"],
        "strategy_ranking": metrics["strategy_ranking"],
        "root_cause_distribution": cases[cases["is_candidate"]]["root_cause_primary"].fillna("SEM_CAUSA_INFORMADA").astype(str).value_counts().to_dict(),
        "official_application": False,
        "structural_limit": None,
    }
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    os.replace(temporary, target)
    return report


def stratified_sample(
    frame: pd.DataFrame,
    sizes: Mapping[str, int],
    seed: int = 42,
    strategy: str | None = None,
    pending_only: bool = False,
) -> pd.DataFrame:
    source = frame[frame["confidence_class"].isin(TARGET_CONFIDENCES)].copy()
    if strategy:
        source = source[source["strategy_selected"].fillna("").eq(strategy)]
    if pending_only:
        source = source[source.get("decision", pd.Series(pd.NA, index=source.index)).fillna("").astype(str).str.strip().eq("")]
    selected = []
    for offset, confidence in enumerate(TARGET_CONFIDENCES):
        count = max(0, int(sizes.get(confidence, 0)))
        bucket = source[source["confidence_class"].eq(confidence)]
        if count:
            selected.append(bucket.sample(n=min(count, len(bucket)), random_state=int(seed) + offset))
    return ordered_cases(pd.concat(selected, ignore_index=True)) if selected else source.iloc[0:0]


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Revisão humana shadow de candidatos de geometria.")
    parser.add_argument("--quality-shadow", type=Path, default=DEFAULT_QUALITY_SHADOW_PATH)
    parser.add_argument("--audit", type=Path, default=DEFAULT_AUDIT_PATH)
    parser.add_argument("--recapes", type=Path, default=DEFAULT_RECAP_PATH)
    parser.add_argument("--reviews", type=Path, default=DEFAULT_REVIEW_PATH)
    parser.add_argument("--export-approved", action="store_true")
    parser.add_argument("--export-rejected", action="store_true")
    parser.add_argument("--report", action="store_true")
    parser.add_argument("--sample-high", type=int, default=0)
    parser.add_argument("--sample-medium", type=int, default=0)
    parser.add_argument("--sample-estimated", type=int, default=0)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    cases = merge_reviews(load_review_data(args.quality_shadow, args.audit, args.recapes), load_reviews(args.reviews))
    if args.export_approved:
        print(f"Aprovadas: {len(export_approved(cases))}")
    if args.export_rejected:
        print(f"Rejeitadas: {len(export_rejected(cases))}")
    if args.report:
        print(f"Relatório: {DEFAULT_REPORT_PATH}")
        write_report(cases)
    if any((args.sample_high, args.sample_medium, args.sample_estimated)):
        sample = stratified_sample(cases, {"HIGH": args.sample_high, "MEDIUM": args.sample_medium, "ESTIMATED": args.sample_estimated}, args.seed)
        target = DEFAULT_REVIEW_PATH.with_name("route_geometry_human_review_sample.csv")
        atomic_write_csv(sample, target)
        print(f"Amostra: {target} ({len(sample)} casos)")


if __name__ == "__main__":
    _cli()
