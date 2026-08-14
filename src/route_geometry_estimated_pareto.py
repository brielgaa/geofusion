"""Diagnostic Pareto analysis for the remaining route-geometry ESTIMATED cases.

This module is intentionally read-only with respect to the geometry pipeline.  It
reads the latest shadow artifacts, reconciles the 42 cases promoted by the
SAME_TRANSVERSAL shadow audit, and writes only diagnostic analysis artifacts.
No geometry, score, class, human decision, or official output is changed.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
PROCESSED = ROOT / "data" / "processed"

QUALITY_SHADOW_PATH = PROCESSED / "route_geometry_quality_shadow.csv"
QUALITY_REPORT_PATH = PROCESSED / "route_geometry_quality_report.json"
SAME_TRANSVERSAL_PATH = PROCESSED / "route_geometry_same_transversal_audit.csv"
SAME_TRANSVERSAL_REPORT_PATH = PROCESSED / "route_geometry_same_transversal_report.json"
HUMAN_REVIEW_PATH = PROCESSED / "route_geometry_human_review.csv"

PARETO_PATH = PROCESSED / "route_geometry_estimated_pareto.csv"
CASES_PATH = PROCESSED / "route_geometry_estimated_cases.csv"
REPORT_PATH = PROCESSED / "route_geometry_estimated_pareto_report.json"

EXPECTED_ESTIMATED = 2079
LOW_SAMPLE_SIZE = 5

POTENTIAL_LEVELS = ("VERY_HIGH", "HIGH", "MEDIUM", "LOW", "VERY_LOW")

ROOT_CAUSE_LABELS = {
    "AUSENCIA_DE_INTERSECAO": "No verified geometric intersection between the principal road and a boundary",
    "TRANSVERSAL_INEXISTENTE": "Referenced transversal was not found or could not be resolved",
    "APENAS_UMA_TRANSVERSAL_CONHECIDA": "Only one boundary/transversal was resolved",
    "PROBLEMA_TOPOLOGICO": "Topology or continuity prevented a safe route",
    "VIA_INTEIRA": "Reference indicates the whole road rather than bounded endpoints",
    "NOME_ABREVIADO": "Abbreviated street name reduced resolution certainty",
    "NOME_INCOMPLETO": "Incomplete street name reduced resolution certainty",
    "MULTIPLOS_SEGMENTOS_POSSIVEIS": "Multiple segments remained possible",
    "OTHER": "Other or missing diagnostic cause",
}

CRITICAL_WARNING_TOKENS = {
    "DESVIO_EXTENSAO_ACIMA_50_PCT",
    "LOOP_DETECTADO",
    "sem_limites_topologicos_confirmados",
    "snap_virtual_nao_aplicar_sem_revisao",
    "limite_inferido",
}

SEVERE_WARNING_TOKENS = {
    "DESVIO_EXTENSAO_ACIMA_50_PCT",
    "LOOP_DETECTADO",
    "snap_virtual_nao_aplicar_sem_revisao",
    "limite_inferido",
}

KNOWN_UNRESOLVED = {"", "NAN", "NONE", "NA", "N/A", "UNRESOLVED", "NAO_RESOLVIDA", "NAO RESOLVIDA"}


def _text(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value).strip()


def _upper(value: Any) -> str:
    return _text(value).upper()


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    raw = _text(value).replace("%", "").replace(",", ".")
    if not raw:
        return None
    try:
        number = float(raw)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return _upper(value) in {"TRUE", "1", "YES", "SIM", "Y", "T"}


def _tokens(value: Any) -> list[str]:
    raw = _text(value)
    if not raw:
        return []
    if raw.startswith("[") and raw.endswith("]"):
        try:
            decoded = json.loads(raw)
            if isinstance(decoded, list):
                return [_text(item) for item in decoded if _text(item)]
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    return [part.strip() for part in raw.replace(",", "|").split("|") if part.strip()]


def _unique(items: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        value = _text(item)
        if value and value not in seen:
            result.append(value)
            seen.add(value)
    return result


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    frame = pd.read_csv(path, encoding="utf-8-sig", dtype=str, low_memory=False)
    if "id" in frame.columns:
        frame["id"] = frame["id"].map(_text)
    return frame


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _coalesce(*values: Any) -> Any:
    for value in values:
        if _text(value):
            return value
    return None


def _is_resolved_status(value: Any) -> bool:
    return _upper(value) not in KNOWN_UNRESOLVED


def _source_causes(row: Mapping[str, Any]) -> list[str]:
    causes = _tokens(row.get("root_causes"))
    primary = _text(row.get("root_cause_primary"))
    if primary and primary not in causes:
        causes.insert(0, primary)
    return _unique(causes)


def _has_cause(row: Mapping[str, Any], cause: str) -> bool:
    return cause in _source_causes(row)


def classify_root_cause(row: Mapping[str, Any]) -> str:
    """Return one deterministic primary cause, preserving the shadow diagnosis.

    The existing shadow artifact already contains a primary root cause generated
    by the geometry audit.  It is the least speculative primary classification,
    so it is retained whenever valid.  Synthetic rows and older artifacts use
    fields in the same row to derive a compatible fallback.
    """

    primary = _text(row.get("root_cause_primary"))
    if primary:
        return primary

    causes = _source_causes(row)
    if causes:
        return causes[0]
    de_known = _is_resolved_status(row.get("de_status"))
    ate_known = _is_resolved_status(row.get("ate_status"))
    if _has_cause(row, "VIA_INTEIRA"):
        return "VIA_INTEIRA"
    if not de_known and not ate_known:
        return "SEM_DE_E_ATE"
    if de_known != ate_known:
        return "APENAS_UMA_TRANSVERSAL_CONHECIDA"
    if _number(row.get("component_count")) and _number(row.get("component_count")) > 1:
        return "PROBLEMA_TOPOLOGICO"
    if _number(row.get("candidate_count")) and _number(row.get("candidate_count")) > 1:
        return "MULTIPLOS_SEGMENTOS_POSSIVEIS"
    return "OTHER"


def derive_secondary_causes(row: Mapping[str, Any], primary: str | None = None) -> list[str]:
    """Build overlapping, evidence-based diagnostic signals for one case."""

    primary = primary or classify_root_cause(row)
    causes = _source_causes(row)
    derived: list[str] = []
    strategy = _upper(row.get("strategy_selected"))
    reason = _upper(row.get("reason"))
    warnings = {_upper(item) for item in _tokens(row.get("warnings"))}
    de_known = _is_resolved_status(row.get("de_status"))
    ate_known = _is_resolved_status(row.get("ate_status"))
    component_count = _number(row.get("component_count"))
    candidate_count = _number(row.get("candidate_count"))
    extension = _number(row.get("extension_deviation"))
    same_intersections = _number(row.get("same_intersection_count_distinct")) or 0

    if "GPS_SNAP_LINEAR_GROWTH" in strategy:
        derived.append("GPS_LINEAR_GROWTH")
    elif "GPS_SNAP" in strategy:
        derived.append("GPS_SNAP")
    if "SHADOW_MAIN" in strategy or "RESOLVIDA_POR_NOME_GPS" in reason or reason == "VIA_PRINCIPAL_SHADOW":
        derived.append("MAIN_STREET_BY_NAME_GPS")
    if strategy.startswith("COORD_EXTENSION"):
        derived.append("COORDINATE_EXTENSION")
    if strategy == "NEAREST_SEGMENT_ESTIMATED":
        derived.append("NEAREST_SEGMENT_FALLBACK")

    if not de_known and not ate_known:
        derived.append("SEM_DE_E_ATE")
    elif de_known != ate_known:
        derived.append("ONE_TRANSVERSAL_ONLY")
        derived.append("SEM_DE" if not de_known else "SEM_ATE")
    if same_intersections >= 1:
        derived.append("SAME_TRANSVERSAL")
    if component_count is not None and component_count > 1:
        derived.append("MULTIPLE_COMPONENTS")
    if candidate_count is not None and candidate_count > 1 or _bool(row.get("ambiguous_candidates")):
        derived.append("MULTIPLE_EQUIVALENT_CANDIDATES")
    if "PISTAS_PARALELAS" in causes or "PARALEL" in reason:
        derived.append("PARALLEL_ROADS")
    if "NOME_ABREVIADO" in causes or "NOME_INCOMPLETO" in causes:
        derived.append("STREET_NAME_UNCERTAINTY")
    if "TRANSVERSAL_INEXISTENTE" in causes:
        derived.append("TRANSVERSAL_UNCERTAINTY")
    if "PROBLEMA_TOPOLOGICO" in causes or _number(row.get("max_gap_m")) not in (None, 0):
        derived.append("TOPOLOGY_GAP")
    if extension is None:
        derived.append("MISSING_EXTENSION")
    elif extension > 50:
        derived.append("HIGH_EXTENSION_DEVIATION")
    elif extension <= 10:
        derived.append("LOW_EXTENSION_DEVIATION")
    if "LOOP_DETECTADO" in warnings or _bool(row.get("loop_detected")):
        derived.append("LOOP_WARNING")
    if _has_cause(row, "VIA_INTEIRA"):
        derived.append("VIA_INTEIRA")

    return _unique([cause for cause in [*causes, *derived] if cause != primary])


def _critical_warnings(row: Mapping[str, Any]) -> list[str]:
    warnings = _unique([*_tokens(row.get("warnings")), *_tokens(row.get("same_warnings"))])
    return [warning for warning in warnings if warning in CRITICAL_WARNING_TOKENS]


def _severe_warnings(row: Mapping[str, Any]) -> list[str]:
    warnings = _unique([*_tokens(row.get("warnings")), *_tokens(row.get("same_warnings"))])
    return [warning for warning in warnings if warning in SEVERE_WARNING_TOKENS]


def _endpoint_count(row: Mapping[str, Any]) -> int:
    return int(_is_resolved_status(row.get("de_status"))) + int(_is_resolved_status(row.get("ate_status")))


def _numeric_value(row: Mapping[str, Any], key: str) -> float | None:
    return _number(row.get(key))


def calculate_promotion_potential(row: Mapping[str, Any]) -> str:
    """Score evidence quality without changing the source confidence class.

    This is a screening indicator for prioritization, not a promotion rule.
    Thresholds are deliberately explicit and are tested independently so that
    future analysis runs remain reproducible.
    """

    points = 0
    score = _numeric_value(row, "score")
    gps = _numeric_value(row, "gps_distance")
    extension = _numeric_value(row, "extension_deviation")
    component_count = _numeric_value(row, "component_count")
    candidate_count = _numeric_value(row, "candidate_count")
    margin = _numeric_value(row, "margin_top2")
    intersections = _numeric_value(row, "same_intersection_count_distinct") or 0

    if score is not None:
        points += 2 if score >= 95 else 1 if score >= 90 else -2 if score < 80 else 0
    else:
        points -= 1
    if gps is None:
        points -= 1
    elif gps <= 5:
        points += 2
    elif gps <= 20:
        points += 1
    elif gps > 100:
        points -= 2
    if extension is None:
        points -= 1
    elif extension <= 10:
        points += 2
    elif extension <= 25:
        points += 1
    elif extension > 50:
        points -= 2
    endpoints = _endpoint_count(row)
    points += 2 if endpoints == 2 else 1 if endpoints == 1 else -1
    points += 2 if intersections >= 2 else 1 if intersections == 1 else 0
    if component_count is not None:
        points += 1 if component_count == 1 else -2
    if _bool(row.get("loop")):
        points -= 2
    points -= min(3, len(_critical_warnings(row)))
    if candidate_count is not None and candidate_count > 1 or _bool(row.get("ambiguous_candidates")):
        points -= 1
    if margin is not None:
        points += 2 if margin >= 50 else 1 if margin >= 10 else -1 if margin < 5 else 0
    topology = _upper(row.get("topology_status"))
    if topology == "SAME_COMPONENT" or _upper(row.get("component_status")) == "SAME_COMPONENT":
        points += 1
    elif topology in {"VIRTUAL_PROJECTION", "GPS_SNAP_LINEAR"}:
        points -= 1

    if points >= 10:
        return "VERY_HIGH"
    if points >= 7:
        return "HIGH"
    if points >= 4:
        return "MEDIUM"
    if points >= 1:
        return "LOW"
    return "VERY_LOW"


def _prepare_effective_cases(
    quality_shadow: pd.DataFrame,
    same_transversal: pd.DataFrame,
    human_review: pd.DataFrame,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    required = {"id", "geometry_confidence"}
    missing = sorted(required - set(quality_shadow.columns))
    if missing:
        raise ValueError(f"quality shadow is missing required columns: {missing}")

    quality = quality_shadow.copy()
    quality["id"] = quality["id"].map(_text)
    raw_estimated = quality[quality["geometry_confidence"].map(_upper).eq("ESTIMATED")].copy()

    same = same_transversal.copy()
    if not same.empty and "id" in same.columns:
        same["id"] = same["id"].map(_text)
    promoted_ids: set[str] = set()
    if not same.empty and {"id", "before_confidence", "after_confidence"}.issubset(same.columns):
        promoted_ids = set(
            same.loc[
                same["before_confidence"].map(_upper).eq("ESTIMATED")
                & ~same["after_confidence"].map(_upper).eq("ESTIMATED"),
                "id",
            ]
        )
    effective = raw_estimated[~raw_estimated["id"].isin(promoted_ids)].copy()

    same_columns = {
        "intersection_count_distinct": "same_intersection_count_distinct",
        "margin_top2": "margin_top2",
        "distance_to_gps_m": "same_distance_to_gps_m",
        "extension_deviation_pct": "same_extension_deviation_pct",
        "after_confidence": "same_after_confidence",
        "after_strategy": "same_after_strategy",
        "warnings": "same_warnings",
    }
    if not same.empty and "id" in same.columns:
        same_view = same[["id", *[column for column in same_columns if column in same.columns]]].rename(columns=same_columns)
        same_view = same_view.drop_duplicates("id", keep="last")
        effective = effective.merge(same_view, on="id", how="left", validate="one_to_one")
    else:
        for column in same_columns.values():
            effective[column] = None

    if not human_review.empty and "id" in human_review.columns:
        review = human_review.copy()
        review["id"] = review["id"].map(_text)
        review_columns = [column for column in ["id", "decision", "approved", "geometry_confidence"] if column in review.columns]
        review_view = review[review_columns].rename(columns={column: f"review_{column}" for column in review_columns if column != "id"})
        review_view = review_view.drop_duplicates("id", keep="last")
        effective = effective.merge(review_view, on="id", how="left", validate="one_to_one")
    else:
        effective["review_decision"] = None
        effective["review_approved"] = None

    effective["score"] = effective.apply(lambda row: _number(_coalesce(row.get("geometry_score"), row.get("same_score"))), axis=1)
    effective["extension_deviation"] = effective.apply(
        lambda row: _number(_coalesce(row.get("same_extension_deviation_pct"), row.get("extension_deviation_pct"))), axis=1
    )
    effective["gps_distance"] = effective.apply(
        lambda row: _number(_coalesce(row.get("same_distance_to_gps_m"), row.get("main_reference_distance_m"))), axis=1
    )
    effective["snap_distance"] = effective.apply(
        lambda row: max(
            [
                value
                for value in (_number(row.get("snap_distance_de_m")), _number(row.get("snap_distance_ate_m")))
                if value is not None
            ],
            default=None,
        ),
        axis=1,
    )
    effective["component_count"] = effective["component_count"].map(_number)
    effective["candidate_count"] = effective["candidate_count"].map(_number)
    effective["margin_top2"] = effective["margin_top2"].map(_number)
    effective["same_intersection_count_distinct"] = effective["same_intersection_count_distinct"].map(_number)
    effective["loop"] = effective.apply(
        lambda row: _bool(row.get("loop_detected")) or "LOOP_DETECTADO" in {_upper(item) for item in _tokens(row.get("warnings"))},
        axis=1,
    )
    effective["warnings"] = effective.apply(
        lambda row: " | ".join(_unique([*_tokens(row.get("warnings")), *_tokens(row.get("same_warnings"))])), axis=1
    )
    effective["root_cause"] = effective.apply(classify_root_cause, axis=1)
    effective["secondary_causes"] = effective.apply(
        lambda row: " | ".join(derive_secondary_causes(row, row["root_cause"])), axis=1
    )
    effective["critical_warning_list"] = effective.apply(_critical_warnings, axis=1)
    effective["critical_warning_count"] = effective["critical_warning_list"].map(len)
    effective["with_de"] = effective["de_status"].map(_is_resolved_status).astype(int)
    effective["with_ate"] = effective["ate_status"].map(_is_resolved_status).astype(int)
    effective["with_both"] = ((effective["with_de"] == 1) & (effective["with_ate"] == 1)).astype(int)
    effective["with_none"] = ((effective["with_de"] == 0) & (effective["with_ate"] == 0)).astype(int)
    effective["multiple_components"] = (effective["component_count"].fillna(0) > 1).astype(int)
    effective["promotion_potential"] = effective.apply(calculate_promotion_potential, axis=1)
    return effective, {
        "quality_shadow_estimated": int(len(raw_estimated)),
        "same_transversal_promoted_from_estimated": int(len(promoted_ids)),
        "same_transversal_promoted_high": int(
            same.loc[same["id"].isin(promoted_ids), "after_confidence"].map(_upper).eq("RECONSTRUCTED_HIGH").sum()
        ) if not same.empty and "after_confidence" in same.columns else 0,
        "same_transversal_promoted_medium": int(
            same.loc[same["id"].isin(promoted_ids), "after_confidence"].map(_upper).eq("RECONSTRUCTED_MEDIUM").sum()
        ) if not same.empty and "after_confidence" in same.columns else 0,
        "effective_estimated": int(len(effective)),
        "expected_estimated": EXPECTED_ESTIMATED,
        "population_reconciliation": (
            "quality_shadow still contains 42 ESTIMATED rows promoted in the latest "
            "same-transversal shadow artifact; those IDs were excluded analytically"
        ),
    }


def load_effective_estimated_cases(
    quality_shadow_path: Path | str = QUALITY_SHADOW_PATH,
    same_transversal_path: Path | str = SAME_TRANSVERSAL_PATH,
    human_review_path: Path | str = HUMAN_REVIEW_PATH,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Load and enrich only the effective ESTIMATED population."""

    cases, reconciliation = _prepare_effective_cases(
        _read_csv(Path(quality_shadow_path)),
        _read_csv(Path(same_transversal_path)),
        _read_csv(Path(human_review_path)),
    )
    if reconciliation["effective_estimated"] != EXPECTED_ESTIMATED:
        reconciliation["population_divergence"] = (
            reconciliation["effective_estimated"] - EXPECTED_ESTIMATED
        )
        reconciliation["population_divergence_reason"] = (
            "The effective count differs from the expected 2,079; source artifacts were not modified."
        )
    else:
        reconciliation["population_divergence"] = 0
        reconciliation["population_divergence_reason"] = None
    return cases, reconciliation


