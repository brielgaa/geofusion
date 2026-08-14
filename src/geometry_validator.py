"""Independent, shadow-only validation of geometries currently marked ESTIMATED.

This module deliberately has no dependency on the route generators, StreetResolver,
ETL, dashboard, or official output writers.  It reads their persisted artifacts and
produces recommendations only.  The RoadGraph is loaded as a read-only spatial
reference; no route or resolver method is called here.

The command line entry point is intentionally explicit::

    python src/geometry_validator.py --shadow

The calibration population is made from paths already present in ``recape_clean``
(positive controls) and deterministic perturbations of a held-out subset (negative
controls).  Thresholds used for classification are reported in the JSON artifact.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import pickle
import re
import sys
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd
from pyproj import Transformer
from shapely import affinity
from shapely.geometry import LineString, MultiLineString, Point
from shapely.ops import linemerge, transform, unary_union
from shapely.strtree import STRtree
from shapely.wkt import loads as load_wkt

try:  # The file is also executable as ``python src/geometry_validator.py``.
    from road_graph import RoadGraph
except ImportError:  # pragma: no cover - package-style import fallback
    from .road_graph import RoadGraph


ROOT = Path(__file__).resolve().parents[1]
PROCESSED = ROOT / "data" / "processed"
CACHE = ROOT / "data" / "cache"
SHADOW_INPUT = PROCESSED / "route_geometry_quality_shadow.csv"
OFFICIAL_INPUT = PROCESSED / "recape_clean.csv"
OUTPUT_CSV = PROCESSED / "geometry_validation_shadow.csv"
OUTPUT_REPORT = PROCESSED / "geometry_validation_report.json"
HUMAN_REVIEW = PROCESSED / "route_geometry_human_review.csv"
GRAPH_CACHE = CACHE / "geosampa_road_graph.pkl"
GRAPH_SOURCE = CACHE / "geosampa_segmento_logradouro.geojson"
MINIMUM_CASES = 30
VALIDATION_CLASSES = (
    "VALIDATED_HIGH",
    "VALIDATED_MEDIUM",
    "INSUFFICIENT_EVIDENCE",
    "REJECTED",
)


def normalize_name(value: Any) -> str:
    """Small, deterministic comparison normalizer local to this module.

    It is intentionally not imported from the production resolver.  It removes
    presentation prefixes and accents, but does not apply aliases or fuzzy matches.
    """

    text = str(value or "").upper().strip()
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.split(r"\s+-\s+|,\s*|/\s*|\s+\(", text, maxsplit=1)[0]
    text = re.sub(
        r"^(RUA|R\.?|AVENIDA|AV\.?|ALAMEDA|AL\.?|TRAVESSA|TV\.?|"
        r"ESTRADA|EST\.?|RODOVIA|ROD\.?|PRACA|PC\.?|LARGO|LGO\.?|"
        r"VIELA|VL\.?|VIADUTO|VD\.?)\s+",
        "",
        text,
    )
    text = re.sub(r"[^A-Z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _float(value: Any) -> float | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    try:
        result = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _text(value: Any) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    return str(value).strip()


def _percentile(values: Iterable[float], percentile: float, fallback: float | None = None) -> float | None:
    data = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    if not data:
        return fallback
    return float(np.percentile(data, percentile))


def _wilson(successes: int, total: int, z: float = 1.96) -> tuple[float | None, float | None]:
    if total <= 0:
        return None, None
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    radius = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / denominator
    return max(0.0, centre - radius), min(1.0, centre + radius)


def _safe_json(value: Any, default: Any = None) -> Any:
    if isinstance(value, (dict, list, tuple)):
        return value
    text = _text(value)
    if not text or text.lower() in {"nan", "none", "null"}:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def parse_geometry(value: Any):
    if not _text(value):
        return None
    try:
        geometry = load_wkt(_text(value))
    except Exception:
        return None
    return geometry if geometry is not None and not geometry.is_empty else None


def _line_parts(geometry) -> list[LineString]:
    if geometry is None or geometry.is_empty:
        return []
    if isinstance(geometry, LineString):
        return [geometry]
    if isinstance(geometry, MultiLineString):
        return [part for part in geometry.geoms if not part.is_empty]
    return [part for part in getattr(geometry, "geoms", ()) if isinstance(part, LineString)]


def _line_geometry(geometry):
    parts = _line_parts(geometry)
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    merged = linemerge(parts)
    return merged if not merged.is_empty else geometry


def _metric_path_from_official(value: Any):
    payload = _safe_json(value)
    if not isinstance(payload, list) or len(payload) < 2:
        return None
    coordinates = []
    for item in payload:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        lon, lat = _float(item[0]), _float(item[1])
        if lon is not None and lat is not None:
            coordinates.append((lon, lat))
    if len(coordinates) < 2:
        return None
    try:
        geographic = LineString(coordinates)
        return transform(WGS84_TO_METRIC.transform, geographic)
    except Exception:
        return None


WGS84_TO_METRIC = Transformer.from_crs("EPSG:4326", "EPSG:31983", always_xy=True)
METRIC_TO_WGS84 = Transformer.from_crs("EPSG:31983", "EPSG:4326", always_xy=True)


@dataclass
class GeometryValidationContext:
    record_id: str
    geometry: Any
    main_street: str = ""
    via: str = ""
    de: str = ""
    ate: str = ""
    codlog: str = ""
    latitude: float | None = None
    longitude: float | None = None
    extension_m: float | None = None
    candidate_count: int | None = None
    alternatives: list[Any] = field(default_factory=list)
    graph: Any = None
    calibration: "Calibration" | None = None
    source_type: str = "ESTIMATED"
    control_label: str = ""
    official_geometry: Any = None


@dataclass
class GeometryValidationEvidence:
    geometry_valid: bool = False
    geometry_type: str = ""
    length_m: float | None = None
    main_street_expected: str = ""
    main_street_available: bool = False
    main_alignment_ratio: float | None = None
    main_mean_distance_m: float | None = None
    main_max_distance_m: float | None = None
    nearby_main_segments: int = 0
    nearby_non_main_segments: int = 0
    codlog_change_count: int = 0
    street_name_change_count: int = 0
    continuity_gap_count: int = 0
    continuity_max_gap_m: float | None = None
    continuity_score: float | None = None
    self_intersection_count: int = 0
    loop_detected: bool = False
    component_count: int | None = None
    topology_score: float | None = None
    topology_status: str = "UNAVAILABLE"
    gps_distance_m: float | None = None
    gps_along_path_m: float | None = None
    gps_endpoint_distance_m: float | None = None
    gps_status: str = "UNAVAILABLE"
    extension_deviation_pct: float | None = None
    extension_band: str = "UNAVAILABLE"
    de_status: str = "UNAVAILABLE"
    ate_status: str = "UNAVAILABLE"
    de_distance_m: float | None = None
    ate_distance_m: float | None = None
    boundary_validation_score: float | None = None
    alternative_count: int = 0
    top2_margin: float | None = None
    competition_status: str = "UNAVAILABLE"
    evidence_count: int = 0
    hard_failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class GeometryValidationResult:
    record_id: str
    source_type: str
    control_label: str
    validation_class: str
    validation_score: float | None
    independent_evidence_count: int
    promotion_recommendation: str
    reason: str
    evidence: GeometryValidationEvidence

    def to_row(self) -> dict[str, Any]:
        row = {
            "id": self.record_id,
            "source_type": self.source_type,
            "control_label": self.control_label,
            "validation_class": self.validation_class,
            "validation_score_independent": self.validation_score,
            "independent_evidence_count": self.independent_evidence_count,
            "shadow_recommendation": self.promotion_recommendation,
            "validation_reason": self.reason,
        }
        row.update(asdict(self.evidence))
        row["hard_failures"] = "|".join(self.evidence.hard_failures)
        row["warnings"] = "|".join(self.evidence.warnings)
        return row


@dataclass
class Calibration:
    positive_calibration_count: int
    positive_validation_count: int
    negative_calibration_count: int
    negative_validation_count: int
    high_score_threshold: float
    medium_score_threshold: float
    high_evidence_threshold: int
    medium_evidence_threshold: int
    main_alignment_reject_ratio: float | None
    continuity_hard_gap_m: float | None
    boundary_hard_distance_m: float | None
    extension_hard_deviation_pct: float | None
    gps_on_path_tolerance_m: float | None
    gps_near_path_tolerance_m: float | None
    method: str = "empirical_control_quantiles"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class IndependentGeometryValidator:
    """Evidence validator that never generates or rewrites a route."""

    def __init__(self, graph=None, calibration: Calibration | None = None):
        self.graph = graph
        self.calibration = calibration
        self._street_union_cache: dict[str, Any] = {}
        self._street_ids_cache: dict[str, list[str]] = {}

    def _expected_street(self, context: GeometryValidationContext) -> str:
        # Prefer the raw expected street.  If its exact spelling is absent from
        # the read-only graph, use the persisted main-street label as a second
        # independent lookup candidate; no fuzzy matching or resolver aliases
        # are applied.
        raw = normalize_name(context.via)
        fallback = normalize_name(context.main_street)
        if self.graph is not None and raw and raw in self.graph.street_segments:
            return raw
        return fallback or raw

    def _street_union(self, street: str):
        if street in self._street_union_cache:
            return self._street_union_cache[street]
        if self.graph is None or not street:
            return None
        ids = list(self.graph.street_segments.get(street, ()))
        self._street_ids_cache[street] = ids
        geometries = [self.graph.segments[identifier].geometry for identifier in ids]
        result = unary_union(geometries) if geometries else None
        self._street_union_cache[street] = result
        return result

    def _nearby_segments(self, geometry, tolerance: float = 0.0) -> list[Any]:
        if self.graph is None or self.graph._tree is None or geometry is None:
            return []
        query = geometry.buffer(tolerance) if tolerance else geometry
        identifiers = self.graph._candidate_ids(query)
        return [self.graph.segments[identifier] for identifier in identifiers if identifier in self.graph.segments]

    @staticmethod
    def _sample_points(line, count: int = 21) -> list[Point]:
        if line is None or line.length <= 0:
            return []
        return [line.interpolate(line.length * index / (count - 1)) for index in range(count)]

    @staticmethod
    def _self_intersections(line) -> int:
        if not isinstance(line, LineString) or len(line.coords) < 4:
            return 0
        segments = [LineString([line.coords[index], line.coords[index + 1]]) for index in range(len(line.coords) - 1)]
        intersections: set[tuple[float, float]] = set()
        for left_index, left in enumerate(segments):
            for right_index in range(left_index + 2, len(segments)):
                if right_index == left_index + 1:
                    continue
                intersection = left.intersection(segments[right_index])
                if intersection.is_empty:
                    continue
                if intersection.geom_type == "Point":
                    intersections.add((round(intersection.x, 3), round(intersection.y, 3)))
                else:
                    for point in getattr(intersection, "geoms", ()):
                        if point.geom_type == "Point":
                            intersections.add((round(point.x, 3), round(point.y, 3)))
        return len(intersections)

    def _alignment(self, line, street: str, evidence: GeometryValidationEvidence) -> None:
        union = self._street_union(street)
        evidence.main_street_available = union is not None and not union.is_empty
        if not evidence.main_street_available:
            evidence.warnings.append("MAIN_STREET_NOT_AVAILABLE_IN_READ_ONLY_GRAPH")
            return
        points = self._sample_points(line)
        distances = [point.distance(union) for point in points]
        evidence.main_mean_distance_m = float(np.mean(distances)) if distances else None
        evidence.main_max_distance_m = max(distances) if distances else None
        try:
            overlap = line.intersection(union).length
            evidence.main_alignment_ratio = float(min(1.0, max(0.0, overlap / line.length))) if line.length else 0.0
        except Exception:
            evidence.main_alignment_ratio = 0.0
        nearby = self._nearby_segments(line, tolerance=3.0)
        evidence.nearby_main_segments = sum(segment.street_norm == street for segment in nearby)
        evidence.nearby_non_main_segments = sum(segment.street_norm != street for segment in nearby)
        codlogs = {str(segment.codlog).strip() for segment in nearby if str(segment.codlog).strip()}
        street_names = {normalize_name(segment.street_name) for segment in nearby if normalize_name(segment.street_name)}
        evidence.codlog_change_count = max(0, len(codlogs) - 1)
        evidence.street_name_change_count = max(0, len(street_names) - 1)

    def _continuity(self, line, street: str, evidence: GeometryValidationEvidence) -> None:
        if line is None:
            return
        evidence.self_intersection_count = self._self_intersections(line)
        evidence.loop_detected = evidence.self_intersection_count > 0 or not line.is_simple
        union = self._street_union(street)
        if union is not None and not union.is_empty:
            distances = [point.distance(union) for point in self._sample_points(line, count=41)]
            hard_gap = (self.calibration.continuity_hard_gap_m if self.calibration else None) or 5.0
            evidence.continuity_max_gap_m = max(distances) if distances else 0.0
            evidence.continuity_gap_count = sum(distance > hard_gap for distance in distances)
            evidence.continuity_score = round(max(0.0, 100.0 * (1.0 - evidence.continuity_max_gap_m / max(hard_gap, 1e-9))), 6)
        nearby = self._nearby_segments(line, tolerance=3.0)
        ids = {segment.identifier for segment in nearby if segment.street_norm == street}
        graph = self.graph.street_graphs.get(street) if self.graph is not None else None
        if graph is not None and ids:
            nodes = set()
            for identifier in ids:
                segment = self.graph.segments[identifier]
                nodes.update((segment.start, segment.end))
            subgraph = graph.subgraph(nodes)
            evidence.component_count = nx_number_connected_components(subgraph)
            evidence.topology_status = "SAME_COMPONENT" if evidence.component_count == 1 else "MULTIPLE_COMPONENTS"
            evidence.topology_score = 100.0 if evidence.component_count == 1 else 0.0
        elif evidence.main_street_available:
            evidence.topology_status = "GEOMETRIC_ALIGNMENT_ONLY"
            evidence.topology_score = 50.0

    def _gps(self, line, context: GeometryValidationContext, evidence: GeometryValidationEvidence) -> None:
        if context.latitude is None or context.longitude is None:
            return
        try:
            point = transform(WGS84_TO_METRIC.transform, Point(context.longitude, context.latitude))
            evidence.gps_distance_m = float(point.distance(line))
            projected = line.project(point)
            evidence.gps_along_path_m = float(projected)
            evidence.gps_endpoint_distance_m = float(min(projected, max(0.0, line.length - projected)))
            on = self.calibration.gps_on_path_tolerance_m if self.calibration else 5.0
            near = self.calibration.gps_near_path_tolerance_m if self.calibration else 15.0
            if evidence.gps_distance_m <= on:
                evidence.gps_status = "ON_PATH"
            elif evidence.gps_distance_m <= near:
                evidence.gps_status = "NEAR_PATH"
            else:
                evidence.gps_status = "OFF_PATH"
        except Exception as exc:  # pragma: no cover - defensive for malformed coordinates
            evidence.warnings.append(f"GPS_EVIDENCE_ERROR:{type(exc).__name__}")

    def _extension(self, line, context: GeometryValidationContext, evidence: GeometryValidationEvidence) -> None:
        expected = context.extension_m
        if expected is None or expected <= 0 or line is None:
            return
        evidence.extension_deviation_pct = abs(line.length - expected) / expected * 100.0
        value = evidence.extension_deviation_pct
        evidence.extension_band = (
            "<=10" if value <= 10 else "10-25" if value <= 25 else "25-50" if value <= 50 else ">50"
        )

    def _boundary(self, line, context: GeometryValidationContext, evidence: GeometryValidationEvidence) -> None:
        street = self._expected_street(context)
        if self.graph is None or not street:
            return
        for field_name, label in (("de", "de"), ("ate", "ate")):
            value = normalize_name(getattr(context, field_name))
            if not value:
                continue
            boundary = self._street_union(value)
            if boundary is None or boundary.is_empty:
                setattr(evidence, f"{label}_status", "UNAVAILABLE")
                continue
            parts = _line_parts(line)
            if not parts:
                setattr(evidence, f"{label}_status", "UNAVAILABLE")
                continue
            endpoint_coordinates = parts[0].coords[0] if label == "de" else parts[-1].coords[-1]
            endpoint = Point(endpoint_coordinates)
            distance = float(endpoint.distance(boundary))
            setattr(evidence, f"{label}_distance_m", distance)
            tolerance = (self.calibration.boundary_hard_distance_m if self.calibration else None) or 5.0
            status = "CONFIRMED" if distance <= tolerance else "CONTRADICTED"
            setattr(evidence, f"{label}_status", status)
        statuses = [evidence.de_status, evidence.ate_status]
        confirmed = sum(status == "CONFIRMED" for status in statuses)
        contradicted = sum(status == "CONTRADICTED" for status in statuses)
        evidence.boundary_validation_score = 0.0 if contradicted else 100.0 if confirmed == 2 else 50.0 if confirmed else None

    def _competition(self, line, context: GeometryValidationContext, evidence: GeometryValidationEvidence) -> None:
        alternatives = [candidate for candidate in context.alternatives if candidate is not None and not candidate.is_empty]
        evidence.alternative_count = len(alternatives)
        if not alternatives:
            return
        scores = [self._independent_raw_score(candidate, context, include_competition=False) for candidate in alternatives]
        selected = self._independent_raw_score(line, context, include_competition=False)
        evidence.top2_margin = float(selected - max(scores)) if scores else None
        evidence.competition_status = "CLEAR_MARGIN" if evidence.top2_margin and evidence.top2_margin > 0 else "LOW_MARGIN"
        if evidence.competition_status == "LOW_MARGIN":
            evidence.warnings.append("COMPETING_CANDIDATE_LOW_INDEPENDENT_MARGIN")
            if evidence.top2_margin is not None and evidence.top2_margin <= 0:
                evidence.hard_failures.append("COMPETING_CANDIDATE")

    def _independent_raw_score(self, line, context: GeometryValidationContext, include_competition: bool = True) -> float:
        evidence = GeometryValidationEvidence(
            geometry_valid=line is not None and not line.is_empty,
            geometry_type=getattr(line, "geom_type", ""),
            length_m=float(line.length) if line is not None else None,
            main_street_expected=self._expected_street(context),
        )
        if line is None or line.is_empty or not isinstance(line, (LineString, MultiLineString)):
            return 0.0
        line = _line_geometry(line)
        self._alignment(line, evidence.main_street_expected, evidence)
        self._continuity(line, evidence.main_street_expected, evidence)
        self._gps(line, context, evidence)
        self._extension(line, context, evidence)
        self._boundary(line, context, evidence)
        score = 15.0
        if evidence.main_alignment_ratio is not None:
            score += 25.0 * evidence.main_alignment_ratio
        if evidence.continuity_max_gap_m is not None:
            limit = (self.calibration.continuity_hard_gap_m if self.calibration else 5.0) or 5.0
            score += 15.0 if evidence.continuity_max_gap_m <= limit else 0.0
        if evidence.gps_status == "ON_PATH":
            score += 15.0
        elif evidence.gps_status == "NEAR_PATH":
            score += 8.0
        if evidence.extension_deviation_pct is not None:
            limit = (self.calibration.extension_hard_deviation_pct if self.calibration else 50.0) or 50.0
            score += 10.0 * max(0.0, 1.0 - evidence.extension_deviation_pct / max(limit, 1e-9))
        for value in (evidence.de_status, evidence.ate_status):
            score += 7.5 if value == "CONFIRMED" else 3.75 if value == "UNAVAILABLE" else 0.0
        if evidence.component_count == 1:
            score += 10.0
        elif evidence.topology_status == "GEOMETRIC_ALIGNMENT_ONLY":
            score += 5.0
        if include_competition and evidence.alternative_count:
            score += 5.0 if evidence.competition_status == "CLEAR_MARGIN" else 0.0
        return float(min(100.0, max(0.0, score)))

    def validate(self, context: GeometryValidationContext) -> GeometryValidationResult:
        evidence = GeometryValidationEvidence(
            geometry_valid=context.geometry is not None and not context.geometry.is_empty,
            geometry_type=getattr(context.geometry, "geom_type", ""),
            length_m=float(context.geometry.length) if context.geometry is not None else None,
            main_street_expected=self._expected_street(context),
        )
        line = _line_geometry(context.geometry)
        if line is None or not isinstance(line, (LineString, MultiLineString)):
            evidence.hard_failures.append("INVALID_OR_NON_LINEAR_GEOMETRY")
        else:
            if isinstance(context.geometry, MultiLineString):
                evidence.warnings.append("MULTIPART_GEOMETRY")
            self._alignment(line, evidence.main_street_expected, evidence)
            self._continuity(line, evidence.main_street_expected, evidence)
            self._gps(line, context, evidence)
            self._extension(line, context, evidence)
            self._boundary(line, context, evidence)
            # Construct a lightweight copy explicitly; copying a RoadGraph via
            # ``dataclasses.asdict`` would be both unnecessary and very large.
            context_for_competition = GeometryValidationContext(
                record_id=context.record_id, geometry=line, main_street=context.main_street,
                via=context.via, de=context.de, ate=context.ate, latitude=context.latitude,
                codlog=context.codlog,
                longitude=context.longitude, extension_m=context.extension_m,
                candidate_count=context.candidate_count, alternatives=context.alternatives,
                graph=context.graph, calibration=context.calibration, source_type=context.source_type,
                control_label=context.control_label, official_geometry=context.official_geometry,
            )
            self._competition(line, context_for_competition, evidence)

            if evidence.self_intersection_count or evidence.loop_detected:
                evidence.hard_failures.append("SELF_INTERSECTION_OR_LOOP")
            if evidence.component_count is not None and evidence.component_count > 1:
                evidence.hard_failures.append("MULTIPLE_TOPOLOGICAL_COMPONENTS")
            if (
                self.calibration and evidence.main_alignment_ratio is not None
                and self.calibration.main_alignment_reject_ratio is not None
                and evidence.main_alignment_ratio < self.calibration.main_alignment_reject_ratio
            ):
                evidence.hard_failures.append("INSUFFICIENT_MAIN_STREET_ALIGNMENT")
            if (
                self.calibration and evidence.continuity_max_gap_m is not None
                and self.calibration.continuity_hard_gap_m is not None
                and evidence.continuity_max_gap_m > self.calibration.continuity_hard_gap_m
            ):
                evidence.hard_failures.append("CONTINUITY_GAP_ABOVE_CONTROL_LIMIT")
            if (
                self.calibration and evidence.extension_deviation_pct is not None
                and self.calibration.extension_hard_deviation_pct is not None
                and evidence.extension_deviation_pct > self.calibration.extension_hard_deviation_pct
            ):
                evidence.hard_failures.append("EXTENSION_DEVIATION_ABOVE_CONTROL_LIMIT")
            if evidence.de_status == "CONTRADICTED" or evidence.ate_status == "CONTRADICTED":
                evidence.hard_failures.append("BOUNDARY_CONTRADICTION")

        score = self._independent_raw_score(line, context) if line is not None else 0.0
        known = [
            evidence.geometry_valid,
            evidence.main_alignment_ratio is not None,
            evidence.continuity_max_gap_m is not None,
            evidence.gps_status != "UNAVAILABLE",
            evidence.extension_deviation_pct is not None,
            evidence.de_status != "UNAVAILABLE",
            evidence.ate_status != "UNAVAILABLE",
            evidence.component_count is not None,
            evidence.alternative_count > 0,
        ]
        evidence.evidence_count = sum(bool(item) for item in known)
        if evidence.hard_failures:
            validation_class = "REJECTED"
            reason = ";".join(evidence.hard_failures)
        elif self.calibration is None:
            validation_class = "INSUFFICIENT_EVIDENCE"
            reason = "CALIBRATION_UNAVAILABLE"
        elif evidence.evidence_count < self.calibration.medium_evidence_threshold:
            validation_class = "INSUFFICIENT_EVIDENCE"
            reason = "EVIDENCE_COUNT_BELOW_EMPIRICAL_MINIMUM"
        elif score >= self.calibration.high_score_threshold and evidence.evidence_count >= self.calibration.high_evidence_threshold:
            validation_class = "VALIDATED_HIGH"
            reason = "INDEPENDENT_EVIDENCE_ABOVE_HIGH_CONTROL_QUANTILE"
        elif score >= self.calibration.medium_score_threshold:
            validation_class = "VALIDATED_MEDIUM"
            reason = "INDEPENDENT_EVIDENCE_ABOVE_MEDIUM_CONTROL_QUANTILE"
        else:
            validation_class = "INSUFFICIENT_EVIDENCE"
            reason = "INDEPENDENT_SCORE_BELOW_CONTROL_QUANTILE"
        recommendation = (
            "PROMOTE_HIGH" if validation_class == "VALIDATED_HIGH"
            else "PROMOTE_MEDIUM" if validation_class == "VALIDATED_MEDIUM"
            else "REJECT" if validation_class == "REJECTED"
            else "KEEP_ESTIMATED"
        )
        return GeometryValidationResult(
            record_id=context.record_id,
            source_type=context.source_type,
            control_label=context.control_label,
            validation_class=validation_class,
            validation_score=round(score, 6),
            independent_evidence_count=evidence.evidence_count,
            promotion_recommendation=recommendation,
            reason=reason,
            evidence=evidence,
        )


def nx_number_connected_components(graph) -> int:
    if graph is None or len(graph) == 0:
        return 0
    # Kept local so the validator does not import the routing implementation.
    seen = set()
    count = 0
    for node in graph:
        if node in seen:
            continue
        count += 1
        stack = [node]
        seen.add(node)
        while stack:
            current = stack.pop()
            for neighbour in graph.neighbors(current):
                if neighbour not in seen:
                    seen.add(neighbour)
                    stack.append(neighbour)
    return count


def _context_from_row(row: Mapping[str, Any], graph, calibration, geometry=None, source_type="ESTIMATED", control_label=""):
    alternatives = []
    for item in _safe_json(row.get("alternatives_json"), default=[]) or []:
        if isinstance(item, Mapping):
            candidate = parse_geometry(item.get("geometry_wkt") or item.get("wkt"))
            if candidate is not None:
                alternatives.append(candidate)
    return GeometryValidationContext(
        record_id=_text(row.get("id")),
        geometry=geometry if geometry is not None else parse_geometry(row.get("geometry_wkt")),
        main_street=_text(row.get("main_street")),
        via=_text(row.get("via")),
        de=_text(row.get("de")),
        ate=_text(row.get("ate")),
        codlog=_text(row.get("codlog")),
        latitude=_float(row.get("latitude")),
        longitude=_float(row.get("longitude")),
        extension_m=_float(row.get("extensao_m")),
        candidate_count=int(_float(row.get("candidate_count")) or 0),
        alternatives=alternatives,
        graph=graph,
        calibration=calibration,
        source_type=source_type,
        control_label=control_label,
    )


def load_graph() -> Any:
    if not GRAPH_CACHE.exists() or not GRAPH_SOURCE.exists():
        return None
    try:
        # Some historical graph caches were built by executing the ETL as
        # ``__main__``.  Mapping only that serialized normalizer lets this
        # read-only consumer reopen the immutable graph without importing the
        # ETL module or its correction rules.
        class _ReadOnlyGraphUnpickler(pickle.Unpickler):
            def find_class(self, module, name):
                if module == "__main__" and name == "normalizar_rua":
                    return normalize_name
                return super().find_class(module, name)

        with GRAPH_CACHE.open("rb") as stream:
            payload = _ReadOnlyGraphUnpickler(stream).load()
        if payload.get("version") != RoadGraph.CACHE_VERSION:
            return None
        graph = payload.get("graph")
        if graph is None:
            return None
        graph.normalizer = normalize_name
        graph._rebuild_spatial_index()
        return graph
    except Exception:
        return None


def load_controls(limit: int | None = None) -> list[GeometryValidationContext]:
    if not OFFICIAL_INPUT.exists():
        return []
    frame = pd.read_csv(OFFICIAL_INPUT, dtype=str, encoding="utf-8-sig", keep_default_na=False)
    controls = []
    for _, row in frame.iterrows():
        geometry = _metric_path_from_official(row.get("path"))
        if geometry is None or geometry.length <= 0:
            continue
        context = _context_from_row(row.to_dict(), None, None, geometry=geometry, source_type="OFFICIAL_POSITIVE", control_label="OFFICIAL_GEOMETRY")
        controls.append(context)
    controls.sort(key=lambda item: hashlib.sha1(item.record_id.encode("utf-8")).hexdigest())
    return controls[:limit] if limit else controls


def split_controls(controls: list[GeometryValidationContext]) -> tuple[list[GeometryValidationContext], list[GeometryValidationContext]]:
    calibration, validation = [], []
    for control in controls:
        bucket = int(hashlib.sha1(control.record_id.encode("utf-8")).hexdigest()[:8], 16) % 10
        (calibration if bucket < 7 else validation).append(control)
    return calibration, validation


def _negative_geometry(geometry, label: str):
    if label == "WRONG_STREET_OFFSET":
        return affinity.translate(geometry, xoff=500.0, yoff=500.0)
    if label == "WRONG_COMPONENT_OFFSET":
        return affinity.translate(geometry, xoff=2000.0, yoff=-1500.0)
    if label == "CRITICAL_GAP":
        line = _line_geometry(geometry)
        coordinates = list(line.coords) if line is not None else []
        if len(coordinates) >= 4:
            midpoint = len(coordinates) // 2
            tail = [(x + 500.0, y + 500.0) for x, y in coordinates[midpoint:]]
            return LineString(coordinates[:midpoint] + tail)
        return affinity.translate(geometry, xoff=500.0, yoff=500.0)
    if label == "LOOP":
        line = _line_geometry(geometry)
        coordinates = list(line.coords) if line is not None else []
        return LineString(coordinates + list(reversed(coordinates))) if len(coordinates) >= 2 else geometry
    if label == "EXTREME_EXTENSION":
        return affinity.scale(geometry, xfact=3.0, yfact=3.0, origin="centroid")
    return geometry


NEGATIVE_LABELS = (
    "WRONG_STREET_OFFSET",
    "WRONG_COMPONENT_OFFSET",
    "CRITICAL_GAP",
    "LOOP",
    "EXTREME_EXTENSION",
)


def make_negative_controls(
    controls: list[GeometryValidationContext],
    max_per_label: int | None = None,
) -> list[GeometryValidationContext]:
    result = []
    selected_controls = controls[:max_per_label] if max_per_label else controls
    for control in selected_controls:
        for label in NEGATIVE_LABELS:
            result.append(
                GeometryValidationContext(
                    record_id=f"{control.record_id}::{label}", geometry=_negative_geometry(control.geometry, label),
                    main_street=control.main_street, via=control.via, de=control.de, ate=control.ate,
                    latitude=control.latitude, longitude=control.longitude, extension_m=control.extension_m,
                    graph=control.graph, calibration=control.calibration, source_type="SYNTHETIC_NEGATIVE",
                    control_label=label,
                )
            )
    return result


def _control_result_metrics(results: list[GeometryValidationResult], positive: bool) -> dict[str, Any]:
    total = len(results)
    accepted = sum(result.validation_class in {"VALIDATED_HIGH", "VALIDATED_MEDIUM"} for result in results)
    rejected = sum(result.validation_class == "REJECTED" for result in results)
    return {
        "total": total,
        "accepted": accepted,
        "rejected": rejected,
        "acceptance_rate": accepted / total if total else None,
        "rejection_rate": rejected / total if total else None,
        "control_kind": "positive" if positive else "negative",
    }


def calibrate(controls: list[GeometryValidationContext], graph) -> tuple[Calibration, dict[str, Any]]:
    calibration_controls, validation_controls = split_controls(controls)
    provisional = IndependentGeometryValidator(graph=graph, calibration=None)
    provisional_positive = [
        provisional.validate(GeometryValidationContext(**{**control.__dict__, "graph": graph}))
        for control in calibration_controls
    ]
    provisional_negative_contexts = make_negative_controls(
        [GeometryValidationContext(**{**control.__dict__, "graph": graph}) for control in calibration_controls],
        max_per_label=50,
    )
    provisional_negative = [provisional.validate(context) for context in provisional_negative_contexts]

    positive_scores = [result.validation_score for result in provisional_positive if result.validation_score is not None]
    negative_scores = [result.validation_score for result in provisional_negative if result.validation_score is not None]
    # The upper positive-control quantile is the conservative HIGH gate and
    # the lower positive-control quantile is the exploratory MEDIUM gate.  The
    # negative population is reserved for measuring false acceptance; shifting
    # the gates with it would mix rejection mechanics into calibration.
    high_score = _percentile(positive_scores, 75, 70.0) or 70.0
    medium_score = _percentile(positive_scores, 25, 60.0) or 60.0

    alignment_values = [result.evidence.main_alignment_ratio for result in provisional_positive]
    gap_values = [result.evidence.continuity_max_gap_m for result in provisional_positive]
    boundary_values = [
        value for result in provisional_positive
        for value in (result.evidence.de_distance_m, result.evidence.ate_distance_m)
        if value is not None
    ]
    extension_values = [result.evidence.extension_deviation_pct for result in provisional_positive]
    gps_values = [result.evidence.gps_distance_m for result in provisional_positive]
    evidence_counts = [result.independent_evidence_count for result in provisional_positive]
    calibration = Calibration(
        positive_calibration_count=len(calibration_controls),
        positive_validation_count=len(validation_controls),
        negative_calibration_count=len(provisional_negative_contexts),
        negative_validation_count=min(len(validation_controls), 50) * len(NEGATIVE_LABELS),
        high_score_threshold=round(high_score, 6),
        medium_score_threshold=round(min(medium_score, high_score), 6),
        high_evidence_threshold=max(3, int(math.ceil(_percentile(evidence_counts, 75, 6) or 6))),
        medium_evidence_threshold=max(2, int(math.ceil(_percentile(evidence_counts, 25, 3) or 3))),
        main_alignment_reject_ratio=_percentile(alignment_values, 1, 0.5),
        continuity_hard_gap_m=_percentile(gap_values, 99, 5.0),
        boundary_hard_distance_m=_percentile(boundary_values, 99, 5.0),
        extension_hard_deviation_pct=_percentile(extension_values, 99, 50.0),
        gps_on_path_tolerance_m=_percentile(gps_values, 95, 5.0),
        gps_near_path_tolerance_m=_percentile(gps_values, 99, 15.0),
    )
    calibrated = IndependentGeometryValidator(graph=graph, calibration=calibration)
    positive_calibration_results = [
        calibrated.validate(GeometryValidationContext(**{**control.__dict__, "graph": graph, "calibration": calibration}))
        for control in calibration_controls
    ]
    negative_calibration_results = [
        calibrated.validate(GeometryValidationContext(**{**context.__dict__, "graph": graph, "calibration": calibration}))
        for context in provisional_negative_contexts
    ]
    positive_validation_results = [
        calibrated.validate(GeometryValidationContext(**{**control.__dict__, "graph": graph, "calibration": calibration}))
        for control in validation_controls
    ]
    negative_validation_contexts = make_negative_controls(
        [GeometryValidationContext(**{**control.__dict__, "graph": graph, "calibration": calibration}) for control in validation_controls],
        max_per_label=50,
    )
    negative_validation_results = [calibrated.validate(context) for context in negative_validation_contexts]
    return calibration, {
        "positive_calibration": _control_result_metrics(positive_calibration_results, True),
        "negative_calibration": _control_result_metrics(negative_calibration_results, False),
        "positive_validation": _control_result_metrics(positive_validation_results, True),
        "negative_validation": _control_result_metrics(negative_validation_results, False),
        "negative_validation_by_label": {
            label: _control_result_metrics([result for result in negative_validation_results if result.control_label == label], False)
            for label in NEGATIVE_LABELS
        },
        "validation_errors": [
            result.reason for result in positive_validation_results
            if result.validation_class in {"REJECTED", "INSUFFICIENT_EVIDENCE"}
        ][:20],
    }


def _human_metrics(results: list[GeometryValidationResult]) -> dict[str, Any]:
    if not HUMAN_REVIEW.exists():
        return {"available": False, "sample_size": 0, "precision_estimated": None, "recall_estimated": None}
    frame = pd.read_csv(HUMAN_REVIEW, dtype=str, encoding="utf-8-sig", keep_default_na=False)
    labels = {}
    for _, row in frame.iterrows():
        decision = _text(row.get("decision") or row.get("review_decision") or row.get("status"))
        decision_upper = decision.upper()
        if decision_upper.startswith("APROVAR") or decision_upper == "APPROVED":
            labels[_text(row.get("id"))] = "APPROVED"
        elif decision_upper.startswith("REJEITAR") or decision_upper == "REJECTED":
            labels[_text(row.get("id"))] = "REJECTED"
    reviewed = [result for result in results if result.record_id in labels]
    reviewed_labels = {result.record_id: labels[result.record_id] for result in reviewed}
    accepted = [result for result in reviewed if result.validation_class in {"VALIDATED_HIGH", "VALIDATED_MEDIUM"}]
    true_positive = sum(labels[result.record_id] == "APPROVED" for result in accepted)
    precision = true_positive / len(accepted) if accepted else None
    approved_total = sum(value == "APPROVED" for value in reviewed_labels.values())
    recall = true_positive / approved_total if approved_total else None
    sufficient = len(reviewed) >= MINIMUM_CASES
    precision_ci = _wilson(true_positive, len(accepted)) if accepted and sufficient else (None, None)
    recall_ci = _wilson(true_positive, approved_total) if approved_total and sufficient else (None, None)
    return {
        "available": True,
        "sample_size": len(reviewed),
        "approved_labels": approved_total,
        "rejected_labels": sum(value == "REJECTED" for value in reviewed_labels.values()),
        "accepted_shadow_cases_with_label": len(accepted),
        "true_positive_shadow_cases": true_positive,
        "precision_observed_unreliable": precision,
        "precision_estimated": precision if sufficient else None,
        "precision_95_ci": precision_ci,
        "recall_observed_unreliable": recall,
        "recall_estimated": recall if sufficient else None,
        "recall_95_ci": recall_ci,
        "minimum_cases_for_promotion": MINIMUM_CASES,
        "sample_sufficient_for_promotion": sufficient,
    }


def _aggregate_patterns(rows: pd.DataFrame) -> list[dict[str, Any]]:
    if rows.empty:
        return []
    rows = rows.copy()
    rows["_validation_score_numeric"] = pd.to_numeric(rows.get("validation_score_independent"), errors="coerce")
    patterns = []
    for field in (
        "categoria_falha_atual", "status_atual", "strategy_selected", "root_cause_primary",
        "root_causes", "component_status", "candidate_count", "ambiguous_candidates",
        "topology_status_official", "extension_band", "gps_status", "validation_class",
    ):
        if field not in rows:
            continue
        grouped = rows.groupby(field, dropna=False).agg(
            cases=("id", "size"),
            mean_score=("_validation_score_numeric", "mean"),
            high=("validation_class", lambda values: int((values == "VALIDATED_HIGH").sum())),
            medium=("validation_class", lambda values: int((values == "VALIDATED_MEDIUM").sum())),
            rejected=("validation_class", lambda values: int((values == "REJECTED").sum())),
        ).reset_index()
        for _, item in grouped.iterrows():
            cases = int(item["cases"])
            patterns.append({
                "attribute": field,
                "value": _text(item[field]),
                "cases": cases,
                "mean_independent_score": _float(item["mean_score"]),
                "validated_high": int(item["high"]),
                "validated_medium": int(item["medium"]),
                "rejected": int(item["rejected"]),
                "minimum_cases_met": cases >= MINIMUM_CASES,
                "promotion_eligible": cases >= MINIMUM_CASES and int(item["rejected"]) == 0,
            })
    return sorted(patterns, key=lambda item: (-item["cases"], item["attribute"], item["value"]))


def _shadow_proposals(rows: pd.DataFrame) -> list[dict[str, Any]]:
    """Describe opportunities without encoding a promotion rule."""
    if rows.empty:
        return []
    total = len(rows)
    proposals = []

    def add(name, description, mask, complexity, risk, confidence, dependencies, false_positives, recommendation):
        cases = int(mask.sum())
        proposals.append({
            "name": name,
            "description": description,
            "complexity": complexity,
            "risk": risk,
            "cases_affected": cases,
            "gain_expected_upper_bound_pp": round(cases / total * 100, 6) if total else 0.0,
            "confidence": confidence,
            "dependencies": dependencies,
            "possible_false_positives": false_positives,
            "recommendation": recommendation,
            "minimum_cases": MINIMUM_CASES,
            "minimum_cases_met": cases >= MINIMUM_CASES,
            "implemented": False,
        })

    high = rows["validation_class"].eq("VALIDATED_HIGH")
    medium = rows["validation_class"].eq("VALIDATED_MEDIUM")
    add(
        "EVIDENCE_BUNDLE_HIGH_SHADOW",
        "Investigar apenas casos que já passaram por alinhamento, continuidade e controle de topologia independentes.",
        high,
        "MÉDIA", "ALTO", "EXPLORATÓRIA",
        ["grafo GeoSampa somente leitura", "calibração com geometria oficial", "rótulo humano >= 30 casos"],
        ["nome principal incorreto", "geometria oficial ausente do cadastro atual", "casos correlacionados por mesma fonte"],
        "NÃO IMPLEMENTAR; submeter a revisão humana estratificada",
    )
    add(
        "EVIDENCE_BUNDLE_MEDIUM_REVIEW",
        "Usar o grupo MEDIUM para formar uma amostra de revisão, sem alteração do status oficial.",
        medium,
        "BAIXA", "MÉDIO", "EXPLORATÓRIA",
        ["amostra humana balanceada", "intervalo de confiança", "checagem de concorrência"],
        ["GPS ausente", "limite de extensão compatível por acaso", "trechos de componente vizinho"],
        "NÃO IMPLEMENTAR; somente priorizar revisão",
    )
    if "hard_failures" in rows:
        boundary = rows["hard_failures"].fillna("").str.contains("BOUNDARY_CONTRADICTION", regex=False)
        add(
            "BOUNDARY_CONTRADICTION_NEVER_PROMOTE",
            "Preservar como ESTIMATED qualquer geometria com extremidade contradita por transversal verificável.",
            boundary,
            "BAIXA", "MUITO ALTO", "ALTA PARA O SINAL",
            ["interseções do grafo", "revisão de casos limítrofes"],
            ["transversal não cadastrada", "atualização temporal do GeoSampa"],
            "NÃO PROMOVER automaticamente",
        )
    if "competition_status" in rows:
        competition = rows["competition_status"].eq("LOW_MARGIN")
        add(
            "LOW_MARGIN_NEVER_HIGH",
            "Não considerar HIGH quando uma alternativa mantém margem independente baixa ou empate.",
            competition,
            "MÉDIA", "ALTO", "EXPLORATÓRIA",
            ["geometrias alternativas persistidas", "margem independente", "validação humana"],
            ["alternativas incompletas no artefato shadow", "diferença de precisão geométrica"],
            "NÃO PROMOVER a HIGH; estudar apenas como MEDIUM/revisão",
        )
    for rank, proposal in enumerate(sorted(proposals, key=lambda item: (-item["gain_expected_upper_bound_pp"], item["complexity"])), 1):
        proposal["roi_rank"] = rank
        proposal["roi_note"] = "upper_bound_only; ganho não validado e não oficial"
    return proposals


def _source_hashes() -> dict[str, str | None]:
    paths = (OFFICIAL_INPUT, PROCESSED / "notificacoes.csv", PROCESSED / "cruzamento.csv", PROCESSED / "recapes_sem_cobertura.csv", PROCESSED / "geosampa_coverage_report.json", PROCESSED / "pipeline_run.json")
    result = {}
    for path in paths:
        if not path.exists():
            result[path.name] = None
            continue
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        result[path.name] = digest.hexdigest()
    return result


def _summary_results(frame: pd.DataFrame) -> list[GeometryValidationResult]:
    return [GeometryValidationResult(
        record_id=_text(row.get("id")), source_type="ESTIMATED", control_label="",
        validation_class=_text(row.get("validation_class")), validation_score=_float(row.get("validation_score_independent")),
        independent_evidence_count=int(_float(row.get("independent_evidence_count")) or 0),
        promotion_recommendation=_text(row.get("shadow_recommendation")), reason=_text(row.get("validation_reason")),
        evidence=GeometryValidationEvidence(),
    ) for _, row in frame.iterrows()]


def _ensure_report_columns(frame: pd.DataFrame, calibration: Mapping[str, Any] | None = None) -> pd.DataFrame:
    """Add the stable report vocabulary without changing the analyzed geometry."""
    frame = frame.copy()
    numeric = lambda name: pd.to_numeric(frame.get(name), errors="coerce")
    frame["validation_score"] = numeric("validation_score_independent")
    frame["geometry_confidence"] = frame.get("official_geometry_confidence", "ESTIMATED")
    frame["geometry_score"] = numeric("official_geometry_score_comparison_only")
    recommendation = frame["shadow_recommendation"] if "shadow_recommendation" in frame else pd.Series("KEEP_ESTIMATED", index=frame.index)
    frame["promotion_recommendation"] = recommendation.replace({
        "SHADOW_CANDIDATE_HIGH": "PROMOTE_HIGH",
        "SHADOW_CANDIDATE_MEDIUM": "PROMOTE_MEDIUM",
    })
    if "validation_class" in frame:
        frame.loc[frame["validation_class"] == "REJECTED", "promotion_recommendation"] = "REJECT"
    frame["main_street_alignment_pct"] = numeric("main_alignment_ratio") * 100.0
    frame["mean_distance_to_main_street_m"] = numeric("main_mean_distance_m")
    frame["max_distance_to_main_street_m"] = numeric("main_max_distance_m")
    frame["continuity_score"] = numeric("continuity_score")
    if frame["continuity_score"].isna().all():
        limit = float((calibration or {}).get("continuity_hard_gap_m") or 5.0)
        frame["continuity_score"] = (100.0 * (1.0 - numeric("continuity_max_gap_m") / max(limit, 1e-9))).clip(lower=0, upper=100)
    frame["gap_count"] = numeric("continuity_gap_count")
    frame["max_gap_m"] = numeric("continuity_max_gap_m")
    frame["path_length_m"] = numeric("length_m")
    frame["de_validation"] = frame.get("de_status", "UNAVAILABLE")
    frame["ate_validation"] = frame.get("ate_status", "UNAVAILABLE")
    frame["boundary_validation_score"] = numeric("boundary_validation_score")
    if frame["boundary_validation_score"].isna().all():
        frame["boundary_validation_score"] = frame.apply(
            lambda row: 0.0 if "CONTRADICT" in str(row.get("de_status", "")) or "CONTRADICT" in str(row.get("ate_status", ""))
            else 100.0 if row.get("de_status") == "CONFIRMED" and row.get("ate_status") == "CONFIRMED"
            else 50.0 if row.get("de_status") == "CONFIRMED" or row.get("ate_status") == "CONFIRMED"
            else None,
            axis=1,
        )
    frame["topology_score"] = numeric("topology_score")
    if frame["topology_score"].isna().all():
        frame["topology_score"] = frame.apply(
            lambda row: 100.0 if str(row.get("component_count")) == "1" else 0.0 if str(row.get("component_count")) not in {"", "nan", "None"} else 50.0 if row.get("topology_status") == "GEOMETRIC_ALIGNMENT_ONLY" else None,
            axis=1,
        )
    frame["validation_margin_top2"] = numeric("top2_margin")
    frame["valid_geometry"] = frame.get("geometry_valid", False)
    frame["reason"] = frame.get("validation_reason", "")
    frame["evidence"] = frame.apply(
        lambda row: json.dumps([
            name for name, value in (
                ("main_street", row.get("main_alignment_ratio")), ("continuity", row.get("continuity_max_gap_m")),
                ("gps", row.get("gps_status")), ("extension", row.get("extension_deviation_pct")),
                ("de", row.get("de_status")), ("ate", row.get("ate_status")),
                ("topology", row.get("component_count")), ("competition", row.get("alternative_count")),
            ) if str(value) not in {"", "nan", "None", "UNAVAILABLE"}
        ], ensure_ascii=False),
        axis=1,
    )
    return frame


def refresh_report_only() -> dict[str, Any]:
    """Refresh labels/pattern summaries without recomputing geometry."""
    if not OUTPUT_CSV.exists() or not OUTPUT_REPORT.exists():
        raise FileNotFoundError("artefatos shadow ausentes para --report-only")
    frame = pd.read_csv(OUTPUT_CSV, dtype=str, encoding="utf-8-sig", keep_default_na=False)
    report = json.loads(OUTPUT_REPORT.read_text(encoding="utf-8"))
    frame = _ensure_report_columns(frame, report.get("calibration"))
    frame.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")
    before_hashes = _source_hashes()
    class_counts = frame["validation_class"].value_counts(dropna=False).to_dict()
    accepted = int(sum(class_counts.get(key, 0) for key in ("VALIDATED_HIGH", "VALIDATED_MEDIUM")))
    quality_report = json.loads((PROCESSED / "route_geometry_quality_report.json").read_text(encoding="utf-8"))
    total_population = int(quality_report.get("total_records") or (quality_report.get("scope") or {}).get("total_recapes") or len(frame))
    official_geometry_count = int((quality_report.get("scope") or {}).get("official_geometry_count") or 0)
    report["total_estimated"] = int(len(frame))
    report["validated_high"] = int(class_counts.get("VALIDATED_HIGH", 0))
    report["validated_medium"] = int(class_counts.get("VALIDATED_MEDIUM", 0))
    report["insufficient_evidence"] = int(class_counts.get("INSUFFICIENT_EVIDENCE", 0))
    report["rejected"] = int(class_counts.get("REJECTED", 0))
    report["promotion_high"] = int(class_counts.get("VALIDATED_HIGH", 0))
    report["promotion_medium"] = int(class_counts.get("VALIDATED_MEDIUM", 0))
    report["keep_estimated"] = int(class_counts.get("INSUFFICIENT_EVIDENCE", 0))
    report["reject"] = int(class_counts.get("REJECTED", 0))
    report["by_strategy"] = {str(value): int(count) for value, count in frame["strategy_selected"].fillna("").value_counts().items()}
    report["by_root_cause"] = {str(value): int(count) for value, count in frame["root_cause_primary"].fillna("").value_counts().items()}
    report["by_validation_class"] = {str(key): int(value) for key, value in class_counts.items()}
    report["control_positive_results"] = (report.get("control_report") or {}).get("positive_validation")
    report["control_negative_results"] = (report.get("control_report") or {}).get("negative_validation")
    report["thresholds"] = report.get("calibration")
    report["calibration_metrics"] = report.get("control_report")
    report["projected_coverage_without_estimated"] = round(official_geometry_count / total_population * 100, 6) if total_population else None
    report["projected_coverage_if_high_applied"] = round((official_geometry_count + int(class_counts.get("VALIDATED_HIGH", 0))) / total_population * 100, 6) if total_population else None
    report["projected_coverage_if_high_medium_applied"] = round((official_geometry_count + accepted) / total_population * 100, 6) if total_population else None
    report["estimated_cases_processed"] = int(len(frame))
    report["classification_counts"] = {str(key): int(value) for key, value in class_counts.items()}
    report["human_validation"] = _human_metrics(_summary_results(frame))
    report["patterns"] = _aggregate_patterns(frame)
    report["shadow_proposals"] = _shadow_proposals(frame)
    report["shadow_simulation"].update({
        "cases_affected": int(len(frame)),
        "promotions_simulated_high": int(class_counts.get("VALIDATED_HIGH", 0)),
        "promotions_simulated_medium": int(class_counts.get("VALIDATED_MEDIUM", 0)),
        "rejections_simulated": int(class_counts.get("REJECTED", 0)),
        "possible_errors": int(class_counts.get("REJECTED", 0)),
        "coverage_gain_upper_bound_pp": round(accepted / total_population * 100, 6) if total_population else None,
    })
    after_hashes = _source_hashes()
    report["official_outputs_unchanged"] = before_hashes == after_hashes
    report["official_output_hashes_before"] = before_hashes
    report["official_output_hashes_after"] = after_hashes
    OUTPUT_REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    return report


def run_shadow(args: argparse.Namespace) -> dict[str, Any]:
    PROCESSED.mkdir(parents=True, exist_ok=True)
    if args.reset_cache:
        for path in (OUTPUT_CSV, OUTPUT_REPORT):
            if path.exists():
                path.unlink()
    before_hashes = _source_hashes()
    graph = load_graph()
    controls = load_controls(args.sample if args.sample else None)
    calibration, control_report = calibrate(controls, graph)
    validator = IndependentGeometryValidator(graph=graph, calibration=calibration)

    frame = pd.read_csv(SHADOW_INPUT, dtype=str, encoding="utf-8-sig", keep_default_na=False)
    frame = frame[frame.get("geometry_confidence", "") == "ESTIMATED"].copy()
    if args.only_strategy:
        frame = frame[frame["strategy_selected"].isin(args.only_strategy)].copy()
    if args.only_root_cause:
        frame = frame[frame["root_cause_primary"].isin(args.only_root_cause)].copy()
    if args.sample:
        frame = frame.head(args.sample)

    existing = {}
    if args.resume and OUTPUT_CSV.exists():
        old = pd.read_csv(OUTPUT_CSV, dtype=str, encoding="utf-8-sig", keep_default_na=False)
        existing = {str(row["id"]): row.to_dict() for _, row in old.iterrows() if "id" in row}
    results = []
    for _, row in frame.iterrows():
        identifier = _text(row.get("id"))
        if identifier in existing:
            results.append(existing[identifier])
            continue
        context = _context_from_row(row.to_dict(), graph, calibration)
        result = validator.validate(context)
        output = result.to_row()
        output.update({
            "via": _text(row.get("via")),
            "via_resolvida": _text(row.get("via_resolvida")),
            "de": _text(row.get("de")),
            "ate": _text(row.get("ate")),
            "extensao_m": _float(row.get("extensao_m")),
            "candidate_count": _float(row.get("candidate_count")),
            "ambiguous_candidates": _text(row.get("ambiguous_candidates")),
            "component_status": _text(row.get("component_status")),
            "topology_status_official": _text(row.get("topology_status")),
            "root_causes": _text(row.get("root_causes")),
            "before_geometry_confidence": _text(row.get("before_geometry_confidence")),
            "before_strategy": _text(row.get("before_strategy")),
            "official_geometry_confidence": _text(row.get("geometry_confidence")),
            "official_strategy": _text(row.get("strategy_selected")),
            "official_geometry_score_comparison_only": _float(row.get("geometry_score")),
            "status_atual": _text(row.get("status_atual")),
            "categoria_falha_atual": _text(row.get("categoria_falha_atual")),
            "root_cause_primary": _text(row.get("root_cause_primary")),
            "strategy_selected": _text(row.get("strategy_selected")),
            "geometry_wkt": _text(row.get("geometry_wkt")),
            "geometry_hash_sha256": hashlib.sha256(_text(row.get("geometry_wkt")).encode("utf-8")).hexdigest(),
            "official_status_unchanged": True,
            "validator_version": "geometry-validator-shadow-v1",
        })
        results.append(output)
    output_frame = _ensure_report_columns(pd.DataFrame(results), calibration.to_dict())
    if not output_frame.empty:
        output_frame.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")

    after_hashes = _source_hashes()
    if before_hashes != after_hashes:
        raise RuntimeError("Arquivo oficial alterado durante a validação shadow")
    class_counts = output_frame["validation_class"].value_counts(dropna=False).to_dict() if not output_frame.empty else {}
    shadow_accepted = int(sum(class_counts.get(key, 0) for key in ("VALIDATED_HIGH", "VALIDATED_MEDIUM")))
    quality_report_path = PROCESSED / "route_geometry_quality_report.json"
    quality_report = {}
    if quality_report_path.exists():
        try:
            quality_report = json.loads(quality_report_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            quality_report = {}
    total_official_population = int(quality_report.get("total_records") or (quality_report.get("scope") or {}).get("total_recapes") or len(frame) or 0)
    official_geometry_count = int((quality_report.get("scope") or {}).get("official_geometry_count") or 0)
    def distribution(field):
        if field not in output_frame:
            return {}
        return {str(value): int(count) for value, count in output_frame[field].fillna("").value_counts().items()}
    report = {
        "mode": "SHADOW_ONLY",
        "validator_version": "geometry-validator-shadow-v1",
        "input": str(SHADOW_INPUT.relative_to(ROOT)),
        "estimated_cases_available": int(len(frame)),
        "estimated_cases_processed": int(len(output_frame)),
        "graph_loaded_read_only": graph is not None,
        "official_outputs_unchanged": before_hashes == after_hashes,
        "official_output_hashes_before": before_hashes,
        "official_output_hashes_after": after_hashes,
        "classification_counts": {str(key): int(value) for key, value in class_counts.items()},
        "total_estimated": int(len(output_frame)),
        "validated_high": int(class_counts.get("VALIDATED_HIGH", 0)),
        "validated_medium": int(class_counts.get("VALIDATED_MEDIUM", 0)),
        "insufficient_evidence": int(class_counts.get("INSUFFICIENT_EVIDENCE", 0)),
        "rejected": int(class_counts.get("REJECTED", 0)),
        "promotion_high": int(class_counts.get("VALIDATED_HIGH", 0)),
        "promotion_medium": int(class_counts.get("VALIDATED_MEDIUM", 0)),
        "keep_estimated": int(class_counts.get("INSUFFICIENT_EVIDENCE", 0)),
        "reject": int(class_counts.get("REJECTED", 0)),
        "by_strategy": distribution("strategy_selected"),
        "by_root_cause": distribution("root_cause_primary"),
        "by_validation_class": {str(key): int(value) for key, value in class_counts.items()},
        "shadow_simulation": {
            "cases_affected": int(len(output_frame)),
            "promotions_simulated_high": int(class_counts.get("VALIDATED_HIGH", 0)),
            "promotions_simulated_medium": int(class_counts.get("VALIDATED_MEDIUM", 0)),
            "rejections_simulated": int(class_counts.get("REJECTED", 0)),
            "official_promotions": 0,
            "official_status_changed": False,
            "possible_errors": int(class_counts.get("REJECTED", 0)),
            "coverage_gain_upper_bound_pp": round(shadow_accepted / total_official_population * 100, 6) if total_official_population else None,
            "coverage_gain_is_realistic_estimate": False,
            "coverage_note": "upper bound of shadow recommendations; no precision/recall sufficient for an official claim",
        },
        "projected_coverage_without_estimated": round(official_geometry_count / total_official_population * 100, 6) if total_official_population else None,
        "projected_coverage_if_high_applied": round((official_geometry_count + int(class_counts.get("VALIDATED_HIGH", 0))) / total_official_population * 100, 6) if total_official_population else None,
        "projected_coverage_if_high_medium_applied": round((official_geometry_count + shadow_accepted) / total_official_population * 100, 6) if total_official_population else None,
        "calibration": calibration.to_dict(),
        "control_report": control_report,
        "control_positive_results": control_report.get("positive_validation"),
        "control_negative_results": control_report.get("negative_validation"),
        "thresholds": calibration.to_dict(),
        "calibration_metrics": control_report,
        "human_validation": _human_metrics([GeometryValidationResult(
            record_id=_text(row["id"]), source_type="ESTIMATED", control_label="",
            validation_class=_text(row["validation_class"]), validation_score=_float(row.get("validation_score_independent")),
            independent_evidence_count=int(_float(row.get("independent_evidence_count")) or 0),
            promotion_recommendation=_text(row.get("shadow_recommendation")), reason=_text(row.get("validation_reason")),
            evidence=GeometryValidationEvidence(),
        ) for _, row in output_frame.iterrows()]),
        "patterns": _aggregate_patterns(output_frame),
        "shadow_proposals": _shadow_proposals(output_frame),
        "official_promotion_performed": False,
        "official_confidence_changed": False,
        "route_or_resolver_called": False,
        "etl_called": False,
        "notes": [
            "VALIDATED_* são recomendações shadow; nenhum resultado oficial foi promovido.",
            "Precisão/recall de casos ESTIMATED só são estimados quando há rótulo humano suficiente.",
            "A coluna official_geometry_score_comparison_only não participa do score independente.",
        ],
    }
    OUTPUT_REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validação geométrica independente em modo shadow")
    parser.add_argument("--shadow", action="store_true", help="executa somente a validação shadow")
    parser.add_argument("--sample", type=int, default=None, help="limita casos ESTIMATED e controles para uma amostra")
    parser.add_argument("--resume", action="store_true", help="reaproveita IDs já presentes no CSV shadow")
    parser.add_argument("--reset-cache", action="store_true", help="remove apenas os dois artefatos shadow antes de executar")
    parser.add_argument("--report-only", action="store_true", help="atualiza métricas do relatório sem recalcular geometrias")
    parser.add_argument("--only-strategy", action="append", default=[], help="filtra strategy_selected; pode repetir")
    parser.add_argument("--only-root-cause", action="append", default=[], help="filtra root_cause_primary; pode repetir")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.shadow:
        print("Modo seguro: informe --shadow para executar; nenhum arquivo foi alterado.", file=sys.stderr)
        return 2
    if args.report_only:
        report = refresh_report_only()
        print(json.dumps({
            "processed": report["estimated_cases_processed"],
            "classes": report["classification_counts"],
            "official_outputs_unchanged": report["official_outputs_unchanged"],
        }, ensure_ascii=False))
        return 0
    report = run_shadow(args)
    print(json.dumps({
        "processed": report["estimated_cases_processed"],
        "classes": report["classification_counts"],
        "official_outputs_unchanged": report["official_outputs_unchanged"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
