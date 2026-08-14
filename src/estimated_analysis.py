"""Exploratory, shadow-only analysis of ESTIMATED geometry cases.

This module mines the existing quality-shadow artifact. It does not instantiate
RoadGraph, call StreetResolver, run the ETL, or modify official outputs.
Candidate rules in this file are simulations of research hypotheses only.
They must not be treated as production heuristics.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Callable

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
PROCESSED = ROOT / "data" / "processed"
QUALITY_SHADOW = PROCESSED / "route_geometry_quality_shadow.csv"
HUMAN_REVIEW = PROCESSED / "route_geometry_human_review.csv"
QUALITY_REPORT = PROCESSED / "route_geometry_quality_report.json"
CLUSTERS_OUTPUT = PROCESSED / "estimated_clusters.csv"
PATTERNS_OUTPUT = PROCESSED / "estimated_patterns.csv"
SIMULATION_OUTPUT = PROCESSED / "estimated_simulation_report.json"

OFFICIAL_OUTPUTS = (
    "recape_clean.csv",
    "notificacoes.csv",
    "cruzamento.csv",
    "recapes_sem_cobertura.csv",
    "geosampa_coverage_report.json",
    "pipeline_run.json",
)
MIN_VALIDATION_LABELS = 30


def _read_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, encoding="utf-8-sig", dtype=str, low_memory=False)


def _number(frame: pd.DataFrame, column: str) -> pd.Series:
    return pd.to_numeric(frame.get(column, pd.Series(index=frame.index)), errors="coerce")


def _bool(frame: pd.DataFrame, column: str) -> pd.Series:
    return frame.get(column, pd.Series(index=frame.index, dtype=object)).fillna("").astype(str).str.lower().isin(
        {"true", "1", "yes", "sim"}
    )


def _bucket(value: Any, bands: tuple[tuple[float, str], ...], missing: str = "MISSING") -> str:
    if value is None or pd.isna(value):
        return missing
    for upper, label in bands:
        if float(value) <= upper:
            return label
    return bands[-1][1].replace("<=", ">")


def _via_type(value: Any) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return "MISSING"
    first = re.split(r"\s+", text)[0]
    aliases = {
        "AV": "AVENIDA",
        "AVEN": "AVENIDA",
        "ROD": "RODOVIA",
        "EST": "ESTRADA",
        "TR": "TRAVESSA",
        "PCA": "PRACA",
        "PC": "PRACA",
    }
    return aliases.get(first, first)


def _hash_file(path: Path) -> str | None:
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _official_signatures() -> dict[str, str | None]:
    return {name: _hash_file(PROCESSED / name) for name in OFFICIAL_OUTPUTS}


def _json_default(value: Any) -> Any:
    if isinstance(value, (pd.Timestamp,)):
        return value.isoformat()
    if isinstance(value, (float,)) and math.isnan(value):
        return None
    if hasattr(value, "item"):
        return value.item()
    raise TypeError(f"Unsupported JSON value: {type(value)!r}")


def _json_safe(value: Any) -> Any:
    """Convert pandas/numpy missing values to strict JSON nulls."""
    if value is None or value is pd.NA:
        return None
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item"):
        return _json_safe(value.item())
    return value


def _wilson(successes: int, trials: int) -> tuple[float | None, float | None]:
    if trials <= 0:
        return None, None
    z = 1.959963984540054
    observed = successes / trials
    denominator = 1 + z * z / trials
    center = (observed + z * z / (2 * trials)) / denominator
    margin = z * math.sqrt((observed * (1 - observed) + z * z / (4 * trials)) / trials) / denominator
    return max(0.0, center - margin), min(1.0, center + margin)


def _prepare(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    result["via_type"] = result["via"].map(_via_type)
    result["has_de"] = result["de"].fillna("").astype(str).str.strip().ne("")
    result["has_ate"] = result["ate"].fillna("").astype(str).str.strip().ne("")
    result["has_extension"] = _number(result, "extensao_m").notna()
    result["has_gps"] = _number(result, "latitude").notna() & _number(result, "longitude").notna()
    result["has_alternatives"] = result["alternatives_json"].fillna("").astype(str).str.strip().ne("") & result[
        "alternatives_json"
    ].fillna("").astype(str).ne("[]")
    result["alternative_count"] = result["alternatives_json"].map(_alternative_count)
    result["warning_loop"] = result["warnings"].fillna("").astype(str).str.contains("LOOP_DETECTADO", regex=False)
    result["warning_large_deviation"] = result["warnings"].fillna("").astype(str).str.contains(
        "DESVIO_EXTENSAO_ACIMA_50_PCT", regex=False
    ) | _number(result, "extension_deviation_pct").gt(50)
    result["candidate_count_num"] = _number(result, "candidate_count")
    result["segment_count_num"] = _number(result, "segment_count")
    result["component_count_num"] = _number(result, "component_count")
    result["score_num"] = _number(result, "geometry_score")
    result["deviation_num"] = _number(result, "extension_deviation_pct")
    result["gap_num"] = _number(result, "max_gap_m")
    result["gps_distance_num"] = pd.concat(
        [_number(result, "snap_distance_de_m"), _number(result, "snap_distance_ate_m")], axis=1
    ).max(axis=1, skipna=True)
    result["reference_distance_num"] = _number(result, "main_reference_distance_m")
    result["candidate_bucket"] = result["candidate_count_num"].map(
        lambda value: _bucket(value, ((1, "1"), (2, "2"), (5, "3-5"), (10, "6-10"), (math.inf, "11+")))
    )
    result["segment_bucket"] = result["segment_count_num"].map(
        lambda value: _bucket(value, ((3, "1-3"), (10, "4-10"), (25, "11-25"), (100, "26-100"), (math.inf, "101+")))
    )
    result["component_bucket"] = result["component_count_num"].map(
        lambda value: _bucket(value, ((1, "1"), (2, "2"), (5, "3-5"), (math.inf, "6+")))
    )
    result["length_bucket"] = _number(result, "path_length_m").map(
        lambda value: _bucket(value, ((50, "<=50m"), (250, "51-250m"), (1000, "251-1000m"), (math.inf, ">1000m")))
    )
    result["deviation_bucket"] = result["deviation_num"].map(
        lambda value: _bucket(value, ((10, "<=10%"), (25, "11-25%"), (50, "26-50%"), (math.inf, ">50%")))
    )
    result["gps_distance_bucket"] = result["reference_distance_num"].map(
        lambda value: _bucket(value, ((1, "<=1m"), (5, "1-5m"), (25, "6-25m"), (math.inf, ">25m")))
    )
    result["risk_flag"] = result["warning_loop"] | result["warning_large_deviation"] | result["component_count_num"].gt(1)
    return result


def _alternative_count(value: Any) -> int:
    try:
        parsed = json.loads(str(value or "[]"))
        return len(parsed) if isinstance(parsed, list) else 0
    except (TypeError, ValueError, json.JSONDecodeError):
        return 0


def _cluster_table(frame: pd.DataFrame) -> pd.DataFrame:
    dimensions = [
        "root_cause_primary",
        "strategy_selected",
        "topology_status",
        "component_status",
        "candidate_bucket",
        "segment_bucket",
        "deviation_bucket",
    ]
    grouped = frame.groupby(dimensions, dropna=False, sort=False)
    rows: list[dict[str, Any]] = []
    for index, group in grouped:
        values = dict(zip(dimensions, index if isinstance(index, tuple) else (index,)))
        values.update(
            {
                "case_count": int(len(group)),
                "share_pct": round(len(group) / len(frame) * 100, 4),
                "median_score": _safe_median(group["score_num"]),
                "median_path_length_m": _safe_median(_number(group, "path_length_m")),
                "median_reference_distance_m": _safe_median(group["reference_distance_num"]),
                "median_candidate_count": _safe_median(group["candidate_count_num"]),
                "median_component_count": _safe_median(group["component_count_num"]),
                "has_de_pct": round(group["has_de"].mean() * 100, 4),
                "has_ate_pct": round(group["has_ate"].mean() * 100, 4),
                "has_extension_pct": round(group["has_extension"].mean() * 100, 4),
                "has_gps_pct": round(group["has_gps"].mean() * 100, 4),
                "loop_pct": round(group["warning_loop"].mean() * 100, 4),
                "large_deviation_pct": round(group["warning_large_deviation"].mean() * 100, 4),
                "dominant_via_type": _mode(group["via_type"]),
                "dominant_de_status": _mode(group["de_status"]),
                "dominant_ate_status": _mode(group["ate_status"]),
            }
        )
        rows.append(values)
    result = pd.DataFrame(rows).sort_values(["case_count", "median_score"], ascending=[False, False])
    result.insert(0, "cluster_id", [f"EST-{index:04d}" for index in range(1, len(result) + 1)])
    return result.reset_index(drop=True)


def _safe_median(values: pd.Series) -> float | None:
    numeric = pd.to_numeric(values, errors="coerce").dropna()
    return round(float(numeric.median()), 6) if not numeric.empty else None


def _mode(values: pd.Series) -> str | None:
    counts = values.fillna("MISSING").astype(str).value_counts()
    return str(counts.index[0]) if not counts.empty else None


def _load_review_labels(estimated: pd.DataFrame) -> pd.DataFrame:
    if not HUMAN_REVIEW.exists():
        return estimated.assign(review_label=pd.NA, review_labeled=False)
    review = _read_csv(HUMAN_REVIEW)
    review = review.drop_duplicates("id", keep="last")
    selected = review.reindex(columns=["id", "decision", "approved", "confidence_class"])
    result = estimated.merge(selected, on="id", how="left")
    result["review_label"] = pd.NA
    result.loc[result["decision"].eq("APROVAR_GEOMETRIA") & result["approved"].astype(str).str.lower().eq("true"), "review_label"] = "APPROVED"
    result.loc[result["decision"].eq("REJEITAR_GEOMETRIA"), "review_label"] = "REJECTED"
    result["review_labeled"] = result["review_label"].notna()
    return result


def _safe_predicate(frame: pd.DataFrame, predicate: Callable[[pd.DataFrame], pd.Series]) -> pd.Series:
    result = predicate(frame)
    return result.reindex(frame.index, fill_value=False).fillna(False).astype(bool)


def _pattern_rows(frame: pd.DataFrame, thresholds: dict[str, float]) -> tuple[pd.DataFrame, dict[str, pd.Series]]:
    score = frame["score_num"]
    clean = ~frame["risk_flag"] & frame["component_count_num"].eq(1) & (
        frame["gap_num"].isna() | frame["gap_num"].le(2)
    )
    resolved_context = frame["de_status"].isin({"EXATO", "FUZZY", "GEOMETRIC_INTERSECTION", "GEOMETRIC_SNAP"}) & frame[
        "ate_status"
    ].isin({"EXATO", "FUZZY", "GEOMETRIC_INTERSECTION", "GEOMETRIC_SNAP"})

    masks: dict[str, pd.Series] = {
        "clean_context_single_component": resolved_context & clean & frame["candidate_count_num"].le(3) & score.ge(thresholds["score_median"]),
        "clean_nearest_single_candidate": frame["strategy_selected"].eq("NEAREST_SEGMENT_ESTIMATED") & frame[
            "candidate_count_num"
        ].eq(1) & clean & score.ge(thresholds["score_p75"]),
        "low_deviation_coordinate_extension": frame["strategy_selected"].str.contains("COORD_EXTENSION", na=False)
        & frame["deviation_num"].notna()
        & frame["deviation_num"].le(thresholds["deviation_p25"])
        & clean
        & score.ge(thresholds["score_median"]),
        "high_evidence_candidate": resolved_context
        & clean
        & frame["candidate_count_num"].eq(1)
        & score.ge(95)
        & frame["topology_status"].isin({"SAME_TRANSVERSAL_INTERSECTIONS", "GEOMETRIC_INTERSECTIONS"}),
        "gps_snap_actionable": frame["strategy_selected"].str.contains("GPS_SNAP", na=False)
        & frame["gps_distance_num"].notna()
        & frame["gps_distance_num"].le(thresholds["gps_distance_p75"])
        & clean
        & score.ge(thresholds["score_median"]),
        "unsafe_never_promote": frame["risk_flag"]
        | frame["root_cause_primary"].isin({"TRANSVERSAL_INEXISTENTE", "APENAS_UMA_TRANSVERSAL_CONHECIDA"}),
    }
    metadata = {
        "clean_context_single_component": {
            "kind": "PROMOTION_PROPOSAL",
            "description": "Casos com De e Até resolvidos, componente único, baixa ambiguidade e sem alertas estruturais.",
            "proposed_class": "MEDIUM",
            "complexity": 2,
            "risk": "MEDIUM",
            "dependencies": "existing quality-shadow fields; human review",
            "confidence": "LOW_UNVALIDATED",
        },
        "clean_nearest_single_candidate": {
            "kind": "PROMOTION_PROPOSAL",
            "description": "Fallback de segmento único próximo, score no quartil superior e sem alerta de loop/desvio/componente.",
            "proposed_class": "MEDIUM",
            "complexity": 1,
            "risk": "MEDIUM_HIGH",
            "dependencies": "GPS evidence; topology review",
            "confidence": "LOW_UNVALIDATED",
        },
        "low_deviation_coordinate_extension": {
            "kind": "PROMOTION_PROPOSAL",
            "description": "Extensões coordenadas com desvio no quartil inferior observado e componente único.",
            "proposed_class": "MEDIUM",
            "complexity": 2,
            "risk": "MEDIUM_HIGH",
            "dependencies": "extension field; GPS evidence; human review",
            "confidence": "LOW_UNVALIDATED",
        },
        "high_evidence_candidate": {
            "kind": "PROMOTION_PROPOSAL",
            "description": "Candidato potencialmente HIGH somente se houver interseções explícitas, score alto e ausência de alertas.",
            "proposed_class": "HIGH",
            "complexity": 3,
            "risk": "HIGH",
            "dependencies": "confirmed topology; independent validation set",
            "confidence": "NO_OBSERVED_CASES",
        },
        "gps_snap_actionable": {
            "kind": "PROMOTION_PROPOSAL",
            "description": "GPS snap com distância no quartil observado e estrutura sem alertas.",
            "proposed_class": "MEDIUM",
            "complexity": 2,
            "risk": "HIGH",
            "dependencies": "GPS quality; topology review",
            "confidence": "NO_OBSERVED_CASES",
        },
        "unsafe_never_promote": {
            "kind": "EXCLUSION_PATTERN",
            "description": "Loops, desvios altos, múltiplos componentes ou transversais ausentes.",
            "proposed_class": "HOLD_ESTIMATED",
            "complexity": 1,
            "risk": "LOW",
            "dependencies": "existing warning fields",
            "confidence": "STRUCTURAL",
        },
    }
    rows: list[dict[str, Any]] = []
    for name, mask in masks.items():
        selected = frame.loc[mask].copy()
        labels = selected.loc[selected["review_labeled"]]
        approved = int(labels["review_label"].eq("APPROVED").sum())
        rejected = int(labels["review_label"].eq("REJECTED").sum())
        labeled = approved + rejected
        ci_low, ci_high = _wilson(approved, labeled)
        observed_precision = approved / labeled if labeled else None
        total_approved = int(frame["review_label"].eq("APPROVED").sum())
        observed_recall = approved / total_approved if total_approved else None
        valid_precision = observed_precision if labeled >= MIN_VALIDATION_LABELS else None
        valid_recall = observed_recall if total_approved >= MIN_VALIDATION_LABELS else None
        row = {
            "pattern_name": name,
            **metadata[name],
            "cases_affected": int(len(selected)),
            "share_of_estimated_pct": round(len(selected) / len(frame) * 100, 4),
            "reviewed_cases": labeled,
            "reviewed_approved": approved,
            "reviewed_rejected": rejected,
            "observed_review_precision": _round_or_none(observed_precision),
            "precision_ci95_low": _round_or_none(ci_low),
            "precision_ci95_high": _round_or_none(ci_high),
            "precision_estimate": _round_or_none(valid_precision),
            "recall_estimate": _round_or_none(valid_recall),
            "minimum_validation_cases": MIN_VALIDATION_LABELS,
            "validation_status": "VALIDATED" if labeled >= MIN_VALIDATION_LABELS else "INSUFFICIENT_LABELS",
            "possible_errors": int(len(selected) - labeled + rejected),
            "naive_upper_bound_gain_cases": int(len(selected)) if name != "unsafe_never_promote" else 0,
            "naive_upper_bound_gain_pct_points": _round_or_none(
                len(selected) / thresholds["total_records"] * 100 if name != "unsafe_never_promote" else 0
            ),
            "screening_roi": _round_or_none(len(selected) / metadata[name]["complexity"]),
        }
        rows.append(row)
    result = pd.DataFrame(rows)
    result["_kind_order"] = result["kind"].eq("EXCLUSION_PATTERN").astype(int)
    result = result.sort_values(["_kind_order", "screening_roi"], ascending=[True, False]).drop(columns="_kind_order")
    return result.reset_index(drop=True), masks


def _round_or_none(value: Any, digits: int = 6) -> float | None:
    return round(float(value), digits) if value is not None and not pd.isna(value) else None


def _distribution(frame: pd.DataFrame, column: str) -> list[dict[str, Any]]:
    counts = frame[column].fillna("MISSING").astype(str).value_counts(dropna=False)
    return [{"value": str(index), "cases": int(value), "share_pct": round(value / len(frame) * 100, 4)} for index, value in counts.items()]


def run() -> dict[str, Any]:
    before_signatures = _official_signatures()
    if not QUALITY_SHADOW.exists():
        raise FileNotFoundError(f"Missing shadow input: {QUALITY_SHADOW}")
    quality = _read_csv(QUALITY_SHADOW)
    estimated = _prepare(quality[quality["geometry_confidence"].eq("ESTIMATED")].copy())
    estimated = _load_review_labels(estimated)
    total_records = 0
    official_geometry_count = 0
    if QUALITY_REPORT.exists():
        report = json.loads(QUALITY_REPORT.read_text(encoding="utf-8"))
        total_records = int(report.get("scope", {}).get("total_recapes", 0) or 0)
        official_geometry_count = int(report.get("scope", {}).get("official_geometry_count", 0) or 0)
    total_records = total_records or len(quality)
    thresholds = {
        "score_median": float(estimated["score_num"].median()),
        "score_p75": float(estimated["score_num"].quantile(0.75)),
        "deviation_p25": float(estimated["deviation_num"].dropna().quantile(0.25)) if estimated["deviation_num"].notna().any() else 0.0,
        "gps_distance_p75": float(estimated["gps_distance_num"].dropna().quantile(0.75)) if estimated["gps_distance_num"].notna().any() else 0.0,
        "total_records": total_records,
    }
    clusters, _ = _cluster_table(estimated), None
    pattern_table, masks = _pattern_rows(estimated, thresholds)
    cluster_path = CLUSTERS_OUTPUT.with_suffix(".tmp")
    pattern_path = PATTERNS_OUTPUT.with_suffix(".tmp")
    simulation_path = SIMULATION_OUTPUT.with_suffix(".tmp")
    clusters.to_csv(cluster_path, index=False, encoding="utf-8-sig")
    pattern_table.to_csv(pattern_path, index=False, encoding="utf-8-sig")
    promotion_names = [name for name in masks if name != "unsafe_never_promote"]
    promotion_union = pd.concat([masks[name].rename(name) for name in promotion_names], axis=1).any(axis=1)
    unsafe = masks["unsafe_never_promote"]
    after_shadow_cases = official_geometry_count + int(promotion_union.sum())
    after_shadow_pct = after_shadow_cases / total_records * 100 if total_records else None
    after_signatures = _official_signatures()
    report = {
        "analysis_version": "estimated-pattern-mining-v1",
        "mode": "shadow_only_exploratory",
        "source": str(QUALITY_SHADOW.relative_to(ROOT)),
        "estimated_cases": int(len(estimated)),
        "quality_shadow_cases": int(len(quality)),
        "total_records": total_records,
        "official_geometry_count": official_geometry_count,
        "official_coverage_pct": _round_or_none(official_geometry_count / total_records * 100 if total_records else None),
        "thresholds_observed": {key: _round_or_none(value) if key != "total_records" else value for key, value in thresholds.items()},
        "distributions": {
            "root_cause_primary": _distribution(estimated, "root_cause_primary"),
            "status_atual": _distribution(estimated, "status_atual"),
            "via_type": _distribution(estimated, "via_type"),
            "strategy_selected": _distribution(estimated, "strategy_selected"),
            "topology_status": _distribution(estimated, "topology_status"),
            "component_status": _distribution(estimated, "component_status"),
            "candidate_bucket": _distribution(estimated, "candidate_bucket"),
            "segment_bucket": _distribution(estimated, "segment_bucket"),
            "length_bucket": _distribution(estimated, "length_bucket"),
            "deviation_bucket": _distribution(estimated, "deviation_bucket"),
            "gps_distance_bucket": _distribution(estimated, "gps_distance_bucket"),
        },
        "patterns": [_json_safe(row) for row in pattern_table.to_dict(orient="records")],
        "simulation": {
            "promotion_patterns": promotion_names,
            "cases_affected_union": int(promotion_union.sum()),
            "overlap_cases": int(sum(masks[name].sum() for name in promotion_names) - promotion_union.sum()),
            "unsafe_hold_estimated_cases": int(unsafe.sum()),
            "simulated_promotions": int(promotion_union.sum()),
            "simulated_demotions": 0,
            "possible_errors": int(estimated.loc[promotion_union, "review_labeled"].eq(False).sum() + estimated.loc[promotion_union, "review_label"].eq("REJECTED").sum()),
            "naive_upper_bound_coverage_pct": _round_or_none(after_shadow_pct),
            "naive_upper_bound_gain_pct_points": _round_or_none(after_shadow_pct - (official_geometry_count / total_records * 100) if total_records else None),
            "interpretation": "Upper bound only; no candidate is promoted in official data.",
        },
        "validation": {
            "estimated_cases_with_human_labels": int(estimated["review_labeled"].sum()),
            "approved_labels": int(estimated["review_label"].eq("APPROVED").sum()),
            "rejected_labels": int(estimated["review_label"].eq("REJECTED").sum()),
            "minimum_validation_cases": MIN_VALIDATION_LABELS,
            "precision_recall_usable": False,
            "reason": "Only five ESTIMATED cases have human labels; this is insufficient for population-level precision or recall.",
        },
        "limitations": [
            "The quality-shadow file does not contain an explicit intersection-count field; candidate/component fields are used as available proxies.",
            "Human review labels are sparse and selection-biased, so observed approval rates are not production precision.",
            "No ground-truth geometry dataset is available for recall estimation.",
            "Rules in the simulation are hypotheses for review, not implemented heuristics.",
        ],
        "official_outputs_unchanged": before_signatures == after_signatures,
        "official_output_sha256_before": before_signatures,
        "official_output_sha256_after": after_signatures,
    }
    simulation_path.write_text(
        json.dumps(_json_safe(report), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
    )
    cluster_path.replace(CLUSTERS_OUTPUT)
    pattern_path.replace(PATTERNS_OUTPUT)
    simulation_path.replace(SIMULATION_OUTPUT)
    return report


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2, default=_json_default))