def _mean(frame: pd.DataFrame, column: str) -> float | None:
    if column not in frame:
        return None
    values = pd.to_numeric(frame[column], errors="coerce").dropna()
    return float(values.mean()) if not values.empty else None


def _median(frame: pd.DataFrame, column: str) -> float | None:
    if column not in frame:
        return None
    values = pd.to_numeric(frame[column], errors="coerce").dropna()
    return float(values.median()) if not values.empty else None


def _round(value: Any, digits: int = 4) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def build_pareto(cases: pd.DataFrame) -> pd.DataFrame:
    total = len(cases)
    rows: list[dict[str, Any]] = []
    for cause, group in cases.groupby("root_cause", sort=False):
        distribution = group["promotion_potential"].value_counts().to_dict()
        reviewed = group["review_decision"].map(_text).ne("") if "review_decision" in group else pd.Series(False, index=group.index)
        approved = group["review_approved"].map(_bool) if "review_approved" in group else pd.Series(False, index=group.index)
        reviewed_count = int(reviewed.sum())
        approved_count = int((reviewed & approved).sum())
        rows.append(
            {
                "root_cause": cause,
                "count": int(len(group)),
                "percentage": _round(100 * len(group) / total if total else 0),
                "promotion_potential": max(POTENTIAL_LEVELS, key=lambda level: (distribution.get(level, 0), -POTENTIAL_LEVELS.index(level))),
                "avg_score": _round(_mean(group, "score")),
                "median_score": _round(_median(group, "score")),
                "avg_extension_deviation": _round(_mean(group, "extension_deviation")),
                "avg_gps_distance": _round(_mean(group, "gps_distance")),
                "avg_snap_distance": _round(_mean(group, "snap_distance")),
                "with_de": int(group["with_de"].sum()),
                "with_ate": int(group["with_ate"].sum()),
                "with_both": int(group["with_both"].sum()),
                "with_none": int(group["with_none"].sum()),
                "multiple_components": int(group["multiple_components"].sum()),
                "loops": int(group["loop"].sum()),
                "critical_warnings": int((group["critical_warning_count"] > 0).sum()),
                "human_reviewed": reviewed_count,
                "human_approved": approved_count,
                "human_approval_rate": _round(100 * approved_count / reviewed_count if reviewed_count else None),
                "human_review_sample_flag": "LOW_SAMPLE_SIZE" if 0 < reviewed_count < LOW_SAMPLE_SIZE else "OK" if reviewed_count else "NO_REVIEW",
                "promotion_potential_distribution": json.dumps(distribution, sort_keys=True),
            }
        )
    result = pd.DataFrame(rows)
    if result.empty:
        return result
    result = result.sort_values(["count", "root_cause"], ascending=[False, True], kind="mergesort").reset_index(drop=True)
    result["cumulative_percentage"] = result["percentage"].cumsum().round(4)
    columns = [
        "root_cause", "count", "percentage", "cumulative_percentage", "promotion_potential",
        "avg_score", "median_score", "avg_extension_deviation", "avg_gps_distance", "avg_snap_distance",
        "with_de", "with_ate", "with_both", "with_none", "multiple_components", "loops", "critical_warnings",
        "human_reviewed", "human_approved", "human_approval_rate", "human_review_sample_flag",
        "promotion_potential_distribution",
    ]
    return result[columns]


