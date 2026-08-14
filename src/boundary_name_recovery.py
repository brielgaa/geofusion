"""Shadow-only diagnostic recovery of De/Até street names.

This module is deliberately independent from the production resolver and from
the ETL.  It reads the persisted boundary contradiction audit, searches only
the local geometric context of a main street, and writes diagnostic artifacts
under ``data/processed``.  It never writes aliases, official geometries, or
production classifications.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import pickle
import re
import sys
import time
import unicodedata
from dataclasses import asdict, dataclass, field, replace
from numbers import Integral
from pathlib import Path
from typing import Any, Iterable, Mapping

import pandas as pd
from pyproj import Transformer
from rapidfuzz import fuzz
from shapely import affinity
from shapely.geometry import LineString, MultiLineString, Point
from shapely.ops import transform, unary_union
from shapely.wkt import loads as load_wkt

try:
    from road_graph import RoadGraph
except ImportError:  # pragma: no cover - package import fallback
    from .road_graph import RoadGraph


ROOT = Path(__file__).resolve().parents[1]
PROCESSED = ROOT / "data" / "processed"
CACHE_DIR = ROOT / "data" / "cache"
AUDIT_INPUT = PROCESSED / "boundary_contradiction_audit.csv"
QUALITY_INPUT = PROCESSED / "route_geometry_quality_shadow.csv"
OFFICIAL_INPUT = PROCESSED / "recape_clean.csv"
GRAPH_CACHE = CACHE_DIR / "geosampa_road_graph.pkl"
GRAPH_SOURCE = CACHE_DIR / "geosampa_segmento_logradouro.geojson"
ALIASES_INPUT = ROOT / "data" / "config" / "street_aliases.csv"
OUTPUT_CSV = PROCESSED / "boundary_name_recovery.csv"
OUTPUT_REPORT = PROCESSED / "boundary_name_recovery_report.json"
ALIAS_OUTPUT = PROCESSED / "boundary_alias_candidates.csv"
LOCAL_CACHE = PROCESSED / "boundary_name_recovery_cache.json"
VERSION = "boundary-name-recovery-shadow-v1"

METRIC_TO_WGS84 = Transformer.from_crs("EPSG:31983", "EPSG:4326", always_xy=True)
WGS84_TO_METRIC = Transformer.from_crs("EPSG:4326", "EPSG:31983", always_xy=True)

NAME_MARKERS = (
    "BOUNDARY_NAME_WRONG",
    "BOUNDARY_NAME_INCOMPLETE",
    "BOUNDARY_ABBREVIATED",
)
STRUCTURAL_TOKENS = {
    "RUA", "AVENIDA", "ALAMEDA", "TRAVESSA", "ESTRADA", "RODOVIA",
    "PRACA", "LARGO", "VIELA", "VIADUTO", "BECO", "PASSAGEM",
}
AUXILIARY_TOKENS = {"A", "AS", "AO", "AOS", "DA", "DAS", "DE", "DO", "DOS", "E", "EM"}
TYPE_MAP = {
    "R": "RUA", "RUA": "RUA", "AV": "AVENIDA", "AVENIDA": "AVENIDA",
    "AL": "ALAMEDA", "ALAMEDA": "ALAMEDA", "TV": "TRAVESSA", "TRAVESSA": "TRAVESSA",
    "EST": "ESTRADA", "ESTR": "ESTRADA", "ESTRADA": "ESTRADA", "ROD": "RODOVIA", "RODOVIA": "RODOVIA",
    "PC": "PRACA", "PRACA": "PRACA", "LGO": "LARGO", "LARGO": "LARGO",
    "VL": "VIELA", "VIELA": "VIELA", "VD": "VIADUTO", "VIADUTO": "VIADUTO",
}
ABBREVIATIONS = {
    "DR": "DOUTOR", "DRA": "DOUTORA", "PROF": "PROFESSOR", "PROFA": "PROFESSORA",
    "PRES": "PRESIDENTE", "DEP": "DEPUTADO", "ENG": "ENGENHEIRO", "ENGO": "ENGENHEIRO",
    "PE": "PADRE", "STA": "SANTA", "STO": "SANTO", "S": "SAO", "CEL": "CORONEL",
    "CAP": "CAPITAO", "GEN": "GENERAL", "EMB": "EMBAIXADOR",
}
OPERATIONAL_TERMS = ("TODA EXTENSAO", "TODA A EXTENSAO", "NO FINAL", "ATE O FINAL", "ALTURA")
RECOVERED_CLASSES = {"NAME_RECOVERED_HIGH", "NAME_RECOVERED_MEDIUM"}


def _text(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
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


def _json(value: Any, default: Any = None) -> Any:
    text = _text(value)
    if not text or text.lower() in {"none", "null", "nan"}:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def normalize_name(value: Any) -> str:
    """Normalize only lexical form; no aliases or city-wide fuzzy lookup."""
    raw = _text(value)
    if not raw:
        return ""
    text = unicodedata.normalize("NFKD", raw.upper()).encode("ascii", "ignore").decode("ascii")
    text = re.split(r"\s+-\s+|,\s*|/\s*|\s+\(", text, maxsplit=1)[0]
    text = re.sub(r"[^A-Z0-9\s.]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    first = text.split(" ", 1)[0].rstrip(".") if text else ""
    if first in TYPE_MAP:
        text = text[len(text.split(" ", 1)[0]):].strip() if " " in text else ""
    tokens = []
    for token in text.split():
        tokens.append(ABBREVIATIONS.get(token.rstrip("."), token.rstrip(".")))
    return re.sub(r"\s+", " ", " ".join(tokens)).strip()


def _raw_type(value: Any) -> str:
    raw = unicodedata.normalize("NFKD", _text(value).upper()).encode("ascii", "ignore").decode("ascii")
    raw = re.sub(r"[^A-Z0-9.\s]", " ", raw).strip()
    if not raw:
        return ""
    return TYPE_MAP.get(raw.split()[0].rstrip("."), "")


def _tokens(value: Any) -> list[str]:
    return [token for token in normalize_name(value).split() if token]


def _critical_tokens(value: Any) -> list[str]:
    return [token for token in _tokens(value) if token not in AUXILIARY_TOKENS and token not in STRUCTURAL_TOKENS]


def _parse_wkt(value: Any):
    text = _text(value)
    if not text:
        return None
    try:
        geometry = load_wkt(text)
        return geometry if geometry is not None and not geometry.is_empty else None
    except Exception:
        return None


def _parse_path(value: Any):
    payload = _json(value, [])
    coordinates = []
    if isinstance(payload, list):
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
        return [part for part in geometry.geoms if not part.is_empty]
    return [part for part in getattr(geometry, "geoms", ()) if isinstance(part, LineString) and not part.is_empty]


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
    for item in getattr(geometry, "geoms", ()):
        result.extend(_points(item))
    return result


class _ReadOnlyGraphUnpickler(pickle.Unpickler):
    def find_class(self, module, name):
        if module == "__main__" and name == "normalizar_rua":
            return normalize_name
        return super().find_class(module, name)


def load_read_only_graph():
    """Load the persisted graph without invoking production construction code."""
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


def _wilson(successes: int, total: int, z: float = 1.96) -> tuple[float | None, float | None]:
    if total <= 0:
        return None, None
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    radius = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / denominator
    return max(0.0, centre - radius), min(1.0, centre + radius)


@dataclass
class BoundaryNameContext:
    record_id: str
    boundary_side: str
    via: str
    main_street: str
    original_name: str
    current_candidate: str = ""
    geometry: Any = None
    latitude: float | None = None
    longitude: float | None = None
    current_status: str = ""
    root_cause: str = ""
    secondary_causes: str = ""
    context_candidates: list[dict[str, Any]] = field(default_factory=list)
    source_type: str = "ESTIMATED"
    expected_name: str = ""
    corruption_kind: str = ""

    @property
    def endpoint(self) -> Point | None:
        if self.geometry is None or self.geometry.is_empty:
            return None
        if self.boundary_side == "DE":
            return Point(self.geometry.coords[0]) if hasattr(self.geometry, "coords") else _points(self.geometry)[0]
        return Point(self.geometry.coords[-1]) if hasattr(self.geometry, "coords") else _points(self.geometry)[-1]

    @property
    def gps_point(self) -> Point | None:
        if self.latitude is None or self.longitude is None:
            return None
        try:
            x, y = WGS84_TO_METRIC.transform(self.longitude, self.latitude)
            return Point(x, y)
        except Exception:
            return None


@dataclass
class BoundaryNameCandidate:
    street_name: str
    street_norm: str
    codlog: str = ""
    distance_to_gps_m: float | None = None
    intersection_count: int = 0
    geometric_intersection: bool = False
    intersection_type: str = "NO_INTERSECTION"
    snap_distance_m: float | None = None
    component_match: str = "UNKNOWN"
    lexical_score: float = 0.0
    critical_token_score: float = 0.0
    token_coverage: float = 0.0
    length_ratio: float | None = None
    name_score: float = 0.0
    critical_exact: bool = False
    problem_types: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BoundaryNameRecoveryResult:
    record_id: str
    boundary_side: str
    via: str
    original_name: str
    normalized_original: str
    current_candidate: str
    current_status: str
    recovered_name: str
    recovered_codlog: str
    problem_types: list[str]
    lexical_score: float | None
    critical_token_score: float | None
    token_coverage: float | None
    distance_to_gps_m: float | None
    intersection_type: str
    intersection_count: int
    snap_distance_m: float | None
    component_match: str
    name_score: float | None
    margin_top2: float | None
    classification: str
    reason: str
    warnings: list[str]
    alternatives: list[dict[str, Any]] = field(default_factory=list)
    source_type: str = "ESTIMATED"
    expected_name: str = ""
    corruption_kind: str = ""

    def to_row(self) -> dict[str, Any]:
        return {
            "id": self.record_id,
            "boundary_side": self.boundary_side,
            "via": self.via,
            "original_name": self.original_name,
            "normalized_original": self.normalized_original,
            "current_candidate": self.current_candidate,
            "current_status": self.current_status,
            "recovered_name": self.recovered_name,
            "recovered_codlog": self.recovered_codlog,
            "problem_types": "|".join(self.problem_types),
            "lexical_score": self.lexical_score,
            "critical_token_score": self.critical_token_score,
            "token_coverage": self.token_coverage,
            "distance_to_gps_m": self.distance_to_gps_m,
            "intersection_type": self.intersection_type,
            "intersection_count": self.intersection_count,
            "snap_distance_m": self.snap_distance_m,
            "component_match": self.component_match,
            "name_score": self.name_score,
            "margin_top2": self.margin_top2,
            "name_margin_top2": self.margin_top2,
            "classification": self.classification,
            "reason": self.reason,
            "warnings": "|".join(self.warnings),
            "alternatives_json": json.dumps(self.alternatives[:10], ensure_ascii=False, sort_keys=True),
            "source_type": self.source_type,
            "expected_name": self.expected_name,
            "corruption_kind": self.corruption_kind,
        }


class BoundaryNameSpatialIndex:
    """Small read-only view over the graph, scoped to local context."""

    def __init__(self, graph, cache: Mapping[str, Any] | None = None):
        self.graph = graph
        self.street_geometry_cache: dict[str, Any] = {}
        self.street_display_cache: dict[str, str] = {}
        self.intersection_cache: dict[tuple[str, str], dict[str, Any]] = {}
        self.neighborhood_cache: dict[str, list[str]] = {}
        self.persisted = dict(cache or {})
        persisted_neighborhood = self.persisted.get("neighborhood") or {}
        if isinstance(persisted_neighborhood, dict):
            self.neighborhood_cache = {
                str(key): [str(value) for value in values]
                for key, values in persisted_neighborhood.items()
                if isinstance(values, list)
            }

    def _ids_for_geometry(self, geometry) -> list[str]:
        if self.graph is None or geometry is None or getattr(self.graph, "_tree", None) is None:
            return []
        try:
            hits = self.graph._tree.query(geometry)
        except Exception:
            return []
        identifiers = list(self.graph.segments)
        result = []
        for hit in hits:
            if isinstance(hit, Integral):
                if 0 <= int(hit) < len(identifiers):
                    result.append(identifiers[int(hit)])
            else:
                index = getattr(self.graph, "_geometry_index", {}).get(id(hit))
                if index is not None and index < len(identifiers):
                    result.append(identifiers[index])
        return result

    def street_geometry(self, street: str):
        if street in self.street_geometry_cache:
            return self.street_geometry_cache[street]
        if self.graph is None:
            return None
        parts = [self.graph.segments[item].geometry for item in self.graph.street_segments.get(street, ())]
        geometry = unary_union(parts) if parts else None
        self.street_geometry_cache[street] = geometry
        return geometry

    def street_display(self, street: str) -> str:
        if street in self.street_display_cache:
            return self.street_display_cache[street]
        if self.graph is None:
            return street
        names = [
            _text(self.graph.segments[item].street_name)
            for item in self.graph.street_segments.get(street, ())
            if _text(self.graph.segments[item].street_name)
        ]
        value = sorted(names, key=lambda item: (-len(item), item))[0] if names else street
        self.street_display_cache[street] = value
        return value

    def street_codlog(self, street: str) -> str:
        if self.graph is None:
            return ""
        values = sorted({
            _text(self.graph.segments[item].codlog)
            for item in self.graph.street_segments.get(street, ())
            if _text(self.graph.segments[item].codlog)
        })
        return values[0] if values else ""

    def local_streets(self, main: str, endpoint: Point | None, geometry, radius: float = 350.0,
                      hints: Iterable[str] = ()) -> list[str]:
        if self.graph is None:
            return []
        key = json.dumps({
            "main": main,
            "endpoint": [round(endpoint.x, 1), round(endpoint.y, 1)] if endpoint else None,
            "length": round(float(geometry.length), 1) if geometry is not None else None,
        }, sort_keys=True)
        if key in self.neighborhood_cache:
            return list(self.neighborhood_cache[key])
        names: set[str] = set()
        area = None
        if endpoint is not None:
            area = endpoint.buffer(radius)
        if geometry is not None:
            path_area = geometry.buffer(35.0)
            area = path_area if area is None else area.union(path_area)
        for identifier in self._ids_for_geometry(area):
            street = _text(self.graph.segments[identifier].street_norm)
            if street and street != main:
                names.add(street)
        for value in hints:
            normalized = normalize_name(value)
            if normalized and normalized in self.graph.street_segments and normalized != main:
                names.add(normalized)
        ordered = sorted(names)
        self.neighborhood_cache[key] = ordered
        return ordered

    def intersection(self, main: str, candidate: str) -> dict[str, Any]:
        key = tuple(sorted((main, candidate)))
        if key in self.intersection_cache:
            return self.intersection_cache[key]
        main_geometry = self.street_geometry(main)
        candidate_geometry = self.street_geometry(candidate)
        if main_geometry is None or candidate_geometry is None:
            result = {"geometry": None, "points": [], "distance_m": None}
        else:
            intersection = main_geometry.intersection(candidate_geometry)
            result = {
                "geometry": intersection,
                "points": _points(intersection),
                "distance_m": float(main_geometry.distance(candidate_geometry)),
            }
        self.intersection_cache[key] = result
        return result

    def export_cache(self) -> dict[str, Any]:
        return {
            "neighborhood": self.neighborhood_cache,
            "intersections": {
                "|".join(key): {
                    "point_count": len(value.get("points", [])),
                    "distance_m": value.get("distance_m"),
                }
                for key, value in self.intersection_cache.items()
            },
            "normalized_candidates": sorted(self.street_geometry_cache),
        }


def _critical_comparison(original: str, candidate: str) -> tuple[float, float, bool]:
    original_tokens = set(_critical_tokens(original))
    candidate_tokens = set(_critical_tokens(candidate))
    if not original_tokens or not candidate_tokens:
        return 0.0, 0.0, False
    overlap = len(original_tokens & candidate_tokens)
    f1 = 2 * overlap / (len(original_tokens) + len(candidate_tokens))
    fuzzy_values = []
    for token in original_tokens:
        fuzzy_values.append(max((fuzz.ratio(token, other) for other in candidate_tokens), default=0.0))
    fuzzy = sum(fuzzy_values) / len(fuzzy_values) if fuzzy_values else 0.0
    return float(f1 * 100.0), float(fuzzy), original_tokens == candidate_tokens


def classify_problem_types(original: str, recovered: str) -> list[str]:
    """Classify explainable lexical differences without asserting history."""
    raw = _text(original)
    original_norm, recovered_norm = normalize_name(raw), normalize_name(recovered)
    if not original_norm or not recovered_norm:
        return ["UNKNOWN"]
    result: list[str] = []
    if any(term in original_norm for term in OPERATIONAL_TERMS):
        result.append("OPERATIONAL_REFERENCE")
    original_type, recovered_type = _raw_type(raw), _raw_type(recovered)
    if original_type and recovered_type and original_type != recovered_type:
        result.append("WRONG_STREET_TYPE")
    if re.search(r"\b(?:R|AV|AL|TV|EST|ESTR|ROD|PC|LGO|VL|VD|DR|DRA|PROF|PRES)\.?\b", raw.upper()):
        result.append("ABBREVIATION")
    if original_norm == recovered_norm:
        if unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode("ascii").upper() != raw.upper():
            result.append("ACCENT_ONLY")
        return list(dict.fromkeys(result or ["UNKNOWN"]))

    original_tokens, recovered_tokens = _critical_tokens(raw), _critical_tokens(recovered)
    original_set, recovered_set = set(original_tokens), set(recovered_tokens)
    original_numbers = {token for token in original_tokens if token.isdigit()}
    recovered_numbers = {token for token in recovered_tokens if token.isdigit()}
    if original_numbers != recovered_numbers and (original_numbers or recovered_numbers):
        result.append("NUMBER_VARIATION")
    if original_set == recovered_set and original_tokens != recovered_tokens:
        result.append("TOKEN_ORDER")
    if original_set < recovered_set:
        result.append("MISSING_TOKEN")
    if recovered_set < original_set:
        result.append("EXTRA_TOKEN")
    if original_tokens and recovered_tokens:
        if all(any(other.startswith(token) for other in recovered_tokens) for token in original_tokens) and original_set != recovered_set:
            result.append("TRUNCATED_NAME")
        if len(original_tokens[-1]) >= 3 and any(
            recovered_token.startswith(original_tokens[-1]) or original_tokens[-1].startswith(recovered_token)
            for recovered_token in recovered_tokens
        ):
            result.append("PARTIAL_SURNAME")
    if fuzz.ratio(original_norm, recovered_norm) >= 72:
        result.append("TYPO")
    return list(dict.fromkeys(result or ["UNKNOWN"]))


def _component_match(graph, main: str, point: Point | None, endpoint: Point | None) -> str:
    if graph is None or point is None or endpoint is None:
        return "UNKNOWN"
    street_graph = getattr(graph, "street_graphs", {}).get(main)
    components = getattr(graph, "street_components", {}).get(main, ())
    if street_graph is None or not components:
        return "UNKNOWN"
    nodes = []
    for identifier in getattr(graph, "street_segments", {}).get(main, ()):
        segment = graph.segments[identifier]
        nodes.extend([segment.start, segment.end])
    if not nodes:
        return "UNKNOWN"
    point_node = min(nodes, key=lambda item: math.hypot(item[0] - point.x, item[1] - point.y))
    endpoint_node = min(nodes, key=lambda item: math.hypot(item[0] - endpoint.x, item[1] - endpoint.y))
    point_component = next((component for component in components if point_node in component), None)
    endpoint_component = next((component for component in components if endpoint_node in component), None)
    if point_component is None or endpoint_component is None:
        return "UNKNOWN"
    return "SAME_COMPONENT" if point_component == endpoint_component else "WRONG_COMPONENT"


class BoundaryNameRecoveryEngine:
    """Contextual name evidence engine. It has no mutation path to production."""

    def __init__(self, graph=None, persisted_cache: Mapping[str, Any] | None = None, search_radius_m: float = 350.0):
        self.graph = graph
        self.search_radius_m = float(search_radius_m)
        persisted_cache = persisted_cache or {}
        self.index = BoundaryNameSpatialIndex(graph, persisted_cache.get("spatial"))
        self.lexical_cache: dict[tuple[str, str], tuple[float, float, float, bool]] = {}

    def main_street(self, context: BoundaryNameContext) -> str:
        candidates = [context.main_street, context.via]
        for value in candidates:
            normalized = normalize_name(value)
            if normalized and self.graph is not None and normalized in self.graph.street_segments:
                return normalized
        if self.graph is not None:
            for value in candidates:
                codlog = _text(value)
                resolved = getattr(self.graph, "codlog_to_street", {}).get(codlog)
                if resolved:
                    return resolved
        return next((normalize_name(value) for value in candidates if normalize_name(value)), "")

    def _lexical(self, original: str, candidate: str) -> tuple[float, float, float, bool]:
        key = (normalize_name(original), normalize_name(candidate))
        if key in self.lexical_cache:
            return self.lexical_cache[key]
        lexical = max(
            fuzz.ratio(key[0], key[1]),
            fuzz.token_sort_ratio(key[0], key[1]),
            fuzz.token_set_ratio(key[0], key[1]),
        ) if key[0] and key[1] else 0.0
        critical, fuzzy_critical, exact = _critical_comparison(original, candidate)
        coverage = 0.0
        original_tokens = set(_critical_tokens(original))
        candidate_tokens = set(_critical_tokens(candidate))
        if original_tokens:
            coverage = len(original_tokens & candidate_tokens) / len(original_tokens) * 100.0
        value = (float(lexical), float(critical), float(coverage), bool(exact))
        self.lexical_cache[key] = value
        return value

    def _candidate_names(self, context: BoundaryNameContext, main: str) -> list[str]:
        hints = [item.get("name") or item.get("street_name") for item in context.context_candidates]
        names = self.index.local_streets(main, context.endpoint, context.geometry, self.search_radius_m, hints)
        if self.graph is not None:
            for value in (context.original_name, context.current_candidate):
                normalized = normalize_name(value)
                if normalized in self.graph.street_segments and normalized != main:
                    names.append(normalized)
        return sorted(set(name for name in names if name and name != main))

    def _external_candidate(self, context: BoundaryNameContext, item: Mapping[str, Any]) -> BoundaryNameCandidate:
        street_norm = normalize_name(item.get("street_norm") or item.get("name"))
        street_name = _text(item.get("name") or street_norm)
        lexical, critical, coverage, exact = self._lexical(context.original_name, street_name)
        intersection = _text(item.get("intersection_type")) or "NO_INTERSECTION"
        intersection_score = {"REAL_NODE": 20.0, "GEOMETRIC_INTERSECTION": 16.0, "SMALL_TOPOLOGY_GAP": 8.0}.get(intersection, 0.0)
        component = _text(item.get("component_match")) or "UNKNOWN"
        component_score = 10.0 if component == "SAME_COMPONENT" else 0.0 if component == "WRONG_COMPONENT" else 5.0
        distance = _number(item.get("intersection_distance_m"))
        gps_distance = _number(item.get("distance_to_gps_m"))
        gps_score = 10.0 * math.exp(-gps_distance / 150.0) if gps_distance is not None else 0.0
        score = critical * 0.25 + lexical * 0.20 + intersection_score + gps_score + component_score
        return BoundaryNameCandidate(
            street_name=street_name, street_norm=street_norm, codlog=_text(item.get("codlog")),
            distance_to_gps_m=gps_distance, intersection_count=int(_number(item.get("intersection_count")) or 0),
            geometric_intersection=intersection in {"REAL_NODE", "GEOMETRIC_INTERSECTION"},
            intersection_type=intersection, snap_distance_m=_number(item.get("snap_distance_m")),
            component_match=component, lexical_score=lexical, critical_token_score=critical,
            token_coverage=coverage, length_ratio=None, name_score=round(score, 6),
            critical_exact=exact, problem_types=classify_problem_types(context.original_name, street_name),
        )

    def _graph_candidate(self, context: BoundaryNameContext, main: str, street: str) -> BoundaryNameCandidate:
        display = self.index.street_display(street)
        street_geometry = self.index.street_geometry(street)
        main_geometry = self.index.street_geometry(main)
        lexical, critical, coverage, exact = self._lexical(context.original_name, display or street)
        evidence = self.index.intersection(main, street)
        # A street may intersect the main street elsewhere in the city.  Such
        # an intersection is not evidence for this boundary.  Keep only
        # points close to this endpoint or to the persisted candidate path.
        raw_points = evidence["points"]
        points = [
            point for point in raw_points
            if (context.endpoint is not None and point.distance(context.endpoint) <= self.search_radius_m)
            or (context.geometry is not None and point.distance(context.geometry) <= 35.0)
        ]
        endpoint = context.endpoint
        point = min(points, key=lambda item: item.distance(endpoint)) if points and endpoint is not None else (points[0] if points else None)
        if points:
            node_distance = 8.0
            all_nodes = []
            for value in (main, street):
                for identifier in self.graph.street_segments.get(value, ()):
                    segment = self.graph.segments[identifier]
                    all_nodes.extend([Point(segment.start), Point(segment.end)])
            is_node = point is not None and any(point.distance(node) <= node_distance for node in all_nodes)
            intersection_type = "REAL_NODE" if is_node else "GEOMETRIC_INTERSECTION"
            intersection_distance = float(point.distance(endpoint)) if point is not None and endpoint is not None else None
            snap_distance = 0.0
        else:
            contextual_distance = float(street_geometry.distance(context.geometry)) if street_geometry is not None and context.geometry is not None else None
            intersection_distance = contextual_distance
            snap_distance = contextual_distance
            if snap_distance is not None and snap_distance <= 0.5:
                intersection_type = "SMALL_TOPOLOGY_GAP"
            elif snap_distance is not None and snap_distance <= 20.0:
                intersection_type = "NEAR_INTERSECTION"
            else:
                intersection_type = "NO_INTERSECTION"
        component = _component_match(self.graph, main, point, endpoint)
        gps_distance = float(street_geometry.distance(context.gps_point)) if street_geometry is not None and context.gps_point is not None else None
        gps_score = 10.0 * math.exp(-gps_distance / 150.0) if gps_distance is not None else 0.0
        intersection_score = {
            "REAL_NODE": 20.0, "GEOMETRIC_INTERSECTION": 16.0,
            "SMALL_TOPOLOGY_GAP": 8.0, "NEAR_INTERSECTION": 5.0,
        }.get(intersection_type, 0.0)
        component_score = 10.0 if component == "SAME_COMPONENT" else 0.0 if component == "WRONG_COMPONENT" else 5.0
        length_ratio = None
        if street_geometry is not None and context.geometry is not None and context.geometry.length:
            length_ratio = float(street_geometry.length / context.geometry.length)
        score = critical * 0.25 + lexical * 0.20 + intersection_score + gps_score + component_score
        return BoundaryNameCandidate(
            street_name=display or street, street_norm=street, codlog=self.index.street_codlog(street),
            distance_to_gps_m=gps_distance, intersection_count=len(points),
            geometric_intersection=bool(points), intersection_type=intersection_type,
            snap_distance_m=snap_distance, component_match=component, lexical_score=lexical,
            critical_token_score=critical, token_coverage=coverage, length_ratio=length_ratio,
            name_score=round(score, 6), critical_exact=exact,
            problem_types=classify_problem_types(context.original_name, display or street),
        )

    def _classification(self, selected: BoundaryNameCandidate | None, second: BoundaryNameCandidate | None,
                        context: BoundaryNameContext) -> tuple[str, list[str], str]:
        if selected is None:
            return "NAME_NOT_FOUND", ["NO_CONTEXTUAL_CANDIDATE"], "nenhum candidato contextual disponível"
        margin = selected.name_score - second.name_score if second is not None else 100.0
        warnings = []
        if second is not None and margin < 10.0:
            warnings.append("LOW_NAME_MARGIN")
        if not selected.critical_exact:
            warnings.append("CRITICAL_TOKEN_MISMATCH")
        if selected.component_match == "WRONG_COMPONENT":
            warnings.append("WRONG_COMPONENT")
        if selected.intersection_type in {"NO_INTERSECTION", "NEAR_INTERSECTION"}:
            warnings.append("WEAK_OR_MISSING_INTERSECTION")
        if context.gps_point is None:
            warnings.append("GPS_UNAVAILABLE")
        strong_geo = selected.intersection_type in {"REAL_NODE", "GEOMETRIC_INTERSECTION", "SMALL_TOPOLOGY_GAP"}
        component_ok = selected.component_match in {"SAME_COMPONENT", "UNKNOWN"}
        margin_ok = margin >= 10.0
        if selected.critical_exact and strong_geo and selected.component_match == "SAME_COMPONENT" and selected.name_score >= 80.0 and margin_ok:
            classification = "NAME_RECOVERED_HIGH"
            reason = "nome contextual único, tokens críticos exatos, interseção e componente confirmados"
        elif selected.critical_exact and strong_geo and component_ok and selected.name_score >= 60.0 and margin >= 5.0:
            classification = "NAME_RECOVERED_MEDIUM"
            reason = "candidato contextual provável com evidência geométrica, sem conflito crítico"
        elif second is not None and margin < 10.0:
            classification = "NAME_AMBIGUOUS"
            reason = "mais de um nome contextual permanece competitivo"
        elif selected.component_match == "WRONG_COMPONENT" or not strong_geo:
            classification = "NAME_DATA_CONTRADICTION"
            reason = "similaridade lexical não foi confirmada pelo contexto geométrico"
        elif selected.name_score >= 45.0:
            classification = "NAME_AMBIGUOUS"
            reason = "há uma aproximação lexical, mas faltam tokens críticos ou evidência suficiente"
        else:
            classification = "NAME_NOT_FOUND"
            reason = "nenhum candidato atingiu evidência mínima contextual"
        return classification, warnings, reason

    def recover(self, context: BoundaryNameContext) -> BoundaryNameRecoveryResult:
        main = self.main_street(context)
        names = self._candidate_names(context, main)
        candidates: list[BoundaryNameCandidate] = []
        if self.graph is not None and main:
            candidates.extend(self._graph_candidate(context, main, street) for street in names)
        if not candidates:
            candidates = [self._external_candidate(context, item) for item in context.context_candidates]
        candidates.sort(key=lambda item: (-item.name_score, item.street_norm, item.codlog))
        selected = candidates[0] if candidates else None
        second = candidates[1] if len(candidates) > 1 else None
        classification, warnings, reason = self._classification(selected, second, context)
        margin = selected.name_score - second.name_score if selected is not None and second is not None else (100.0 if selected else None)
        problem_types = selected.problem_types if selected else ["UNKNOWN"]
        return BoundaryNameRecoveryResult(
            record_id=context.record_id, boundary_side=context.boundary_side, via=context.via,
            original_name=context.original_name, normalized_original=normalize_name(context.original_name),
            current_candidate=context.current_candidate, current_status=context.current_status,
            recovered_name=selected.street_name if selected and selected.name_score >= 35.0 else "",
            recovered_codlog=selected.codlog if selected and selected.name_score >= 35.0 else "",
            problem_types=problem_types, lexical_score=selected.lexical_score if selected else None,
            critical_token_score=selected.critical_token_score if selected else None,
            token_coverage=selected.token_coverage if selected else None,
            distance_to_gps_m=selected.distance_to_gps_m if selected else None,
            intersection_type=selected.intersection_type if selected else "NO_INTERSECTION",
            intersection_count=selected.intersection_count if selected else 0,
            snap_distance_m=selected.snap_distance_m if selected else None,
            component_match=selected.component_match if selected else "UNKNOWN",
            name_score=selected.name_score if selected else None, margin_top2=margin,
            classification=classification, reason=reason, warnings=warnings,
            alternatives=[item.to_dict() for item in candidates], source_type=context.source_type,
            expected_name=context.expected_name, corruption_kind=context.corruption_kind,
        )

    def export_cache(self) -> dict[str, Any]:
        return {
            "version": VERSION,
            "spatial": self.index.export_cache(),
            "lexical": {"|".join(key): list(value) for key, value in self.lexical_cache.items()},
        }


def _candidate_payload(value: Any) -> list[dict[str, Any]]:
    payload = _json(value, [])
    return payload if isinstance(payload, list) else []


def _context_from_rows(audit_row: Mapping[str, Any], quality_row: Mapping[str, Any], side: str) -> BoundaryNameContext:
    side = side.upper()
    original = _text(audit_row.get("de_original" if side == "DE" else "ate_original"))
    current = _text(audit_row.get("de_current" if side == "DE" else "ate_current"))
    status = _text(audit_row.get("de_status" if side == "DE" else "ate_status"))
    hints = _candidate_payload(audit_row.get("de_candidates_json" if side == "DE" else "ate_candidates_json"))
    geometry = _parse_wkt(audit_row.get("diagnostic_geometry_wkt")) or _parse_wkt(audit_row.get("candidate_geometry_wkt")) or _parse_wkt(quality_row.get("geometry_wkt"))
    return BoundaryNameContext(
        record_id=_text(audit_row.get("id") or audit_row.get("record_id")), boundary_side=side,
        via=_text(quality_row.get("via") or audit_row.get("via_resolvida") or audit_row.get("via")),
        main_street=_text(quality_row.get("main_street") or quality_row.get("via_resolvida") or audit_row.get("via")),
        original_name=original, current_candidate=current, geometry=geometry,
        latitude=_number(quality_row.get("latitude")), longitude=_number(quality_row.get("longitude")),
        current_status=status, root_cause=_text(audit_row.get("root_cause")),
        secondary_causes=_text(audit_row.get("secondary_causes")), context_candidates=hints,
    )


def load_name_problem_rows(sample: int | None = None, only_side: str | None = None,
                            only_problem_type: str | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    audit = pd.read_csv(AUDIT_INPUT, dtype=str, encoding="utf-8-sig", keep_default_na=False)
    quality = pd.read_csv(QUALITY_INPUT, dtype=str, encoding="utf-8-sig", keep_default_na=False).set_index("id") if QUALITY_INPUT.exists() else pd.DataFrame()
    marker_text = audit.root_cause.fillna("") + "|" + audit.secondary_causes.fillna("")
    # Keep the population identical to boundary_contradiction_report.json:
    # its official 661 count is secondary_causes containing BOUNDARY_NAME_.
    mask = audit.secondary_causes.fillna("").str.contains("BOUNDARY_NAME_", case=False, regex=False) | audit.root_cause.fillna("").str.contains("NAME_PROBLEM", case=False, regex=False)
    audit = audit[mask].copy()
    if only_problem_type:
        aliases = {
            "ABBREVIATION": "BOUNDARY_NAME_", "TYPO": "NAME_WRONG", "MISSING_TOKEN": "NAME_INCOMPLETE",
            "TRUNCATED_NAME": "NAME_INCOMPLETE", "WRONG_STREET_TYPE": "NAME_WRONG",
        }
        token = aliases.get(only_problem_type.upper(), only_problem_type.upper())
        audit = audit[marker_text.loc[audit.index].str.contains(token, case=False, regex=False)].copy()
    audit = audit.sort_values("id", key=lambda series: series.astype(str))
    if sample:
        audit = audit.head(int(sample))
    return audit, quality


def load_official_controls(limit: int | None = None) -> list[BoundaryNameContext]:
    if not OFFICIAL_INPUT.exists():
        return []
    frame = pd.read_csv(OFFICIAL_INPUT, dtype=str, encoding="utf-8-sig", keep_default_na=False)
    result = []
    for _, row in frame.iterrows():
        geometry = _parse_path(row.get("path"))
        if geometry is None:
            continue
        for side, field_name in (("DE", "de"), ("ATE", "ate")):
            original = _text(row.get(field_name))
            if not original:
                continue
            result.append(BoundaryNameContext(
                record_id=f"OFFICIAL::{_text(row.get('id'))}::{side}", boundary_side=side,
                via=_text(row.get("via")), main_street=_text(row.get("via")), original_name=original,
                current_candidate=original, geometry=geometry,
                latitude=_number(row.get("latitude")), longitude=_number(row.get("longitude")),
                source_type="OFFICIAL_POSITIVE", expected_name=original,
            ))
    result.sort(key=lambda item: hashlib.sha1(item.record_id.encode()).hexdigest())
    return result[:limit] if limit else result


CORRUPTION_KINDS = (
    "ABBREVIATION", "TYPO", "TRUNCATED_NAME", "MISSING_TOKEN", "EXTRA_TOKEN",
    "WRONG_STREET_TYPE", "ACCENT_ONLY", "TOKEN_ORDER", "NUMBER_VARIATION",
)


def _replace_leading_type(value: str, replacement: str) -> str:
    raw = _text(value)
    if _raw_type(raw):
        pieces = raw.split(None, 1)
        return replacement + (" " + pieces[1] if len(pieces) > 1 else "")
    return replacement + " " + raw


def degrade_name(value: str, kind: str) -> str:
    raw = _text(value)
    tokens = raw.split()
    if not raw:
        return raw
    kind = kind.upper()
    if kind == "ACCENT_ONLY":
        return unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode("ascii")
    if kind == "ABBREVIATION":
        return _replace_leading_type(raw, {"RUA": "R.", "AVENIDA": "AV.", "ALAMEDA": "AL.", "TRAVESSA": "TV.", "ESTRADA": "EST."}.get(_raw_type(raw), "R."))
    if kind == "WRONG_STREET_TYPE":
        return _replace_leading_type(raw, "AV.")
    if kind == "MISSING_TOKEN" and len(tokens) > 2:
        return " ".join(tokens[:-1])
    if kind == "EXTRA_TOKEN":
        return raw + " CENTRAL"
    if kind == "TOKEN_ORDER" and len(tokens) > 2:
        return " ".join([tokens[0], *reversed(tokens[1:])])
    if kind == "NUMBER_VARIATION":
        return re.sub(r"\d+", lambda match: str(int(match.group(0)) + 1), raw, count=1) if re.search(r"\d", raw) else raw + " 2"
    if kind in {"TYPO", "TRUNCATED_NAME"}:
        index = max(range(len(tokens)), key=lambda pos: len(normalize_name(tokens[pos])))
        token = tokens[index]
        if len(token) > 4:
            tokens[index] = token[:-1] if kind == "TRUNCATED_NAME" else token[:2] + token[3:]
        return " ".join(tokens)
    return raw


def make_positive_controls(base: Iterable[BoundaryNameContext], max_per_kind: int | None = None) -> list[BoundaryNameContext]:
    controls = []
    for kind in CORRUPTION_KINDS:
        selected = list(base)
        if max_per_kind:
            selected = selected[:max_per_kind]
        for context in selected:
            degraded = degrade_name(context.expected_name or context.original_name, kind)
            controls.append(replace(
                context, record_id=f"{context.record_id}::{kind}", original_name=degraded,
                current_candidate=context.expected_name or context.original_name,
                source_type="SYNTHETIC_POSITIVE", corruption_kind=kind,
            ))
    return controls


def _swap_critical(value: str) -> str:
    tokens = value.split()
    if tokens:
        index = len(tokens) - 1
        token = tokens[index]
        tokens[index] = "MARIA" if token.upper().rstrip(".") != "MARIA" else "MARINA"
    return " ".join(tokens)


def make_negative_controls(base: Iterable[BoundaryNameContext], max_per_kind: int = 50) -> list[BoundaryNameContext]:
    controls = list(base)[:max_per_kind]
    result = []
    for context in controls:
        variants = {
            "CRITICAL_TOKEN_SWAP": _swap_critical(context.expected_name or context.original_name),
            "EXTRA_CRITICAL_TOKEN": (context.expected_name or context.original_name) + " SANTA MARIA",
            "PARALLEL_OR_WRONG_STREET": "SANTA MARIA",
            "NO_INTERSECTION": context.expected_name or context.original_name,
            "WRONG_COMPONENT": context.expected_name or context.original_name,
        }
        for label, name in variants.items():
            geometry = context.geometry
            if label in {"NO_INTERSECTION", "WRONG_COMPONENT"} and geometry is not None:
                geometry = affinity.translate(geometry, xoff=2000.0, yoff=1500.0)
            result.append(replace(
                context, record_id=f"{context.record_id}::NEG::{label}", original_name=name,
                geometry=geometry, source_type="SYNTHETIC_NEGATIVE", expected_name="", corruption_kind=label,
            ))
    return result


def _hash_file(path: Path) -> str | None:
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def protected_hashes() -> dict[str, str | None]:
    names = [
        ALIASES_INPUT, PROCESSED / "recape_clean.csv", PROCESSED / "notificacoes.csv",
        PROCESSED / "cruzamento.csv", PROCESSED / "recapes_sem_cobertura.csv",
        PROCESSED / "geosampa_coverage_report.json", PROCESSED / "pipeline_run.json",
        ROOT / "src" / "road_graph.py", ROOT / "src" / "street_resolver.py",
        ROOT / "src" / "geometry_validator.py", ROOT / "src" / "boundary_contradiction_audit.py",
    ]
    return {str(path.relative_to(ROOT)): _hash_file(path) for path in names}


def _control_metrics(results: list[BoundaryNameRecoveryResult], kind: str) -> dict[str, Any]:
    total = len(results)
    accepted = sum(item.classification in RECOVERED_CLASSES for item in results)
    rejected = total - accepted
    if kind == "positive":
        top1 = sum(normalize_name(item.recovered_name) == normalize_name(item.expected_name) for item in results if item.expected_name)
        top2 = sum(
            normalize_name(item.expected_name) in {normalize_name(candidate.get("street_name")) for candidate in item.alternatives[:2]}
            for item in results if item.expected_name
        )
    else:
        top1 = top2 = 0
    rate = (accepted / total) if total else None
    low, high = _wilson(accepted, total)
    return {
        "control_kind": kind, "total": total, "accepted": accepted, "rejected": rejected,
        "recovery_rate": rate if kind == "positive" else None,
        "rejection_rate": (rejected / total) if total else None,
        "false_acceptance_rate": rate if kind == "negative" else None,
        "top1_accuracy": (top1 / total) if kind == "positive" and total else None,
        "top2_accuracy": (top2 / total) if kind == "positive" and total else None,
        "accepted_wilson_95": {"low": low, "high": high},
        "minimum_cases": 30,
        "meets_minimum_cases": total >= 30,
    }


def evaluate_controls(engine: BoundaryNameRecoveryEngine, positives: list[BoundaryNameContext], negatives: list[BoundaryNameContext]) -> tuple[dict[str, Any], dict[str, Any], list[BoundaryNameRecoveryResult], list[BoundaryNameRecoveryResult]]:
    positive_results = [engine.recover(item) for item in positives]
    negative_results = [engine.recover(item) for item in negatives]
    positive = _control_metrics(positive_results, "positive")
    positive["by_corruption"] = {
        kind: _control_metrics([item for item in positive_results if item.corruption_kind == kind], "positive")
        for kind in CORRUPTION_KINDS
    }
    negative = _control_metrics(negative_results, "negative")
    negative["by_negative_type"] = {
        kind: _control_metrics([item for item in negative_results if item.corruption_kind == kind], "negative")
        for kind in sorted({item.corruption_kind for item in negatives})
    }
    return positive, negative, positive_results, negative_results


def mine_alias_candidates(results: Iterable[BoundaryNameRecoveryResult]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[BoundaryNameRecoveryResult]] = {}
    for result in results:
        if not result.normalized_original or not result.recovered_name:
            continue
        if result.classification not in RECOVERED_CLASSES and (result.name_score or 0) < 60:
            continue
        key = (result.normalized_original, result.recovered_codlog or normalize_name(result.recovered_name))
        groups.setdefault(key, []).append(result)
    rows = []
    for (original_norm, codlog), group in sorted(groups.items()):
        names = sorted({item.recovered_name for item in group})
        scores = [item.name_score for item in group if item.name_score is not None]
        distances = [item.distance_to_gps_m for item in group if item.distance_to_gps_m is not None]
        intersection_rate = sum(item.intersection_type in {"REAL_NODE", "GEOMETRIC_INTERSECTION"} for item in group) / len(group)
        component_rate = sum(item.component_match == "SAME_COMPONENT" for item in group) / len(group)
        critical_rate = sum((item.critical_token_score or 0) >= 99.999 for item in group) / len(group)
        canonical_share = len(group) / sum(len(value) for key, value in groups.items() if key[0] == original_norm)
        if len(group) >= 3 and canonical_share >= 0.95 and intersection_rate >= 0.9 and component_rate >= 0.9 and critical_rate >= 0.9 and min(scores or [0]) >= 80:
            scope, confidence = "GLOBAL_ALIAS", "HIGH"
        elif len(group) >= 2 and intersection_rate >= 0.7 and component_rate >= 0.7:
            scope, confidence = "CONTEXTUAL_ALIAS", "MEDIUM"
        else:
            scope, confidence = "DO_NOT_ALIAS", "LOW"
        rows.append({
            "original_name": original_norm, "normalized_original": original_norm,
            "candidate_name": names[0], "candidate_codlog": codlog,
            "occurrences": len(group), "contexts": "|".join(item.record_id for item in group[:20]),
            "avg_score": sum(scores) / len(scores) if scores else None,
            "min_score": min(scores) if scores else None, "max_score": max(scores) if scores else None,
            "avg_distance_m": sum(distances) / len(distances) if distances else None,
            "intersection_confirmation_rate": intersection_rate,
            "component_confirmation_rate": component_rate,
            "critical_token_match_rate": critical_rate, "confidence": confidence,
            "recommended_scope": scope,
        })
    return sorted(rows, key=lambda row: (-float(row.get("avg_score") or 0), -int(row["occurrences"]), row["original_name"], row["candidate_name"]))


def _distribution(results: Iterable[BoundaryNameRecoveryResult], field: str) -> dict[str, int]:
    values = {}
    for result in results:
        value = getattr(result, field)
        if isinstance(value, list):
            for item in value:
                values[item] = values.get(item, 0) + 1
        else:
            key = _text(value) or "UNKNOWN"
            values[key] = values.get(key, 0) + 1
    return dict(sorted(values.items(), key=lambda item: (-item[1], item[0])))


def _load_persisted_cache() -> dict[str, Any]:
    if not LOCAL_CACHE.exists():
        return {}
    try:
        payload = json.loads(LOCAL_CACHE.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) and payload.get("version") == VERSION else {}
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def _project_before_after(audit: pd.DataFrame, results: list[BoundaryNameRecoveryResult]) -> dict[str, Any]:
    before = {str(row["id"]): _text(row.get("recommendation")) for _, row in audit.iterrows()}
    by_case: dict[str, list[BoundaryNameRecoveryResult]] = {}
    for result in results:
        by_case.setdefault(result.record_id, []).append(result)
    projected = {}
    for record_id, items in by_case.items():
        current = before.get(record_id, "UNKNOWN")
        classes = {item.classification for item in items}
        if "NAME_RECOVERED_HIGH" in classes and classes <= RECOVERED_CLASSES:
            candidate = "POTENTIAL_BOUNDARIES_VALIDATED_HIGH"
        elif classes & RECOVERED_CLASSES:
            candidate = "POTENTIAL_BOUNDARIES_VALIDATED_MEDIUM"
        else:
            candidate = current
        projected[record_id] = {"before": current, "potential_after": candidate}
    return {
        "before_by_recommendation": {key: sum(value == key for value in before.values()) for key in sorted(set(before.values()))},
        "potential_high": sum(value["potential_after"] == "POTENTIAL_BOUNDARIES_VALIDATED_HIGH" for value in projected.values()),
        "potential_medium": sum(value["potential_after"] == "POTENTIAL_BOUNDARIES_VALIDATED_MEDIUM" for value in projected.values()),
        "case_projection": projected,
        "official_promotions_applied": 0,
    }


def run_shadow(args: argparse.Namespace) -> dict[str, Any]:
    if not args.shadow:
        raise ValueError("Esta auditoria só pode ser executada com --shadow")
    started = time.perf_counter()
    PROCESSED.mkdir(parents=True, exist_ok=True)
    if args.reset_cache:
        for path in (OUTPUT_CSV, OUTPUT_REPORT, ALIAS_OUTPUT, LOCAL_CACHE):
            if path.exists():
                path.unlink()
    before_hashes = protected_hashes()
    audit, quality = load_name_problem_rows(args.sample, args.only_side, args.only_problem_type)
    graph = load_read_only_graph()
    cache = _load_persisted_cache() if args.resume else {}
    engine = BoundaryNameRecoveryEngine(graph, cache.get("engine_cache"))
    existing = {}
    if args.resume and OUTPUT_CSV.exists():
        frame = pd.read_csv(OUTPUT_CSV, dtype=str, encoding="utf-8-sig", keep_default_na=False)
        existing = {(str(row["id"]), str(row["boundary_side"])): row.to_dict() for _, row in frame.iterrows()}
    results: list[BoundaryNameRecoveryResult] = []
    for _, audit_row in audit.iterrows():
        record_id = _text(audit_row.get("id"))
        quality_row = quality.loc[record_id].to_dict() if not quality.empty and record_id in quality.index else {}
        for side in ("DE", "ATE"):
            context = _context_from_rows(audit_row, quality_row, side)
            if not context.original_name or (args.only_side and side != args.only_side.upper()):
                continue
            key = (record_id, side)
            if key in existing:
                row = existing[key]
                results.append(BoundaryNameRecoveryResult(
                    record_id=record_id, boundary_side=side, via=_text(row.get("via")), original_name=_text(row.get("original_name")),
                    normalized_original=_text(row.get("normalized_original")), current_candidate=_text(row.get("current_candidate")),
                    current_status=_text(row.get("current_status")), recovered_name=_text(row.get("recovered_name")),
                    recovered_codlog=_text(row.get("recovered_codlog")), problem_types=_text(row.get("problem_types")).split("|") if _text(row.get("problem_types")) else ["UNKNOWN"],
                    lexical_score=_number(row.get("lexical_score")), critical_token_score=_number(row.get("critical_token_score")),
                    token_coverage=_number(row.get("token_coverage")), distance_to_gps_m=_number(row.get("distance_to_gps_m")),
                    intersection_type=_text(row.get("intersection_type")), intersection_count=int(_number(row.get("intersection_count")) or 0),
                    snap_distance_m=_number(row.get("snap_distance_m")), component_match=_text(row.get("component_match")),
                    name_score=_number(row.get("name_score")), margin_top2=_number(row.get("margin_top2")),
                    classification=_text(row.get("classification")), reason=_text(row.get("reason")),
                    warnings=_text(row.get("warnings")).split("|") if _text(row.get("warnings")) else [],
                    alternatives=_json(row.get("alternatives_json"), []), source_type=_text(row.get("source_type")) or "ESTIMATED",
                ))
            else:
                results.append(engine.recover(context))

    base_controls = load_official_controls(limit=max(args.sample or 0, 120) if args.sample else None)
    control_limit = args.sample if args.sample else 80
    positives = make_positive_controls(base_controls, max_per_kind=control_limit)
    negatives = make_negative_controls(base_controls, max_per_kind=min(50, control_limit))
    positive_metrics, negative_metrics, _, _ = evaluate_controls(engine, positives, negatives)
    rows = [result.to_row() for result in results]
    output = pd.DataFrame(rows)
    if not output.empty:
        output = output.sort_values(["id", "boundary_side"], key=lambda series: series.astype(str))
        output.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")
    aliases = mine_alias_candidates(results)
    pd.DataFrame(aliases).to_csv(ALIAS_OUTPUT, index=False, encoding="utf-8-sig")
    after_hashes = protected_hashes()
    classifications = _distribution(results, "classification")
    target_ids = {item.record_id for item in results}
    audit_scope = audit[audit["id"].isin(target_ids)] if not audit.empty else audit
    projection = _project_before_after(audit_scope, results)
    global_aliases = [row for row in aliases if row["recommended_scope"] == "GLOBAL_ALIAS"]
    contextual_aliases = [row for row in aliases if row["recommended_scope"] == "CONTEXTUAL_ALIAS"]
    report = {
        "version": VERSION, "shadow_only": True, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "total_name_problems": len(target_ids), "name_problem_boundary_rows": len(results),
        "recovered_high": classifications.get("NAME_RECOVERED_HIGH", 0),
        "recovered_medium": classifications.get("NAME_RECOVERED_MEDIUM", 0),
        "ambiguous": classifications.get("NAME_AMBIGUOUS", 0),
        "not_found": classifications.get("NAME_NOT_FOUND", 0),
        "data_contradiction": classifications.get("NAME_DATA_CONTRADICTION", 0),
        "by_classification": classifications, "by_problem_type": _distribution(results, "problem_types"),
        "by_boundary_side": _distribution(results, "boundary_side"),
        "population": {"source_rows": len(audit), "source_unique_ids": int(audit["id"].nunique()) if not audit.empty else 0, "expected_marker": list(NAME_MARKERS)},
        "positive_control_metrics": positive_metrics, "negative_control_metrics": negative_metrics,
        "alias_candidates": aliases, "global_alias_candidates": global_aliases,
        "contextual_alias_candidates": contextual_aliases,
        "projected_boundary_high": classifications.get("NAME_RECOVERED_HIGH", 0),
        "projected_boundary_medium": classifications.get("NAME_RECOVERED_MEDIUM", 0),
        "projection": projection,
        "official_promotions_applied": False, "official_outputs_written": False,
        "aliases_official_written": False,
        "protected_hashes_before": before_hashes, "protected_hashes_after": after_hashes,
        "official_outputs_unchanged": before_hashes == after_hashes,
        "street_aliases_unchanged": before_hashes.get(str(ALIASES_INPUT.relative_to(ROOT))) == after_hashes.get(str(ALIASES_INPUT.relative_to(ROOT))),
        "road_graph_unchanged": before_hashes.get("src/road_graph.py") == after_hashes.get("src/road_graph.py"),
        "street_resolver_unchanged": before_hashes.get("src/street_resolver.py") == after_hashes.get("src/street_resolver.py"),
        "boundary_audit_unchanged": before_hashes.get("src/boundary_contradiction_audit.py") == after_hashes.get("src/boundary_contradiction_audit.py"),
        "geometry_validator_unchanged": before_hashes.get("src/geometry_validator.py") == after_hashes.get("src/geometry_validator.py"),
        "graph_loaded_read_only": graph is not None,
        "cache": {"resume": bool(args.resume), "cache_path": str(LOCAL_CACHE.relative_to(ROOT)), "local_neighborhoods": len(engine.index.neighborhood_cache), "intersections": len(engine.index.intersection_cache), "lexical_pairs": len(engine.lexical_cache)},
        "runtime_seconds": round(time.perf_counter() - started, 3),
        "memory_rss_mb": _memory_rss_mb(),
    }
    OUTPUT_REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    LOCAL_CACHE.write_text(json.dumps({"version": VERSION, "engine_cache": engine.export_cache()}, ensure_ascii=False), encoding="utf-8")
    return report


def _memory_rss_mb() -> float | None:
    try:
        import psutil
        return round(psutil.Process().memory_info().rss / 1024 / 1024, 3)
    except Exception:
        pass
    if sys.platform.startswith("win"):
        try:
            import ctypes

            class _Counters(ctypes.Structure):
                _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                            ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

            counters = _Counters()
            counters.cb = ctypes.sizeof(_Counters)
            kernel32 = ctypes.WinDLL("kernel32.dll")
            psapi = ctypes.WinDLL("psapi.dll")
            handle = kernel32.GetCurrentProcess()
            get_memory = psapi.GetProcessMemoryInfo
            get_memory.argtypes = [ctypes.c_void_p, ctypes.POINTER(_Counters), ctypes.c_ulong]
            get_memory.restype = ctypes.c_int
            ok = get_memory(handle, ctypes.byref(counters), counters.cb)
            if ok:
                return round(counters.WorkingSetSize / 1024 / 1024, 3)
        except Exception:
            return None
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Boundary Name Recovery Engine shadow-only")
    parser.add_argument("--shadow", action="store_true", help="obrigatório; não há modo de escrita oficial")
    parser.add_argument("--sample", type=int, default=None)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--reset-cache", action="store_true")
    parser.add_argument("--only-side", choices=["DE", "ATE"], default=None)
    parser.add_argument("--only-problem-type", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = run_shadow(args)
    except (OSError, ValueError, KeyError, pd.errors.EmptyDataError) as exc:
        print(f"boundary_name_recovery: erro: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({
        "total_name_problems": report["total_name_problems"],
        "recovered_high": report["recovered_high"],
        "recovered_medium": report["recovered_medium"],
        "ambiguous": report["ambiguous"],
        "not_found": report["not_found"],
        "data_contradiction": report["data_contradiction"],
        "runtime_seconds": report["runtime_seconds"],
        "official_outputs_unchanged": report["official_outputs_unchanged"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
