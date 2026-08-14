"""Shadow-only diagnostic audit for De/Até boundary contradictions.

The module is intentionally separate from ``geometry_validator.py`` and from all
generation/resolution code.  It reads persisted shadow rows, official geometries
as positive controls, and a read-only RoadGraph spatial index.  It may construct
temporary diagnostic paths for comparison, but it never writes an official
geometry or changes a classification.
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
from numbers import Integral
from pathlib import Path
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd
from pyproj import Transformer
from rapidfuzz import fuzz
from shapely import affinity
from shapely.geometry import LineString, MultiLineString, Point
from shapely.ops import linemerge, transform, unary_union
from shapely.strtree import STRtree
from shapely.wkt import loads as load_wkt

try:
    from road_graph import RoadGraph
except ImportError:  # pragma: no cover
    from .road_graph import RoadGraph


ROOT = Path(__file__).resolve().parents[1]
PROCESSED = ROOT / "data" / "processed"
CACHE = ROOT / "data" / "cache"
VALIDATION_INPUT = PROCESSED / "geometry_validation_shadow.csv"
QUALITY_INPUT = PROCESSED / "route_geometry_quality_shadow.csv"
OFFICIAL_INPUT = PROCESSED / "recape_clean.csv"
QUALITY_REPORT = PROCESSED / "route_geometry_quality_report.json"
GRAPH_CACHE = CACHE / "geosampa_road_graph.pkl"
GRAPH_SOURCE = CACHE / "geosampa_segmento_logradouro.geojson"
OUTPUT_CSV = PROCESSED / "boundary_contradiction_audit.csv"
OUTPUT_REPORT = PROCESSED / "boundary_contradiction_report.json"
LOCAL_CACHE = PROCESSED / "boundary_contradiction_audit_cache.json"
MINIMUM_CASES = 30
NEGATIVE_LABELS = (
    "SWAP_DE",
    "SWAP_ATE",
    "INVERT_ONE_BOUNDARY",
    "PARALLEL_STREET",
    "WRONG_COMPONENT",
    "NEAR_WRONG_BOUNDARY",
)

WGS84_TO_METRIC = Transformer.from_crs("EPSG:4326", "EPSG:31983", always_xy=True)


def _text(value: Any) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    return str(value).strip()


def _number(value: Any) -> float | None:
    text = _text(value)
    if not text:
        return None
    try:
        number = float(text.replace(",", "."))
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _percentile(values: Iterable[float | None], percentile: float, fallback: float) -> float:
    data = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    return float(np.percentile(data, percentile)) if data else fallback


def _wilson(successes: int, total: int, z: float = 1.96) -> tuple[float | None, float | None]:
    if total <= 0:
        return None, None
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    radius = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / denominator
    return max(0.0, centre - radius), min(1.0, centre + radius)


def normalize_name(value: Any) -> str:
    """Local exact-comparison normalizer; no production aliases or fuzzy global match."""
    text = unicodedata.normalize("NFKD", _text(value).upper()).encode("ascii", "ignore").decode("ascii")
    text = re.split(r"\s+-\s+|,\s*|/\s*|\s+\(", text, maxsplit=1)[0]
    text = re.sub(
        r"^(RUA|R\.?|AVENIDA|AV\.?|ALAMEDA|AL\.?|TRAVESSA|TV\.?|ESTRADA|EST\.?|"
        r"RODOVIA|ROD\.?|PRACA|PC\.?|LARGO|LGO\.?|VIELA|VL\.?|VIADUTO|VD\.?)\s+",
        "",
        text,
    )
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9\s]", " ", text)).strip()


def _json(value: Any, default: Any = None) -> Any:
    text = _text(value)
    if not text or text.lower() in {"none", "null", "nan"}:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def parse_wkt(value: Any):
    try:
        geometry = load_wkt(_text(value))
        return geometry if geometry is not None and not geometry.is_empty else None
    except Exception:
        return None


def parse_official_path(value: Any):
    payload = _json(value, [])
    if not isinstance(payload, list):
        return None
    coordinates = []
    for item in payload:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            lon, lat = _number(item[0]), _number(item[1])
            if lon is not None and lat is not None:
                coordinates.append((lon, lat))
    if len(coordinates) < 2:
        return None
    return transform(WGS84_TO_METRIC.transform, LineString(coordinates))


def _line_parts(geometry) -> list[LineString]:
    if geometry is None or geometry.is_empty:
        return []
    if isinstance(geometry, LineString):
        return [geometry]
    if isinstance(geometry, MultiLineString):
        return list(geometry.geoms)
    return [part for part in getattr(geometry, "geoms", ()) if isinstance(part, LineString)]


def _as_line(geometry):
    parts = _line_parts(geometry)
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    merged = linemerge(parts)
    return merged if not merged.is_empty else geometry


def _points(geometry) -> list[Point]:
    if geometry is None or geometry.is_empty:
        return []
    if geometry.geom_type == "Point":
        return [geometry]
    if geometry.geom_type == "MultiPoint":
        return list(geometry.geoms)
    if geometry.geom_type in {"LineString", "MultiLineString"}:
        return [Point(part.coords[0]) for part in _line_parts(geometry)] + [
            Point(part.coords[-1]) for part in _line_parts(geometry)
        ]
    result = []
    for part in getattr(geometry, "geoms", ()):
        result.extend(_points(part))
    return result


class _ReadOnlyGraphUnpickler(pickle.Unpickler):
    def find_class(self, module, name):
        # Historical caches serialized the ETL normalizer as __main__. Do not
        # import the ETL to reopen the cache.
        if module == "__main__" and name == "normalizar_rua":
            return normalize_name
        return super().find_class(module, name)


def load_read_only_graph():
    if not GRAPH_CACHE.exists() or not GRAPH_SOURCE.exists():
        return None
    try:
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


@dataclass
class BoundaryCalibration:
    positive_calibration_count: int
    positive_validation_count: int
    negative_calibration_count: int
    negative_validation_count: int
    valid_endpoint_distance_m: float
    plausible_endpoint_distance_m: float
    small_gap_distance_m: float
    node_tolerance_m: float
    parallel_distance_m: float
    lexical_plausible_threshold: float
    high_score_threshold: float
    medium_score_threshold: float
    high_evidence_threshold: int
    medium_evidence_threshold: int
    candidate_margin_threshold: float
    method: str = "official_positive_control_quantiles_with_synthetic_negative_check"

    def to_dict(self):
        return asdict(self)


@dataclass
class BoundaryCandidate:
    name: str
    street_norm: str
    codlog: str = ""
    lexical_similarity: float = 0.0
    intersection_type: str = "NO_INTERSECTION"
    intersection_distance_m: float | None = None
    distance_to_start_m: float | None = None
    distance_to_end_m: float | None = None
    snap_required: bool = False
    snap_distance_m: float | None = None
    component_match: str = "UNKNOWN"
    intersection_count: int = 0
    parallel: bool = False
    candidate_score: float = 0.0
    is_current: bool = False
    intersection_point: Any = None

    def to_dict(self):
        result = asdict(self)
        result.pop("intersection_point", None)
        return result


@dataclass
class BoundaryEvidence:
    side: str
    original_name: str = ""
    normalized_name: str = ""
    current_name: str = ""
    current_normalized: str = ""
    status: str = "NOT_FOUND"
    selected_candidate: str = ""
    selected_codlog: str = ""
    selected_lexical_similarity: float | None = None
    candidates: list[BoundaryCandidate] = field(default_factory=list)
    intersection_type: str = "NO_INTERSECTION"
    intersection_distance_m: float | None = None
    distance_to_start_m: float | None = None
    distance_to_end_m: float | None = None
    snap_required: bool = False
    snap_distance_m: float | None = None
    component_match: str = "UNKNOWN"
    intersection_count: int = 0
    gps_distance_m: float | None = None
    gap_m: float | None = None
    root_causes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def recovered(self) -> bool:
        return bool(self.selected_candidate) and self.status in {"VALID", "PLAUSIBLE"}


@dataclass
class BoundaryContext:
    record_id: str
    via: str
    via_resolved: str
    de: str
    ate: str
    de_current: str
    ate_current: str
    geometry: Any
    extension_m: float | None
    latitude: float | None
    longitude: float | None
    gps_status: str
    gps_distance_m: float | None
    extension_deviation_pct: float | None
    main_street: str
    root_cause_primary: str
    source_type: str = "ESTIMATED"
    control_label: str = ""


@dataclass
class BoundaryContradictionResult:
    record_id: str
    source_type: str
    control_label: str
    via: str
    de_original: str
    ate_original: str
    de_current: str
    ate_current: str
    de_status: str
    ate_status: str
    de_candidate: str
    ate_candidate: str
    de_codlog: str
    ate_codlog: str
    de_intersection_type: str
    ate_intersection_type: str
    de_distance_m: float | None
    ate_distance_m: float | None
    de_snap_distance_m: float | None
    ate_snap_distance_m: float | None
    de_gps_distance_m: float | None
    ate_gps_distance_m: float | None
    boundaries_reversed: bool
    root_cause: str
    secondary_causes: list[str]
    boundary_validation_score: float
    boundary_evidence_count: int
    recovered_de: str
    recovered_ate: str
    recovered_both: bool
    candidate_geometry_wkt: str
    diagnostic_geometry_wkt: str
    overlap_with_current_pct: float | None
    length_difference_pct: float | None
    hausdorff_distance_m: float | None
    recommendation: str
    requires_review: bool
    reason: str
    warnings: list[str]
    de_candidates: list[dict[str, Any]]
    ate_candidates: list[dict[str, Any]]

    def to_row(self):
        result = asdict(self)
        result["id"] = self.record_id
        result["secondary_causes"] = "|".join(self.secondary_causes)
        result["warnings"] = "|".join(self.warnings)
        result["de_candidates_json"] = json.dumps(self.de_candidates, ensure_ascii=False)
        result["ate_candidates_json"] = json.dumps(self.ate_candidates, ensure_ascii=False)
        result.pop("de_candidates")
        result.pop("ate_candidates")
        return result


class BoundarySpatialIndex:
    """Read-only spatial view over RoadGraph with independent local caches."""

    def __init__(self, graph):
        self.graph = graph
        self.street_union_cache: dict[str, Any] = {}
        self.street_node_cache: dict[str, list[Point]] = {}
        self.intersection_cache: dict[tuple[str, str], tuple[list[Point], Any]] = {}

    def ids_for_geometry(self, geometry) -> list[str]:
        if self.graph is None or self.graph._tree is None or geometry is None:
            return []
        try:
            hits = self.graph._tree.query(geometry)
        except Exception:
            return []
        identifiers = list(self.graph.segments)
        result = []
        for hit in hits:
            if isinstance(hit, Integral):
                result.append(identifiers[int(hit)])
            else:
                index = self.graph._geometry_index.get(id(hit))
                if index is not None:
                    result.append(identifiers[index])
        return result

    def nearby_segments(self, geometry, radius: float) -> list[Any]:
        return [self.graph.segments[item] for item in self.ids_for_geometry(geometry.buffer(radius))]

    def street_geometry(self, street: str):
        if street in self.street_union_cache:
            return self.street_union_cache[street]
        if not street or self.graph is None:
            return None
        geometries = [self.graph.segments[item].geometry for item in self.graph.street_segments.get(street, ())]
        value = unary_union(geometries) if geometries else None
        self.street_union_cache[street] = value
        return value

    def street_nodes(self, street: str) -> list[Point]:
        if street in self.street_node_cache:
            return self.street_node_cache[street]
        nodes = []
        seen = set()
        if self.graph is not None:
            for identifier in self.graph.street_segments.get(street, ()):
                segment = self.graph.segments[identifier]
                for coordinate in (segment.start, segment.end):
                    if coordinate not in seen:
                        seen.add(coordinate)
                        nodes.append(Point(coordinate))
        self.street_node_cache[street] = nodes
        return nodes

    def intersections(self, main: str, other: str) -> tuple[list[Point], Any]:
        key = tuple(sorted((main, other)))
        if key in self.intersection_cache:
            return self.intersection_cache[key]
        main_geometry, other_geometry = self.street_geometry(main), self.street_geometry(other)
        if main_geometry is None or other_geometry is None:
            value = ([], None)
        else:
            intersection = main_geometry.intersection(other_geometry)
            value = (_points(intersection), intersection)
        self.intersection_cache[key] = value
        return value

    def exact_street(self, value: str) -> str | None:
        normalized = normalize_name(value)
        return normalized if normalized and normalized in self.graph.street_segments else None

    def local_streets(self, main: str, geometry, endpoint: Point, radius: float = 25.0) -> set[str]:
        names = set()
        main_geometry = self.street_geometry(main)
        for item in self.nearby_segments(endpoint, radius):
            if item.street_norm != main:
                names.add(item.street_norm)
        if main_geometry is not None:
            for item in self.nearby_segments(main_geometry, radius):
                if item.street_norm != main:
                    names.add(item.street_norm)
        if geometry is not None:
            for item in self.nearby_segments(geometry, radius):
                if item.street_norm != main:
                    names.add(item.street_norm)
        return names


class BoundaryContradictionAuditEngine:
    def __init__(self, graph, calibration: BoundaryCalibration | None = None):
        self.index = BoundarySpatialIndex(graph)
        self.graph = graph
        self.calibration = calibration or BoundaryCalibration(
            positive_calibration_count=0, positive_validation_count=0,
            negative_calibration_count=0, negative_validation_count=0,
            valid_endpoint_distance_m=12.0, plausible_endpoint_distance_m=30.0,
            small_gap_distance_m=8.0, node_tolerance_m=8.0,
            parallel_distance_m=20.0, lexical_plausible_threshold=70.0,
            high_score_threshold=70.0, medium_score_threshold=50.0,
            high_evidence_threshold=6, medium_evidence_threshold=3,
            candidate_margin_threshold=0.08,
        )

    def main_street(self, context: BoundaryContext) -> str:
        candidates = (context.main_street, context.via_resolved, context.via)
        for value in candidates:
            normalized = normalize_name(value)
            if self.graph is not None and normalized in self.graph.street_segments:
                return normalized
        return next((normalize_name(value) for value in candidates if normalize_name(value)), "")

    def _candidate_names(self, main: str, context: BoundaryContext, original: str, current: str, endpoint: Point) -> list[str]:
        names = self.index.local_streets(main, context.geometry, endpoint, radius=self.calibration.parallel_distance_m)
        for value in (original, current):
            exact = self.index.exact_street(value)
            if exact:
                names.add(exact)
        if not names:
            return []
        original_norm = normalize_name(original)
        ranked = sorted(
            names,
            key=lambda name: (-fuzz.token_set_ratio(original_norm, name), name),
        )
        return ranked[:40]

    def _component_match(self, main: str, point: Point | None, endpoint: Point) -> str:
        if point is None:
            return "UNKNOWN"
        main_nodes = self.index.street_nodes(main)
        if not main_nodes:
            return "UNKNOWN"
        nearest_point = min(main_nodes, key=lambda item: item.distance(point))
        endpoint_node = min(main_nodes, key=lambda item: item.distance(endpoint))
        graph = self.graph.street_graphs.get(main) if self.graph is not None else None
        if graph is None:
            return "UNKNOWN"
        try:
            import networkx as nx
            node_a = (nearest_point.x, nearest_point.y)
            node_b = (endpoint_node.x, endpoint_node.y)
            if node_a not in graph or node_b not in graph:
                return "UNKNOWN"
            return "SAME_COMPONENT" if nx.has_path(graph, node_a, node_b) else "WRONG_COMPONENT"
        except Exception:
            return "UNKNOWN"

    def _candidate(self, main: str, boundary: str, original: str, current: str, endpoint: Point, other_endpoint: Point, context: BoundaryContext) -> list[BoundaryCandidate]:
        original_norm = normalize_name(original)
        current_norm = normalize_name(current)
        result = []
        for street in self._candidate_names(main, context, original, current, endpoint):
            geometry = self.index.street_geometry(street)
            if geometry is None:
                continue
            points, intersection = self.index.intersections(main, street)
            distances = [endpoint.distance(point) for point in points]
            if points:
                position = int(np.argmin(distances))
                point = points[position]
                node_distance = min(
                    [node.distance(point) for node in self.index.street_nodes(main) + self.index.street_nodes(street)] or [999999.0]
                )
                if node_distance <= self.calibration.node_tolerance_m:
                    intersection_type = "REAL_NODE"
                else:
                    intersection_type = "GEOMETRIC_NO_NODE"
                intersection_distance = float(distances[position])
                gap = 0.0
            else:
                intersection_distance = float(endpoint.distance(geometry))
                main_geometry = self.index.street_geometry(main)
                gap = float(main_geometry.distance(geometry)) if main_geometry is not None else None
                if gap is not None and gap <= self.calibration.small_gap_distance_m:
                    intersection_type = "SMALL_TOPOLOGY_GAP"
                elif gap is not None and gap <= self.calibration.parallel_distance_m:
                    intersection_type = "PARALLEL_NEAR"
                else:
                    intersection_type = "NO_INTERSECTION"
                point = None
            lexical = float(fuzz.token_set_ratio(original_norm, street)) if original_norm else 0.0
            quality = {
                "REAL_NODE": 1.0, "GEOMETRIC_NO_NODE": 0.85,
                "SMALL_TOPOLOGY_GAP": 0.68, "PARALLEL_NEAR": 0.34,
                "NO_INTERSECTION": 0.0,
            }[intersection_type]
            proximity = math.exp(-intersection_distance / max(self.calibration.plausible_endpoint_distance_m, 1e-6))
            component = self._component_match(main, point, endpoint)
            component_score = 1.0 if component == "SAME_COMPONENT" else 0.0 if component == "WRONG_COMPONENT" else 0.5
            score = 0.50 * quality + 0.35 * lexical / 100.0 + 0.10 * proximity + 0.05 * component_score
            nodes = self.index.street_nodes(street)
            codlogs = sorted({self.graph.segments[item].codlog for item in self.graph.street_segments.get(street, ()) if self.graph.segments[item].codlog})
            result.append(BoundaryCandidate(
                name=street, street_norm=street, codlog=codlogs[0] if codlogs else "",
                lexical_similarity=lexical, intersection_type=intersection_type,
                intersection_distance_m=intersection_distance,
                distance_to_start_m=float(endpoint.distance(point)) if point is not None else intersection_distance,
                distance_to_end_m=float(other_endpoint.distance(point)) if point is not None else float(other_endpoint.distance(geometry)),
                snap_required=intersection_type in {"GEOMETRIC_NO_NODE", "SMALL_TOPOLOGY_GAP"},
                snap_distance_m=float(node_distance) if points else gap,
                component_match=component, intersection_count=len(points),
                parallel=intersection_type == "PARALLEL_NEAR", candidate_score=score,
                is_current=street == current_norm, intersection_point=point,
            ))
        return sorted(result, key=lambda item: (-item.candidate_score, item.name))

    def _side_root_causes(self, evidence: BoundaryEvidence) -> list[str]:
        selected = evidence.candidates[0] if evidence.candidates else None
        if selected is None:
            return ["BOUNDARY_NOT_IN_GEOSAMPA"]
        roots = []
        if selected.intersection_type == "GEOMETRIC_NO_NODE":
            roots.append("BOUNDARY_GEOMETRIC_INTERSECTION_NO_NODE")
        elif selected.intersection_type == "SMALL_TOPOLOGY_GAP":
            roots.append("BOUNDARY_SMALL_TOPOLOGY_GAP")
        elif selected.intersection_type == "PARALLEL_NEAR":
            roots.append("BOUNDARY_PARALLEL_STREET")
        if selected.component_match == "WRONG_COMPONENT":
            roots.append("BOUNDARY_WRONG_COMPONENT")
        if selected.intersection_count > 1:
            roots.append("MULTIPLE_INTERSECTIONS")
        if selected.is_current and selected.intersection_type == "NO_INTERSECTION":
            roots.append("BOUNDARY_RESOLVED_TO_WRONG_STREET")
        lexical = selected.lexical_similarity
        original_norm = evidence.normalized_name
        if original_norm and selected.name != original_norm:
            words_original = set(original_norm.split())
            words_selected = set(selected.name.split())
            if words_original and words_original < words_selected:
                roots.append("BOUNDARY_NAME_INCOMPLETE")
            if re.search(r"\b(R|AV|EST|ROD|TV|AL)\b", _text(evidence.original_name).upper()) or "." in _text(evidence.original_name):
                roots.append("BOUNDARY_ABBREVIATED")
            if lexical < self.calibration.lexical_plausible_threshold:
                roots.append("BOUNDARY_NAME_WRONG")
        return list(dict.fromkeys(roots)) or ["UNKNOWN"]

    def _evaluate_side(self, context: BoundaryContext, side: str) -> BoundaryEvidence:
        original = context.de if side == "DE" else context.ate
        current = context.de_current if side == "DE" else context.ate_current
        endpoint = Point(context.geometry.coords[0] if side == "DE" else context.geometry.coords[-1])
        other_endpoint = Point(context.geometry.coords[-1] if side == "DE" else context.geometry.coords[0])
        evidence = BoundaryEvidence(
            side=side, original_name=original, normalized_name=normalize_name(original),
            current_name=current, current_normalized=normalize_name(current),
            gps_distance_m=context.gps_distance_m,
        )
        main = self.main_street(context)
        if not main or self.graph is None:
            evidence.status = "NOT_FOUND"
            evidence.root_causes = ["BOUNDARY_NOT_IN_GEOSAMPA"]
            evidence.warnings.append("MAIN_STREET_NOT_FOUND_IN_READ_ONLY_GRAPH")
            return evidence
        evidence.candidates = self._candidate(main, side, original, current, endpoint, other_endpoint, context)
        if not evidence.candidates:
            evidence.status = "NOT_FOUND"
            evidence.root_causes = ["BOUNDARY_NOT_IN_GEOSAMPA"]
            return evidence
        current_candidate = next((item for item in evidence.candidates if item.is_current), None)
        best = evidence.candidates[0]
        margin = best.candidate_score - evidence.candidates[1].candidate_score if len(evidence.candidates) > 1 else 1.0
        if len(evidence.candidates) > 1 and margin <= self.calibration.candidate_margin_threshold:
            evidence.status = "AMBIGUOUS"
            evidence.warnings.append("MULTIPLE_CONTEXTUAL_BOUNDARY_CANDIDATES")
        else:
            candidate = current_candidate if current_candidate and current_candidate.candidate_score >= best.candidate_score - self.calibration.candidate_margin_threshold else best
            distance = candidate.intersection_distance_m or 0.0
            if candidate.is_current and candidate.intersection_type in {"REAL_NODE", "GEOMETRIC_NO_NODE"} and distance <= self.calibration.valid_endpoint_distance_m:
                evidence.status = "VALID"
            elif candidate.intersection_type in {"REAL_NODE", "GEOMETRIC_NO_NODE", "SMALL_TOPOLOGY_GAP"} and distance <= self.calibration.plausible_endpoint_distance_m and candidate.lexical_similarity >= self.calibration.lexical_plausible_threshold:
                evidence.status = "PLAUSIBLE"
            elif candidate.intersection_type == "PARALLEL_NEAR":
                evidence.status = "CONTRADICTORY"
            elif current_candidate is not None or original:
                evidence.status = "CONTRADICTORY"
            else:
                evidence.status = "NOT_FOUND"
        selected = best
        evidence.selected_candidate = selected.name
        evidence.selected_codlog = selected.codlog
        evidence.selected_lexical_similarity = selected.lexical_similarity
        evidence.intersection_type = selected.intersection_type
        evidence.intersection_distance_m = selected.intersection_distance_m
        evidence.distance_to_start_m = selected.distance_to_start_m
        evidence.distance_to_end_m = selected.distance_to_end_m
        evidence.snap_required = selected.snap_required
        evidence.snap_distance_m = selected.snap_distance_m
        evidence.component_match = selected.component_match
        evidence.intersection_count = selected.intersection_count
        evidence.gap_m = selected.snap_distance_m if selected.intersection_type == "SMALL_TOPOLOGY_GAP" else 0.0 if selected.intersection_type != "NO_INTERSECTION" else selected.snap_distance_m
        evidence.root_causes = self._side_root_causes(evidence)
        if evidence.status == "CONTRADICTORY" and current_candidate is not None and current_candidate.name != selected.name:
            evidence.root_causes.insert(0, "BOUNDARY_RESOLVED_TO_WRONG_STREET")
        return evidence

    def _diagnostic_path(self, main: str, de: BoundaryEvidence, ate: BoundaryEvidence):
        if not de.candidates or not ate.candidates or self.graph is None:
            return None
        point_a, point_b = de.candidates[0].intersection_point, ate.candidates[0].intersection_point
        if point_a is None or point_b is None:
            return None
        graph = self.graph.street_graphs.get(main)
        if graph is None:
            return None
        nodes = self.index.street_nodes(main)
        if not nodes:
            return None
        node_a = min(nodes, key=lambda item: item.distance(point_a))
        node_b = min(nodes, key=lambda item: item.distance(point_b))
        start, end = (node_a.x, node_a.y), (node_b.x, node_b.y)
        if start not in graph or end not in graph:
            return None
        try:
            import networkx as nx
            path_nodes = nx.shortest_path(graph, start, end, weight="length")
        except Exception:
            return None
        coordinates = []
        for left, right in zip(path_nodes, path_nodes[1:]):
            edge = graph.get_edge_data(left, right)
            if not edge:
                return None
            segment = self.graph.segments[edge["identifier"]].geometry
            first = segment.coords[0]
            if math.hypot(first[0] - left[0], first[1] - left[1]) > math.hypot(first[0] - right[0], first[1] - right[1]):
                segment = LineString(list(segment.coords)[::-1])
            part = list(segment.coords)
            coordinates.extend(part if not coordinates else part[1:] if coordinates[-1] == part[0] else part)
        return LineString(coordinates) if len(coordinates) >= 2 else None

    def _score(self, context: BoundaryContext, de: BoundaryEvidence, ate: BoundaryEvidence) -> tuple[float, int]:
        score = 0.0
        evidence_count = 0
        for item in (de, ate):
            if item.status == "VALID":
                score += 25.0
                evidence_count += 2
            elif item.status == "PLAUSIBLE":
                score += 16.0
                evidence_count += 1
            elif item.status == "AMBIGUOUS":
                score += 7.0
                evidence_count += 1
            if item.intersection_type in {"REAL_NODE", "GEOMETRIC_NO_NODE", "SMALL_TOPOLOGY_GAP"}:
                evidence_count += 1
        if de.component_match == "SAME_COMPONENT" and ate.component_match == "SAME_COMPONENT":
            score += 20.0
            evidence_count += 1
        elif de.component_match == "SAME_COMPONENT" or ate.component_match == "SAME_COMPONENT":
            score += 10.0
            evidence_count += 1
        if context.gps_status == "ON_PATH":
            score += 10.0
            evidence_count += 1
        elif context.gps_status == "NEAR_PATH":
            score += 6.0
            evidence_count += 1
        if context.extension_deviation_pct is not None:
            score += 10.0 * max(0.0, 1.0 - context.extension_deviation_pct / 50.0)
            evidence_count += 1
        if de.candidates and ate.candidates:
            margin = min(
                de.candidates[0].candidate_score - (de.candidates[1].candidate_score if len(de.candidates) > 1 else 0.0),
                ate.candidates[0].candidate_score - (ate.candidates[1].candidate_score if len(ate.candidates) > 1 else 0.0),
            )
            if margin > self.calibration.candidate_margin_threshold:
                score += 10.0
            elif margin > 0:
                score += 4.0
            evidence_count += 1
        return round(min(100.0, score), 6), evidence_count

    def validate(self, context: BoundaryContext) -> BoundaryContradictionResult:
        if context.geometry is None or not isinstance(context.geometry, (LineString, MultiLineString)):
            de = BoundaryEvidence("DE", original_name=context.de, normalized_name=normalize_name(context.de), status="NOT_FOUND", root_causes=["UNKNOWN"])
            ate = BoundaryEvidence("ATE", original_name=context.ate, normalized_name=normalize_name(context.ate), status="NOT_FOUND", root_causes=["UNKNOWN"])
        else:
            line = _as_line(context.geometry)
            de, ate = self._evaluate_side(context, "DE"), self._evaluate_side(context, "ATE")
        reversed_order = bool(
            de.candidates and ate.candidates
            and de.candidates[0].distance_to_end_m is not None and ate.candidates[0].distance_to_start_m is not None
            and de.candidates[0].distance_to_end_m <= self.calibration.plausible_endpoint_distance_m
            and ate.candidates[0].distance_to_start_m <= self.calibration.plausible_endpoint_distance_m
            and (de.distance_to_start_m or 0.0) > self.calibration.plausible_endpoint_distance_m
            and (ate.distance_to_end_m or 0.0) > self.calibration.plausible_endpoint_distance_m
        )
        main = self.main_street(context)
        diagnostic = self._diagnostic_path(main, de, ate) if de.recovered() and ate.recovered() else None
        current = _as_line(context.geometry)
        overlap = None
        length_difference = None
        hausdorff = None
        if diagnostic is not None and current is not None and current.length:
            overlap = round(float(diagnostic.intersection(current).length / current.length * 100.0), 6)
            length_difference = round(abs(diagnostic.length - current.length) / current.length * 100.0, 6)
            hausdorff = round(float(diagnostic.hausdorff_distance(current)), 6)
        score, evidence_count = self._score(context, de, ate)
        if reversed_order:
            root = "BOUNDARIES_REVERSED"
        elif normalize_name(context.de) and normalize_name(context.de) == normalize_name(context.ate):
            root = "SAME_TRANSVERSAL"
        elif any(term in (normalize_name(context.de) + " " + normalize_name(context.ate)) for term in ("TODA EXTENSAO", "TODA A EXTENSAO")):
            root = "VIA_INTEIRA"
        elif any(term in (normalize_name(context.de) + " " + normalize_name(context.ate)) for term in ("FIM DA VIA", "FINAL DA VIA")):
            root = "FIM_DA_VIA"
        elif de.recovered() and ate.recovered():
            root = "BOUNDARIES_RECOVERED_STRONG" if de.status == "VALID" and ate.status == "VALID" else "BOUNDARIES_RECOVERED_MEDIUM"
        elif de.recovered():
            root = "ONLY_DE_VALID"
        elif ate.recovered():
            root = "ONLY_ATE_VALID"
        elif de.status == "NOT_FOUND" and ate.status == "NOT_FOUND":
            root = "BOUNDARY_NOT_IN_GEOSAMPA"
        else:
            root = "BOTH_INVALID"
        secondary = list(dict.fromkeys(de.root_causes + ate.root_causes))
        if reversed_order:
            secondary.append("BOUNDARIES_REVERSED")
        if de.intersection_type == "GEOMETRIC_NO_NODE" or ate.intersection_type == "GEOMETRIC_NO_NODE":
            secondary.append("BOUNDARY_GEOMETRIC_INTERSECTION_NO_NODE")
        if de.intersection_type == "SMALL_TOPOLOGY_GAP" or ate.intersection_type == "SMALL_TOPOLOGY_GAP":
            secondary.append("BOUNDARY_SMALL_TOPOLOGY_GAP")
        if de.component_match == "WRONG_COMPONENT" or ate.component_match == "WRONG_COMPONENT":
            secondary.append("BOUNDARY_WRONG_COMPONENT")
        secondary = list(dict.fromkeys(secondary))
        if root == "SAME_TRANSVERSAL":
            recommendation = "KEEP_CONTRADICTION"
        elif reversed_order:
            recommendation = "BOUNDARIES_REVERSED"
        elif de.recovered() and ate.recovered() and score >= self.calibration.high_score_threshold and evidence_count >= self.calibration.high_evidence_threshold:
            recommendation = "BOUNDARIES_VALIDATED_HIGH"
        elif de.recovered() and ate.recovered() and score >= self.calibration.medium_score_threshold and evidence_count >= self.calibration.medium_evidence_threshold:
            recommendation = "BOUNDARIES_VALIDATED_MEDIUM"
        elif de.recovered() or ate.recovered():
            recommendation = "ONE_BOUNDARY_VALIDATED"
        elif evidence_count < self.calibration.medium_evidence_threshold:
            recommendation = "DATA_INSUFFICIENT"
        else:
            recommendation = "KEEP_CONTRADICTION"
        reason = f"{root}; De={de.status}; Até={ate.status}; score={score:.2f}"
        warnings = list(dict.fromkeys(de.warnings + ate.warnings))
        return BoundaryContradictionResult(
            record_id=context.record_id, source_type=context.source_type, control_label=context.control_label,
            via=context.via, de_original=context.de, ate_original=context.ate,
            de_current=context.de_current, ate_current=context.ate_current,
            de_status=de.status, ate_status=ate.status,
            de_candidate=de.selected_candidate, ate_candidate=ate.selected_candidate,
            de_codlog=de.selected_codlog, ate_codlog=ate.selected_codlog,
            de_intersection_type=de.intersection_type, ate_intersection_type=ate.intersection_type,
            de_distance_m=de.intersection_distance_m, ate_distance_m=ate.intersection_distance_m,
            de_snap_distance_m=de.snap_distance_m, ate_snap_distance_m=ate.snap_distance_m,
            de_gps_distance_m=de.gps_distance_m, ate_gps_distance_m=ate.gps_distance_m,
            boundaries_reversed=reversed_order, root_cause=root, secondary_causes=secondary,
            boundary_validation_score=score, boundary_evidence_count=evidence_count,
            recovered_de=de.selected_candidate if de.recovered() else "",
            recovered_ate=ate.selected_candidate if ate.recovered() else "",
            recovered_both=de.recovered() and ate.recovered(),
            candidate_geometry_wkt=context.geometry.wkt if context.geometry is not None else "",
            diagnostic_geometry_wkt=diagnostic.wkt if diagnostic is not None else "",
            overlap_with_current_pct=overlap, length_difference_pct=length_difference,
            hausdorff_distance_m=hausdorff, recommendation=recommendation,
            requires_review=recommendation not in {"BOUNDARIES_VALIDATED_HIGH", "BOUNDARIES_VALIDATED_MEDIUM"},
            reason=reason, warnings=warnings,
            de_candidates=[item.to_dict() for item in de.candidates[:10]],
            ate_candidates=[item.to_dict() for item in ate.candidates[:10]],
        )


def context_from_row(row: Mapping[str, Any], quality_row: Mapping[str, Any] | None = None, source_type: str = "ESTIMATED", control_label: str = "") -> BoundaryContext:
    quality_row = quality_row or {}
    geometry = parse_wkt(row.get("geometry_wkt"))
    de_current = _text(quality_row.get("de_candidate") or quality_row.get("de_resolved") or row.get("de"))
    ate_current = _text(quality_row.get("ate_candidate") or quality_row.get("ate_resolved") or row.get("ate"))
    return BoundaryContext(
        record_id=_text(row.get("id")), via=_text(row.get("via")),
        via_resolved=_text(row.get("via_resolvida") or row.get("main_street_expected")),
        de=_text(row.get("de")), ate=_text(row.get("ate")), de_current=de_current, ate_current=ate_current,
        geometry=geometry, extension_m=_number(row.get("extensao_m")),
        latitude=_number(row.get("latitude")), longitude=_number(row.get("longitude")),
        gps_status=_text(row.get("gps_status")), gps_distance_m=_number(row.get("gps_distance_m")),
        extension_deviation_pct=_number(row.get("extension_deviation_pct")),
        main_street=_text(row.get("main_street_expected") or row.get("main_street")),
        root_cause_primary=_text(row.get("root_cause_primary")), source_type=source_type, control_label=control_label,
    )


def load_target_rows(sample: int | None = None, only_root_cause: list[str] | None = None, only_id: list[str] | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    validation = pd.read_csv(VALIDATION_INPUT, dtype=str, encoding="utf-8-sig", keep_default_na=False)
    quality = pd.read_csv(QUALITY_INPUT, dtype=str, encoding="utf-8-sig", keep_default_na=False).set_index("id")
    mask = validation.hard_failures.str.contains("BOUNDARY_CONTRADICTION", regex=False) | validation.de_status.eq("CONTRADICTED") | validation.ate_status.eq("CONTRADICTED")
    validation = validation[mask].copy()
    if only_root_cause:
        validation = validation[validation.root_cause_primary.isin(only_root_cause)].copy()
    if only_id:
        validation = validation[validation.id.isin(only_id)].copy()
    validation = validation.sort_values("id", key=lambda series: series.astype(str))
    if sample:
        validation = validation.head(sample)
    return validation, quality


def load_official_controls(limit: int | None = None) -> list[BoundaryContext]:
    frame = pd.read_csv(OFFICIAL_INPUT, dtype=str, encoding="utf-8-sig", keep_default_na=False)
    controls = []
    for _, row in frame.iterrows():
        geometry = parse_official_path(row.get("path"))
        if geometry is None or not _text(row.get("de")) and not _text(row.get("ate")):
            continue
        values = row.to_dict()
        values["geometry_wkt"] = geometry.wkt
        controls.append(context_from_row(values, source_type="OFFICIAL_POSITIVE", control_label="OFFICIAL_GEOMETRY"))
    controls.sort(key=lambda item: hashlib.sha1(item.record_id.encode()).hexdigest())
    return controls[:limit] if limit else controls


def split_controls(controls: list[BoundaryContext]) -> tuple[list[BoundaryContext], list[BoundaryContext]]:
    calibration, validation = [], []
    for item in controls:
        bucket = int(hashlib.sha1(item.record_id.encode()).hexdigest()[:8], 16) % 10
        (calibration if bucket < 7 else validation).append(item)
    return calibration, validation


def _replace_context(context: BoundaryContext, **changes) -> BoundaryContext:
    values = asdict(context)
    values.update(changes)
    return BoundaryContext(**values)


def make_negative_controls(controls: list[BoundaryContext], max_per_label: int = 50) -> list[BoundaryContext]:
    result = []
    for index, control in enumerate(controls[:max_per_label]):
        other = controls[(index + 1) % len(controls)] if controls else control
        if not controls:
            continue
        if control.de and other.de and normalize_name(control.de) != normalize_name(other.de):
            swap_de = other.de
        else:
            swap_de = other.ate or "RUA INEXISTENTE"
        if control.ate and other.ate and normalize_name(control.ate) != normalize_name(other.ate):
            swap_ate = other.ate
        else:
            swap_ate = other.de or "RUA INEXISTENTE"
        substitutions = {
            "SWAP_DE": {"de": swap_de},
            "SWAP_ATE": {"ate": swap_ate},
            "INVERT_ONE_BOUNDARY": {"de": control.ate or swap_de},
            "PARALLEL_STREET": {"de": swap_de, "ate": swap_ate},
            "WRONG_COMPONENT": {"geometry": affinity.translate(control.geometry, xoff=2000.0, yoff=1500.0)},
            "NEAR_WRONG_BOUNDARY": {"de": swap_de, "ate": control.ate},
        }
        for label, changes in substitutions.items():
            result.append(_replace_context(control, **changes, record_id=f"{control.record_id}::{label}", source_type="SYNTHETIC_NEGATIVE", control_label=label))
    return result


def calibrate(controls: list[BoundaryContext], graph) -> tuple[BoundaryCalibration, dict[str, Any]]:
    calibration_controls, validation_controls = split_controls(controls)
    provisional = BoundaryContradictionAuditEngine(graph)
    raw_results = [provisional.validate(item) for item in calibration_controls]
    usable_types = {"REAL_NODE", "GEOMETRIC_NO_NODE", "SMALL_TOPOLOGY_GAP"}
    distances = [
        value for result in raw_results
        for value, intersection_type in ((result.de_distance_m, result.de_intersection_type), (result.ate_distance_m, result.ate_intersection_type))
        if value is not None and intersection_type in usable_types
    ]
    gap_values = [
        value for result in raw_results
        for value, intersection_type in ((result.de_snap_distance_m, result.de_intersection_type), (result.ate_snap_distance_m, result.ate_intersection_type))
        if value is not None and intersection_type == "SMALL_TOPOLOGY_GAP"
    ]
    node_values = [
        value for result in raw_results
        for value, intersection_type in ((result.de_snap_distance_m, result.de_intersection_type), (result.ate_snap_distance_m, result.ate_intersection_type))
        if value is not None and intersection_type == "REAL_NODE"
    ]
    calibration = BoundaryCalibration(
        positive_calibration_count=len(calibration_controls), positive_validation_count=len(validation_controls),
        negative_calibration_count=min(len(calibration_controls), 50) * len(NEGATIVE_LABELS),
        negative_validation_count=min(len(validation_controls), 50) * len(NEGATIVE_LABELS),
        valid_endpoint_distance_m=_percentile(distances, 95, 12.0),
        plausible_endpoint_distance_m=_percentile(distances, 99, 30.0),
        small_gap_distance_m=_percentile(gap_values, 95, 8.0),
        node_tolerance_m=_percentile(node_values, 99, 8.0),
        parallel_distance_m=20.0,
        lexical_plausible_threshold=70.0,
        high_score_threshold=_percentile([result.boundary_validation_score for result in raw_results], 75, 70.0),
        medium_score_threshold=_percentile([result.boundary_validation_score for result in raw_results], 25, 50.0),
        high_evidence_threshold=max(5, int(math.ceil(_percentile([result.boundary_evidence_count for result in raw_results], 75, 6)))),
        medium_evidence_threshold=max(3, int(math.ceil(_percentile([result.boundary_evidence_count for result in raw_results], 25, 3)))),
        candidate_margin_threshold=0.08,
    )
    engine = BoundaryContradictionAuditEngine(graph, calibration)
    positive_validation = [engine.validate(item) for item in validation_controls]
    positive_calibration = [engine.validate(item) for item in calibration_controls]
    negative_calibration_contexts = make_negative_controls(calibration_controls)
    negative_validation_contexts = make_negative_controls(validation_controls)
    negative_calibration = [engine.validate(item) for item in negative_calibration_contexts]
    negative_validation = [engine.validate(item) for item in negative_validation_contexts]

    def metrics(results, kind):
        accepted = sum(result.recommendation in {"BOUNDARIES_VALIDATED_HIGH", "BOUNDARIES_VALIDATED_MEDIUM"} for result in results)
        rejected = sum(result.recommendation in {"KEEP_CONTRADICTION", "DATA_INSUFFICIENT"} for result in results)
        return {
            "control_kind": kind, "total": len(results), "accepted": accepted, "rejected": rejected,
            "acceptance_rate": accepted / len(results) if results else None,
            "rejection_rate": rejected / len(results) if results else None,
            "false_acceptance_rate": accepted / len(results) if kind == "negative" and results else None,
        }
    return calibration, {
        "positive_calibration": metrics(positive_calibration, "positive"),
        "positive_validation": metrics(positive_validation, "positive"),
        "negative_calibration": metrics(negative_calibration, "negative"),
        "negative_validation": metrics(negative_validation, "negative"),
        "negative_validation_by_label": {label: metrics([item for item in negative_validation if item.control_label == label], "negative") for label in NEGATIVE_LABELS},
    }


def _hashes() -> dict[str, str | None]:
    paths = [PROCESSED / name for name in ("recape_clean.csv", "notificacoes.csv", "cruzamento.csv", "recapes_sem_cobertura.csv", "geosampa_coverage_report.json", "pipeline_run.json")]
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


def _distribution(rows: pd.DataFrame, field: str) -> dict[str, int]:
    if field not in rows:
        return {}
    return {str(key): int(value) for key, value in rows[field].fillna("").value_counts().items()}


def run_shadow(args: argparse.Namespace) -> dict[str, Any]:
    PROCESSED.mkdir(parents=True, exist_ok=True)
    if args.reset_cache:
        for path in (OUTPUT_CSV, OUTPUT_REPORT, LOCAL_CACHE):
            if path.exists():
                path.unlink()
    before = _hashes()
    targets, quality = load_target_rows(args.sample, args.only_root_cause, args.only_id)
    control_limit = max(args.sample or 0, 100) if args.sample else None
    controls = load_official_controls(control_limit)
    graph = load_read_only_graph()
    calibration, control_report = calibrate(controls, graph)
    engine = BoundaryContradictionAuditEngine(graph, calibration)
    existing = {}
    if args.resume and OUTPUT_CSV.exists():
        old = pd.read_csv(OUTPUT_CSV, dtype=str, encoding="utf-8-sig", keep_default_na=False)
        existing = {str(row["id"]): row.to_dict() for _, row in old.iterrows()}
    rows = []
    for _, row in targets.iterrows():
        record_id = _text(row.get("id"))
        if record_id in existing:
            rows.append(existing[record_id])
            continue
        qrow = quality.loc[record_id].to_dict() if record_id in quality.index else {}
        result = engine.validate(context_from_row(row.to_dict(), qrow))
        rows.append(result.to_row())
    output = pd.DataFrame(rows)
    if not output.empty:
        output.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")
    after = _hashes()
    if before != after:
        raise RuntimeError("output oficial alterado durante boundary shadow")
    class_counts = _distribution(output, "recommendation")
    official_total = int((json.loads(QUALITY_REPORT.read_text(encoding="utf-8")).get("scope") or {}).get("total_recapes") or 0) if QUALITY_REPORT.exists() else 0
    report = {
        "mode": "SHADOW_ONLY", "audit_version": "boundary-contradiction-audit-v1",
        "population_source": str(VALIDATION_INPUT.relative_to(ROOT)), "total_cases": int(len(output)),
        "official_outputs_unchanged": before == after, "official_output_hashes_before": before, "official_output_hashes_after": after,
        "graph_loaded_read_only": graph is not None, "official_promotion_applied": False,
        "de_invalid": int((output.de_status.isin(["CONTRADICTORY", "NOT_FOUND"]) if not output.empty else pd.Series(dtype=bool)).sum()),
        "ate_invalid": int((output.ate_status.isin(["CONTRADICTORY", "NOT_FOUND"]) if not output.empty else pd.Series(dtype=bool)).sum()),
        "both_invalid": int(((output.de_status.isin(["CONTRADICTORY", "NOT_FOUND"]) & output.ate_status.isin(["CONTRADICTORY", "NOT_FOUND"])) if not output.empty else pd.Series(dtype=bool)).sum()),
        "de_recovered": int(output.recovered_de.ne("").sum()) if not output.empty else 0,
        "ate_recovered": int(output.recovered_ate.ne("").sum()) if not output.empty else 0,
        "both_recovered": int(output.recovered_both.astype(str).str.lower().eq("true").sum()) if not output.empty else 0,
        "reversed": int(output.boundaries_reversed.astype(str).str.lower().eq("true").sum()) if not output.empty else 0,
        "geometric_no_node": int((output.de_intersection_type.eq("GEOMETRIC_NO_NODE") | output.ate_intersection_type.eq("GEOMETRIC_NO_NODE")).sum()) if not output.empty else 0,
        "small_gap": int((output.de_intersection_type.eq("SMALL_TOPOLOGY_GAP") | output.ate_intersection_type.eq("SMALL_TOPOLOGY_GAP")).sum()) if not output.empty else 0,
        "wrong_component": int(output.secondary_causes.fillna("").str.contains("BOUNDARY_WRONG_COMPONENT", regex=False).sum()) if not output.empty else 0,
        "parallel_street": int(output.secondary_causes.fillna("").str.contains("BOUNDARY_PARALLEL_STREET", regex=False).sum()) if not output.empty else 0,
        "name_problem": int(output.secondary_causes.fillna("").str.contains("BOUNDARY_NAME_", regex=True).sum()) if not output.empty else 0,
        "data_problem": int(output.root_cause.isin(["BOUNDARY_NOT_IN_GEOSAMPA", "BOTH_INVALID"]).sum()) if not output.empty else 0,
        "high_boundary_validation": int(output.recommendation.eq("BOUNDARIES_VALIDATED_HIGH").sum()) if not output.empty else 0,
        "medium_boundary_validation": int(output.recommendation.eq("BOUNDARIES_VALIDATED_MEDIUM").sum()) if not output.empty else 0,
        "insufficient": int(output.recommendation.eq("DATA_INSUFFICIENT").sum()) if not output.empty else 0,
        "by_root_cause": _distribution(output, "root_cause"), "by_recommendation": class_counts,
        "control_positive_results": control_report.get("positive_validation"), "control_negative_results": control_report.get("negative_validation"),
        "control_report": control_report, "thresholds": calibration.to_dict(),
        "calibration_metrics": control_report,
        "impact_simulation": {
            "high_cases": int(output.recommendation.eq("BOUNDARIES_VALIDATED_HIGH").sum()) if not output.empty else 0,
            "high_plus_medium_cases": int(output.recommendation.isin(["BOUNDARIES_VALIDATED_HIGH", "BOUNDARIES_VALIDATED_MEDIUM"]).sum()) if not output.empty else 0,
            "official_promotions": 0, "coverage_gain_high_upper_bound_pp": round(output.recommendation.eq("BOUNDARIES_VALIDATED_HIGH").sum() / official_total * 100, 6) if official_total else None,
            "coverage_gain_high_medium_upper_bound_pp": round(output.recommendation.isin(["BOUNDARIES_VALIDATED_HIGH", "BOUNDARIES_VALIDATED_MEDIUM"]).sum() / official_total * 100, 6) if official_total else None,
            "not_an_official_projection": True,
        },
        "prioritization": prioritize(output),
        "human_labels_sufficient": False,
    }
    OUTPUT_REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    LOCAL_CACHE.write_text(json.dumps({"calibration": calibration.to_dict(), "input_hashes": before}, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def prioritize(output: pd.DataFrame) -> list[dict[str, Any]]:
    if output.empty:
        return []
    groups = output.groupby("root_cause", dropna=False).agg(cases=("id", "size"), recovered=("recovered_both", lambda values: sum(str(value).lower() == "true" for value in values)), high=("recommendation", lambda values: sum(value == "BOUNDARIES_VALIDATED_HIGH" for value in values)), medium=("recommendation", lambda values: sum(value == "BOUNDARIES_VALIDATED_MEDIUM" for value in values))).reset_index()
    result = []
    for _, row in groups.iterrows():
        cases = int(row.cases)
        recovery_rate = (int(row.recovered) + int(row.high) + int(row.medium)) / cases if cases else 0.0
        risk = 1.0 if row.root_cause in {"BOTH_INVALID", "BOUNDARY_NOT_IN_GEOSAMPA"} else 0.5
        result.append({"root_cause": _text(row.root_cause), "cases": cases, "recovered_or_validated": int(row.recovered) + int(row.high) + int(row.medium), "recovery_rate": round(recovery_rate, 6), "risk": risk, "priority_score": round(cases * recovery_rate / max(risk, 0.1), 6), "future_heuristic": False})
    return sorted(result, key=lambda item: (-item["priority_score"], -item["cases"]))


def build_parser():
    parser = argparse.ArgumentParser(description="Auditoria shadow de contradições de De/Até")
    parser.add_argument("--shadow", action="store_true")
    parser.add_argument("--sample", type=int, default=None)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--reset-cache", action="store_true")
    parser.add_argument("--only-root-cause", action="append", default=[])
    parser.add_argument("--only-id", action="append", default=[])
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.shadow:
        print("Modo seguro: use --shadow; nenhum arquivo foi alterado.", file=sys.stderr)
        return 2
    report = run_shadow(args)
    print(json.dumps({"processed": report["total_cases"], "root_causes": report["by_root_cause"], "official_outputs_unchanged": report["official_outputs_unchanged"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