def pareto_cut(result: pd.DataFrame, threshold: float) -> dict[str, Any]:
    if result.empty:
        return {"threshold_percentage": threshold, "groups": [], "count": 0, "percentage": 0.0}
    selected = result[result["cumulative_percentage"] <= threshold].copy()
    if selected.empty:
        selected = result.iloc[[0]].copy()
    elif float(selected.iloc[-1]["cumulative_percentage"]) < threshold and len(selected) < len(result):
        selected = result.iloc[: len(selected) + 1].copy()
    return {
        "threshold_percentage": threshold,
        "groups": [
            {
                "root_cause": row.root_cause,
                "count": int(row.count),
                "percentage": _round(row.percentage),
                "cumulative_percentage": _round(row.cumulative_percentage),
            }
            for row in selected.itertuples()
        ],
        "count": int(selected["count"].sum()),
        "percentage": _round(float(selected["count"].sum()) / len(result.assign(_total=1)) * 100) if False else _round(float(selected["count"].sum()) / float(result["count"].sum()) * 100),
    }


def simulate_scenarios(
    cases: pd.DataFrame,
    current_coverage_without_estimated: float,
    coverage_denominator: int,
    rates: Mapping[str, Mapping[str, float]] | None = None,
) -> dict[str, Any]:
    """Return hypothetical promotion scenarios; no source class is changed."""

    scenario_rates = rates or {
        "CONSERVADOR": {"VERY_HIGH": 0.80},
        "MODERADO": {"VERY_HIGH": 0.80, "HIGH": 0.60},
        "AGRESSIVO": {"VERY_HIGH": 0.80, "HIGH": 0.70, "MEDIUM": 0.50},
    }
    result: dict[str, Any] = {
        "assumption": "Rates are hypothetical screening assumptions, not observed promotion rates.",
        "coverage_denominator": int(coverage_denominator),
        "current_coverage_without_estimated_pct": _round(current_coverage_without_estimated),
        "scenarios": {},
    }
    for name, configured_rates in scenario_rates.items():
        by_level: dict[str, dict[str, Any]] = {}
        promoted = 0
        for level, rate in configured_rates.items():
            count = int((cases["promotion_potential"] == level).sum())
            planned = int(math.floor(count * float(rate)))
            promoted += planned
            by_level[level] = {"eligible_cases": count, "assumed_rate": float(rate), "potentially_promoted": planned}
        remaining = len(cases) - promoted
        coverage = current_coverage_without_estimated + (100 * promoted / coverage_denominator if coverage_denominator else 0)
        result["scenarios"][name] = {
            "eligible_by_potential": by_level,
            "potentially_promoted": promoted,
            "estimated_remaining": remaining,
            "projected_coverage_without_estimated_pct": _round(coverage),
        }
    return result


HEURISTIC_DEFINITIONS = [
    {
        "name": "PROMOTE_DUAL_BOUNDARY_LOW_DEVIATION",
        "complexity": "MEDIUM",
        "risk": "LOW",
        "dependencies": "Two resolved boundaries; one component; no loop; extension <=25%; score >=90",
        "evidence": "Manual review on a stratified sample and comparison against segment continuity",
        "expected_confidence": "HIGH",
        "risk_cost": 1.0,
    },
    {
        "name": "PROMOTE_COORD_EXTENSION_LOW_DEVIATION",
        "complexity": "MEDIUM",
        "risk": "LOW_TO_MEDIUM",
        "dependencies": "Coordinate extension; one component; no loop; score >=85; extension <=25%",
        "evidence": "Validate endpoint direction and continuity against the principal-road component",
        "expected_confidence": "MEDIUM_TO_HIGH",
        "risk_cost": 1.2,
    },
    {
        "name": "REVIEW_SINGLE_TRANSVERSAL_WITH_GPS",
        "complexity": "MEDIUM",
        "risk": "MEDIUM",
        "dependencies": "One resolved boundary; one component; GPS distance <=20m; no loop",
        "evidence": "Human review focused on inferred boundary direction and missing/compatible extension",
        "expected_confidence": "MEDIUM",
        "risk_cost": 1.5,
    },
    {
        "name": "CALIBRATE_GPS_LINEAR_GROWTH_GATE",
        "complexity": "LOW",
        "risk": "HIGH",
        "dependencies": "GPS linear-growth strategy; GPS distance <=20m; one component; no loop",
        "evidence": "Calibrate score and extension gates with a large stratified review sample",
        "expected_confidence": "LOW",
        "risk_cost": 2.5,
    },
    {
        "name": "CALIBRATE_GPS_SNAP_GATE",
        "complexity": "LOW",
        "risk": "HIGH",
        "dependencies": "GPS snap strategy; GPS distance <=20m; one component; no loop",
        "evidence": "Validate snap distance threshold and nearby parallel-road rate",
        "expected_confidence": "LOW",
        "risk_cost": 2.5,
    },
    {
        "name": "DISAMBIGUATE_NEAREST_SEGMENT_BY_EVIDENCE",
        "complexity": "HIGH",
        "risk": "HIGH",
        "dependencies": "Nearest-segment fallback; candidate disambiguation evidence; topology safeguards",
        "evidence": "Large stratified human-review sample because the cohort is high-volume and ambiguous",
        "expected_confidence": "LOW_TO_MEDIUM",
        "risk_cost": 3.0,
    },
]


def _heuristic_match(name: str, row: Mapping[str, Any]) -> bool:
    strategy = _upper(row.get("strategy_selected"))
    score = _number(row.get("score"))
    extension = _number(row.get("extension_deviation"))
    gps = _number(row.get("gps_distance"))
    component = _number(row.get("component_count"))
    candidate = _number(row.get("candidate_count"))
    if _bool(row.get("loop")) or component != 1:
        return False
    if name == "PROMOTE_DUAL_BOUNDARY_LOW_DEVIATION":
        return bool(row.get("with_both")) and score is not None and score >= 90 and extension is not None and extension <= 25 and not _severe_warnings(row) and (candidate is None or candidate <= 3)
    if name == "PROMOTE_COORD_EXTENSION_LOW_DEVIATION":
        return "COORD_EXTENSION" in strategy and component == 1 and not _bool(row.get("loop")) and score is not None and score >= 85 and extension is not None and extension <= 25 and not _severe_warnings(row)
    if name == "REVIEW_SINGLE_TRANSVERSAL_WITH_GPS":
        return (int(row.get("with_de", 0)) + int(row.get("with_ate", 0))) == 1 and component == 1 and not _bool(row.get("loop")) and score is not None and score >= 70 and gps is not None and gps <= 20 and (extension is None or extension <= 25) and not _severe_warnings(row)
    if name == "CALIBRATE_GPS_LINEAR_GROWTH_GATE":
        return "GPS_SNAP_LINEAR_GROWTH" in strategy and component == 1 and not _bool(row.get("loop")) and gps is not None and gps <= 20 and not _severe_warnings(row)
    if name == "CALIBRATE_GPS_SNAP_GATE":
        return "GPS_SNAP" in strategy and "LINEAR_GROWTH" not in strategy and component == 1 and not _bool(row.get("loop")) and gps is not None and gps <= 20
    if name == "DISAMBIGUATE_NEAREST_SEGMENT_BY_EVIDENCE":
        return strategy == "NEAREST_SEGMENT_ESTIMATED" and score is not None and score >= 90 and (candidate is None or candidate <= 1) and (extension is None or extension <= 25) and gps is not None and gps <= 20 and not _severe_warnings(row)
    return False


def recommend_heuristics(cases: pd.DataFrame) -> list[dict[str, Any]]:
    recommendations: list[dict[str, Any]] = []
    for definition in HEURISTIC_DEFINITIONS:
        matched = cases[cases.apply(lambda row: _heuristic_match(definition["name"], row), axis=1)]
        affected = len(matched)
        potential_counts = matched["promotion_potential"].value_counts().to_dict() if affected else {}
        high_quality = int(sum(potential_counts.get(level, 0) for level in ("VERY_HIGH", "HIGH")))
        average_score = _mean(matched, "score")
        priority_score = (affected * ((average_score or 0) / 100) / definition["risk_cost"]) if affected else 0.0
        primary_causes = matched["root_cause"].value_counts().head(3).to_dict() if affected else {}
        recommendations.append(
            {
                "name": definition["name"],
                "root_causes_attacked": primary_causes,
                "affected_cases": affected,
                "potentially_promotable_cases": affected,
                "high_evidence_cases": high_quality,
                "potential_distribution": potential_counts,
                "gain_potential": {
                    "screening_cohort_cases": affected,
                    "high_evidence_cases": high_quality,
                    "note": "Hypothetical cohort only; no records were promoted",
                },
                "complexity": definition["complexity"],
                "risk": definition["risk"],
                "dependencies": definition["dependencies"],
                "evidence_needed": definition["evidence"],
                "expected_confidence": definition["expected_confidence"],
                "priority_score": _round(priority_score),
            }
        )
    return sorted(recommendations, key=lambda item: (-item["priority_score"], -item["affected_cases"], item["name"]))[:5]


def assign_recommended_heuristic(cases: pd.DataFrame, recommendations: list[dict[str, Any]]) -> pd.Series:
    names = [item["name"] for item in recommendations]
    return cases.apply(
        lambda row: next((name for name in names if _heuristic_match(name, row)), "NONE_IDENTIFIED"), axis=1
    )


def _distribution(series: pd.Series) -> dict[str, int]:
    return {str(key): int(value) for key, value in series.fillna("<NA>").value_counts().items()}


def _token_ranking(series: pd.Series, total: int) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    for value in series.fillna(""):
        counts.update(item for item in _tokens(value) if item)
    return [
        {"name": name, "count": int(count), "percentage": _round(100 * count / total if total else 0)}
        for name, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def _value_ranking(series: pd.Series, total: int) -> list[dict[str, Any]]:
    return [
        {"name": str(name), "count": int(count), "percentage": _round(100 * count / total if total else 0)}
        for name, count in sorted(series.fillna("<NA>").value_counts().items(), key=lambda item: (-item[1], str(item[0])))
    ]


def _coverage_baseline(same_report: Mapping[str, Any], quality_report: Mapping[str, Any]) -> tuple[float, int, dict[str, Any]]:
    coverage = same_report.get("coverage", {}).get("after_same_transversal", {})
    if not coverage:
        coverage = quality_report.get("coverage", {}).get("after_same_transversal", {})
    confidence = coverage.get("confidence", {})
    reconstructed = int(confidence.get("RECONSTRUCTED_HIGH", 0)) + int(confidence.get("RECONSTRUCTED_MEDIUM", 0))
    denominator = int(coverage.get("projected_coverage_cases", 0) or 0)
    if not denominator:
        denominator = int(same_report.get("coverage", {}).get("after_same_transversal", {}).get("projected_coverage_cases", 0) or 0)
    current = coverage.get("projected_coverage_with_reconstructed_pct")
    if current is None and denominator:
        official = float(coverage.get("official_coverage_pct", 0) or 0)
        current = official + 100 * reconstructed / denominator
    return float(current or 0), denominator, {
        "reconstructed_high": int(confidence.get("RECONSTRUCTED_HIGH", 0)),
        "reconstructed_medium": int(confidence.get("RECONSTRUCTED_MEDIUM", 0)),
        "reconstructed_current": reconstructed,
        "projected_coverage_cases": denominator,
    }


def build_report(
    cases: pd.DataFrame,
    pareto: pd.DataFrame,
    reconciliation: Mapping[str, Any],
    same_report: Mapping[str, Any],
    quality_report: Mapping[str, Any],
) -> dict[str, Any]:
    current_coverage, denominator, baseline = _coverage_baseline(same_report, quality_report)
    recommendations = recommend_heuristics(cases)
    scenarios = simulate_scenarios(cases, current_coverage, denominator)
    assigned = assign_recommended_heuristic(cases, recommendations)
    cases["recommended_next_heuristic"] = assigned

    human_total = int(cases["review_decision"].map(_text).ne("").sum()) if "review_decision" in cases else 0
    human_approved = int((cases["review_decision"].map(_text).ne("") & cases["review_approved"].map(_bool)).sum()) if "review_decision" in cases else 0
    dimensions = {
        "strategy_selected": _distribution(cases["strategy_selected"]),
        "categoria_falha_atual": _distribution(cases["categoria_falha_atual"]),
        "de_status": _distribution(cases["de_status"]),
        "ate_status": _distribution(cases["ate_status"]),
        "promotion_potential": _distribution(cases["promotion_potential"]),
        "component_count": _distribution(cases["component_count"]),
        "loop": _distribution(cases["loop"]),
        "snap_used": _distribution(cases["snap_used"]),
        "reviewed": human_total,
        "approved": human_approved,
        "approval_rate_observed_pct": _round(100 * human_approved / human_total if human_total else None),
        "review_sample_flag": "LOW_SAMPLE_SIZE" if 0 < human_total < LOW_SAMPLE_SIZE else "OK" if human_total else "NO_REVIEW",
    }
    root_causes = []
    for row in pareto.itertuples():
        root_causes.append(
            {
                "root_cause": row.root_cause,
                "description": ROOT_CAUSE_LABELS.get(row.root_cause, "Observed source diagnostic cause"),
                "count": int(row.count),
                "percentage": _round(row.percentage),
                "cumulative_percentage": _round(row.cumulative_percentage),
                "promotion_potential": row.promotion_potential,
                "promotion_potential_distribution": json.loads(row.promotion_potential_distribution),
                "avg_score": row.avg_score,
                "median_score": row.median_score,
                "avg_extension_deviation": row.avg_extension_deviation,
                "avg_gps_distance": row.avg_gps_distance,
                "avg_snap_distance": row.avg_snap_distance,
                "with_de": int(row.with_de),
                "with_ate": int(row.with_ate),
                "with_both": int(row.with_both),
                "with_none": int(row.with_none),
                "multiple_components": int(row.multiple_components),
                "loops": int(row.loops),
                "critical_warnings": int(row.critical_warnings),
                "human_reviewed": int(row.human_reviewed),
                "human_approved": int(row.human_approved),
                "human_approval_rate_observed_pct": row.human_approval_rate,
                "human_review_sample_flag": row.human_review_sample_flag,
            }
        )
    return {
        "analysis_type": "diagnostic_only",
        "analysis_version": "route-geometry-estimated-pareto-v1",
        "total_estimated": int(len(cases)),
        "expected_estimated": EXPECTED_ESTIMATED,
        "population_reconciliation": dict(reconciliation),
        "pareto_50": pareto_cut(pareto, 50),
        "pareto_80": pareto_cut(pareto, 80),
        "pareto_90": pareto_cut(pareto, 90),
        "root_causes": root_causes,
        "secondary_cause_ranking": _token_ranking(cases["secondary_causes"], len(cases)),
        "strategy_ranking": _value_ranking(cases["strategy_selected"], len(cases)),
        "dimensions": dimensions,
        "promotion_potential_distribution": _distribution(cases["promotion_potential"]),
        "coverage_baseline": baseline | {"coverage_without_estimated_pct": _round(current_coverage)},
        "scenarios": scenarios["scenarios"],
        "scenario_assumption": scenarios["assumption"],
        "recommended_heuristics": recommendations,
        "heuristic_cohort_overlap_note": "Recommended heuristic cohorts are diagnostic cohorts and may overlap; case-level assignment uses the first matching ranked cohort.",
        "official_outputs_changed": False,
        "geometry_promoted": False,
    }


def _write_csv(frame: pd.DataFrame, path: Path, columns: list[str]) -> None:
    output = frame.copy()
    for column in columns:
        if column not in output:
            output[column] = None
    output[columns].to_csv(path, index=False, encoding="utf-8-sig")


def run_analysis(
    quality_shadow_path: Path | str = QUALITY_SHADOW_PATH,
    same_transversal_path: Path | str = SAME_TRANSVERSAL_PATH,
    human_review_path: Path | str = HUMAN_REVIEW_PATH,
    quality_report_path: Path | str = QUALITY_REPORT_PATH,
    same_report_path: Path | str = SAME_TRANSVERSAL_REPORT_PATH,
    pareto_path: Path | str = PARETO_PATH,
    cases_path: Path | str = CASES_PATH,
    report_path: Path | str = REPORT_PATH,
) -> dict[str, Any]:
    cases, reconciliation = load_effective_estimated_cases(quality_shadow_path, same_transversal_path, human_review_path)
    pareto = build_pareto(cases)
    report = build_report(cases, pareto, reconciliation, _read_json(Path(same_report_path)), _read_json(Path(quality_report_path)))

    pareto_columns = [
        "root_cause", "count", "percentage", "cumulative_percentage", "promotion_potential", "avg_score",
        "median_score", "avg_extension_deviation", "avg_gps_distance", "avg_snap_distance", "with_de", "with_ate",
        "with_both", "with_none", "multiple_components", "loops", "critical_warnings", "human_reviewed",
        "human_approved", "human_approval_rate",
    ]
    cases["strategy"] = cases["strategy_selected"]
    case_columns = [
        "id", "via", "de", "ate", "strategy", "root_cause", "secondary_causes", "promotion_potential",
        "score", "extension_deviation", "gps_distance", "snap_distance", "component_count", "loop", "warnings",
        "candidate_count", "recommended_next_heuristic",
    ]
    pareto_path = Path(pareto_path)
    cases_path = Path(cases_path)
    report_path = Path(report_path)
    pareto_path.parent.mkdir(parents=True, exist_ok=True)
    cases_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    _write_csv(pareto, pareto_path, pareto_columns)
    _write_csv(cases, cases_path, case_columns)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the diagnostic ESTIMATED Pareto analysis")
    parser.add_argument("--quality-shadow", type=Path, default=QUALITY_SHADOW_PATH)
    parser.add_argument("--same-transversal", type=Path, default=SAME_TRANSVERSAL_PATH)
    parser.add_argument("--human-review", type=Path, default=HUMAN_REVIEW_PATH)
    args = parser.parse_args()
    report = run_analysis(args.quality_shadow, args.same_transversal, args.human_review)
    print(json.dumps({
        "total_estimated": report["total_estimated"],
        "promotion_potential_distribution": report["promotion_potential_distribution"],
        "pareto_50": report["pareto_50"],
        "pareto_80": report["pareto_80"],
        "pareto_90": report["pareto_90"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
