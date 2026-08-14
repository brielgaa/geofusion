"""Auditoria diagnóstica e recuperação conservadora de geometrias de recape.

O módulo não grava saídas oficiais nem persiste o RoadGraph. Todas as estratégias
produzem candidatos auditáveis; a decisão de aplicar qualquer geometria permanece
separada e explícita.
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
import os
import pickle
import sys
import time
import tracemalloc
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field, replace
from numbers import Integral
from pathlib import Path
from typing import Any, Iterable, Mapping

import pandas as pd
from pyproj import Transformer
from rapidfuzz import fuzz, process
from shapely.geometry import LineString, Point, mapping
from shapely.ops import linemerge, nearest_points, substring
from shapely.strtree import STRtree

try:
    from street_resolution_overrides import HumanReviewOverrides, load_human_review_overrides
    from transform import (
        CACHE_DIR, DEFAULT_HUMAN_REVIEW_PATH, GEOSAMPA_SEGMENTOS, PROCESSED_DIR,
        RoadGraph, load_recape, normalizar_rua,
    )
except ImportError:  # pragma: no cover - import path for ``python -m src...``
    from .street_resolution_overrides import HumanReviewOverrides, load_human_review_overrides
    from .transform import (
        CACHE_DIR, DEFAULT_HUMAN_REVIEW_PATH, GEOSAMPA_SEGMENTOS, PROCESSED_DIR,
        RoadGraph, load_recape, normalizar_rua,
    )


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CLEAN_PATH = ROOT / "data" / "processed" / "recape_clean.csv"
DEFAULT_AUDIT_PATH = ROOT / "data" / "processed" / "route_geometry_audit.csv"
DEFAULT_REPORT_PATH = ROOT / "data" / "processed" / "route_geometry_report.json"
DEFAULT_REVIEW_PATH = ROOT / "data" / "processed" / "route_geometry_review.csv"
DEFAULT_CHECKPOINT_PATH = ROOT / "data" / "processed" / "route_geometry_audit_checkpoint.json"
DEFAULT_QUALITY_AUDIT_PATH = ROOT / "data" / "processed" / "route_geometry_quality_shadow.csv"
DEFAULT_QUALITY_REPORT_PATH = ROOT / "data" / "processed" / "route_geometry_quality_report.json"
DEFAULT_QUALITY_REVIEW_PATH = ROOT / "data" / "processed" / "route_geometry_quality_review.csv"
DEFAULT_QUALITY_CHECKPOINT_PATH = ROOT / "data" / "processed" / "route_geometry_quality_checkpoint.json"
DEFAULT_SAME_TRANSVERSAL_AUDIT_PATH = ROOT / "data" / "processed" / "route_geometry_same_transversal_audit.csv"
DEFAULT_SAME_TRANSVERSAL_REPORT_PATH = ROOT / "data" / "processed" / "route_geometry_same_transversal_report.json"
DEFAULT_SAME_TRANSVERSAL_CHECKPOINT_PATH = ROOT / "data" / "processed" / "route_geometry_same_transversal_checkpoint.json"
VERSION = "route-geometry-audit-v1"
QUALITY_VERSION = "route-geometry-quality-shadow-v2.4"
SAME_TRANSVERSAL_STRATEGY = "SAME_TRANSVERSAL_TWO_INTERSECTIONS"
INTERSECTION_DEDUP_TOLERANCE_M = 1.0
SAME_TRANSVERSAL_MAX_GAP_M = 5.0
SAME_TRANSVERSAL_MIN_LENGTH_M = 2.0
SAME_TRANSVERSAL_GPS_HIGH_M = 35.0
SAME_TRANSVERSAL_GPS_MEDIUM_M = 75.0
SAME_TRANSVERSAL_EXT_HIGH_PCT = 20.0
SAME_TRANSVERSAL_EXT_MEDIUM_PCT = 40.0
WGS84_TO_UTM = Transformer.from_crs("EPSG:4326", "EPSG:31983", always_xy=True).transform


def _text(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    text = str(value).strip()
    return "" if text.casefold() in {"", "nan", "none", "null", "<na>"} else text


def _float(value: Any) -> float | None:
    try:
        result = float(value)
        return result if pd.notna(result) else None
    except (TypeError, ValueError):
        return None


def _bool(value: Any) -> bool:
    return _text(value).casefold() in {"true", "1", "sim", "yes"}


def _valid_path(value: Any) -> bool:
    text = _text(value)
    return bool(text and text.casefold() not in {"none", "nan", "[]"})


def _atomic_write_csv(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    frame.to_csv(temporary, index=False, encoding="utf-8-sig")
    os.replace(temporary, path)


def _json_default(value: Any) -> Any:
    if isinstance(value, (pd.Timestamp,)):
        return value.isoformat()
    if hasattr(value, "item"):
        return value.item()
    return str(value)


@dataclass
class GeometryRecoveryCandidate:
    strategy: str
    geometry_wkt: str | None
    path_nodes: list[Any] = field(default_factory=list)
    length_m: float | None = None
    deviation_pct: float | None = None
    start_distance_m: float | None = None
    end_distance_m: float | None = None
    de_status: str = "UNRESOLVED"
    ate_status: str = "UNRESOLVED"
    topology_status: str = "UNRESOLVED"
    component_status: str = "UNRESOLVED"
    snap_used: bool = False
    snap_distance_de_m: float | None = None
    snap_distance_ate_m: float | None = None
    confidence: str = "UNRESOLVED"
    score: float = 0.0
    evidence: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    geometry_geojson: str | None = None
    segment_count: int = 0
    component_count: int = 1
    max_gap_m: float | None = None
    loop_detected: bool = False
    main_street: str | None = None
    main_match_score: float | None = None
    main_reference_distance_m: float | None = None
    component_index: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GeometryRecoveryResult:
    recape_id: str
    current_status: str
    recovered: bool
    selected_strategy: str | None
    confidence: str
    candidate_count: int
    selected_candidate: GeometryRecoveryCandidate | None
    alternatives: list[GeometryRecoveryCandidate]
    requires_review: bool
    reason: str
    baseline_alternatives_json: str | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)


class GeometryRecoveryEngine:
    """Gera candidatos sem alterar índices, caches ou geometrias oficiais."""

    def __init__(self, graph: Any, normalizer=normalizar_rua, overrides: HumanReviewOverrides | None = None):
        self.graph = graph
        self.normalizer = normalizer
        self.overrides = overrides
        self.context_cache: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.component_cache: dict[tuple[str, str], Any] = {}
        self.projection_cache: dict[tuple[str, float, float], float] = {}
        self.street_spatial_cache: dict[str, tuple[list[str], list[Any], Any, dict[int, int]]] = {}
        self.geometric_pair_cache: dict[tuple[str, str], list[tuple[float, Point]]] = {}
        self.cache_hits = {"context": 0, "component": 0, "projection": 0}

    def _reference(self, row: Mapping[str, Any]) -> Point | None:
        lat, lon = _float(row.get("latitude")), _float(row.get("longitude"))
        if lat is None or lon is None:
            return None
        try:
            x, y = WGS84_TO_UTM(lon, lat)
            return Point(x, y)
        except (TypeError, ValueError):
            return None

    def _main_street(self, row: Mapping[str, Any], override: Any) -> tuple[str, str]:
        if override is not None and override.valid and override.applicable and override.resolved_street:
            return override.resolved_street, override.resolved_codlog or ""
        via = next((_text(row.get(name)) for name in ("logradouro_geosampa", "via", "rua_raw") if _text(row.get(name))), "")
        codlog = _text(row.get("codlog")) or _text(row.get("cd_codlog"))
        return self.normalizer(via), codlog

    def _component_geometry(self, street: str, reference: Point | None):
        key = (street, str(reference.wkt if reference else ""))
        if key in self.component_cache:
            self.cache_hits["component"] += 1
            return self.component_cache[key]
        identifiers = list((getattr(self.graph, "street_segments", {}) or {}).get(street, ()))
        if not identifiers:
            self.component_cache[key] = None
            return None
        segments = [self.graph.segments[item] for item in identifiers if item in self.graph.segments]
        if not segments:
            self.component_cache[key] = None
            return None
        nearest = min(segments, key=lambda segment: segment.geometry.distance(reference)) if reference else segments[0]
        try:
            geometry, _ = self.graph._whole_component_geometry(street, nearest.start)
        except (AttributeError, KeyError, TypeError):
            geometry = linemerge([segment.geometry for segment in segments])
        if geometry is not None and geometry.geom_type != "LineString":
            geometry = max(getattr(geometry, "geoms", ()), key=lambda part: part.length, default=None)
        self.component_cache[key] = geometry
        return geometry

    def _projection(self, geometry, reference: Point | None) -> float:
        if geometry is None or reference is None:
            return 0.0
        cache_key = (geometry.wkb_hex, round(reference.x, 2), round(reference.y, 2))
        if cache_key in self.projection_cache:
            self.cache_hits["projection"] += 1
            return self.projection_cache[cache_key]
        distance = float(geometry.project(reference))
        self.projection_cache[cache_key] = distance
        return distance

    @staticmethod
    def _slice(geometry, start: float, end: float):
        if geometry is None or geometry.is_empty:
            return None
        start = max(0.0, min(float(start), geometry.length))
        end = max(0.0, min(float(end), geometry.length))
        if abs(end - start) < 0.01:
            return None
        segment = substring(geometry, start, end)
        return segment if segment is not None and not segment.is_empty else None

    def _street_intersections(self, main: str, transversal: str, reference: Point | None) -> tuple[Any, str]:
        if not transversal:
            return None, "CAMPO_VAZIO"
        normalized = self.normalizer(transversal)
        try:
            resolved, score, method = self.graph.resolve_street(normalized)
        except (AttributeError, TypeError, ValueError):
            return None, "NAO_RESOLVIDA"
        if not resolved:
            return None, "NAO_RESOLVIDA"
        try:
            intersections = self.graph.intersections(main, resolved)
        except (AttributeError, KeyError):
            intersections = []
        if not intersections:
            return None, method or "SEM_INTERSECAO"
        item = min(intersections, key=lambda value: value[0].distance(reference)) if reference else intersections[0]
        return item[0], method or "EXATO"

    def _component_count(self, street: str) -> int:
        return len(getattr(self.graph, "street_components", {}).get(street, ())) or 1

    def _gap_to_street(self, main: str, transversal: str, reference: Point | None) -> float | None:
        _, gap = self._geometric_intersection(main, transversal, reference, max_gap=100.0)
        return gap

    def _geometric_intersection(self, main: str, transversal: str, reference: Point | None, max_gap: float = 5.0) -> tuple[Point | None, float | None]:
        """Find a source-geometry crossing or a small virtual gap.

        ``RoadGraph.intersections`` intentionally exposes only intersections
        mapped to graph nodes. The audit also records crossings present in the
        GeoSampa geometries but absent from the topology. The default five metre
        limit is used for recovery; callers may request a wider diagnostic
        search to report a rejected gap.
        """
        if not transversal:
            return None, None
        normalized = self.normalizer(transversal)
        try:
            resolved, _, _ = self.graph.resolve_street(normalized)
        except (AttributeError, TypeError, ValueError):
            return None, None
        if not resolved:
            return None, None
        main_ids = getattr(self.graph, "street_segments", {}).get(main, ())
        other_ids = getattr(self.graph, "street_segments", {}).get(resolved, ())
        pairs = []
        pair_key = (main, resolved)
        pairs = self.geometric_pair_cache.get(pair_key)
        if pairs is None:
            pairs = []
            other_geometries = [self.graph.segments[item].geometry for item in other_ids if item in self.graph.segments]
            geometry_ids = [item for item in other_ids if item in self.graph.segments]
            tree = STRtree(other_geometries) if other_geometries else None
            geometry_index = {id(geometry): index for index, geometry in enumerate(other_geometries)}
            for main_id in main_ids:
                main_segment = self.graph.segments.get(main_id)
                if main_segment is None or tree is None:
                    continue
                try:
                    hits = tree.query(main_segment.geometry.buffer(100.0))
                except (AttributeError, TypeError, ValueError):
                    hits = ()
                nearby = []
                for hit in hits:
                    if isinstance(hit, Integral):
                        index = int(hit)
                    else:
                        index = geometry_index.get(id(hit), -1)
                    if 0 <= index < len(geometry_ids):
                        nearby.append(other_geometries[index])
                for other_geometry in nearby:
                    intersection = main_segment.geometry.intersection(other_geometry)
                    points = []
                    if not intersection.is_empty:
                        if intersection.geom_type == "Point":
                            points = [intersection]
                        elif intersection.geom_type in {"MultiPoint", "GeometryCollection"}:
                            points = [item for item in getattr(intersection, "geoms", ()) if item.geom_type == "Point"]
                    if points:
                        pairs.extend((0.0, point) for point in points)
                        continue
                    first, second = nearest_points(main_segment.geometry, other_geometry)
                    gap = float(first.distance(second))
                    if gap <= 100.0:
                        pairs.append((gap, first))
            self.geometric_pair_cache[pair_key] = pairs
        if not pairs:
            return None, None
        eligible = [item for item in pairs if item[0] <= max_gap]
        if not eligible:
            return None, None
        gap, point = min(eligible, key=lambda item: (item[0], float(item[1].distance(reference)) if reference else 0.0))
        return point, gap

    def _candidate(
        self,
        strategy: str,
        geometry,
        row: Mapping[str, Any],
        main: str,
        de_status: str,
        ate_status: str,
        topology_status: str,
        component_status: str,
        confidence: str,
        evidence: Iterable[str],
        warnings: Iterable[str] = (),
        anchors: tuple[Point | None, Point | None] = (None, None),
        snap_used: bool = False,
        snap_distances: tuple[float | None, float | None] = (None, None),
        main_match_score: float | None = None,
        component_index: int | None = None,
    ) -> GeometryRecoveryCandidate | None:
        if geometry is None or geometry.is_empty or geometry.geom_type != "LineString":
            return None
        extension = _float(row.get("extensao_m"))
        length = float(geometry.length)
        deviation = abs(length - extension) / extension * 100 if extension and extension > 0 else None
        reference = self._reference(row)
        proximity = float(geometry.distance(reference)) if reference else None
        continuity = len(list(geometry.coords)) >= 2
        rounded = [(round(x, 2), round(y, 2)) for x, y in geometry.coords]
        loop = len(rounded) != len(set(rounded))
        components = self._component_count(main)
        weights = []
        values = []
        weights.append(25); values.append(1.0 if main in getattr(self.graph, "street_segments", {}) else 0.0)
        weights.append(15); values.append(1.0 if de_status not in {"UNRESOLVED", "CAMPO_VAZIO", "NAO_RESOLVIDA"} else 0.0)
        weights.append(15); values.append(1.0 if ate_status not in {"UNRESOLVED", "CAMPO_VAZIO", "NAO_RESOLVIDA"} else 0.0)
        weights.append(15); values.append(1.0 if topology_status in {"TOPOLOGICAL", "PROJECTED_INTERSECTIONS", "GEOMETRIC_INTERSECTION", "VIRTUAL_SNAP"} else 0.5)
        weights.append(10); values.append(max(0.0, 1.0 - min(proximity or 500.0, 500.0) / 500.0))
        weights.append(10); values.append(1.0 if deviation is None else max(0.0, 1.0 - min(deviation, 100.0) / 100.0))
        weights.append(5); values.append(1.0 if continuity and not loop else 0.0)
        weights.append(5); values.append(1.0 if components == 1 else 0.5)
        score = round(sum(weight * value for weight, value in zip(weights, values)) / sum(weights) * 100, 2)
        candidate = GeometryRecoveryCandidate(
            strategy=strategy, geometry_wkt=geometry.wkt, length_m=length, deviation_pct=deviation,
            start_distance_m=float(geometry.distance(anchors[0])) if anchors[0] is not None else None,
            end_distance_m=float(geometry.distance(anchors[1])) if anchors[1] is not None else None,
            de_status=de_status, ate_status=ate_status, topology_status=topology_status,
            component_status=component_status, snap_used=snap_used,
            snap_distance_de_m=snap_distances[0], snap_distance_ate_m=snap_distances[1],
            confidence=confidence, score=score, evidence=list(evidence), warnings=list(warnings),
            geometry_geojson=json.dumps(mapping(geometry), ensure_ascii=False, default=_json_default),
            segment_count=max(0, len(getattr(self.graph, "street_segments", {}).get(main, ()))),
            component_count=components, max_gap_m=max((item for item in snap_distances if item is not None), default=None),
            loop_detected=loop, main_street=main, main_match_score=main_match_score,
            main_reference_distance_m=proximity, component_index=component_index,
        )
        if loop:
            candidate.warnings.append("LOOP_DETECTADO")
        if deviation is not None and deviation > 50:
            candidate.warnings.append("DESVIO_EXTENSAO_ACIMA_50_PCT")
        return candidate

    def _route_candidate(self, row: Mapping[str, Any], override: Any, main: str, codlog: str) -> GeometryRecoveryCandidate | None:
        if override is None or not override.valid or not override.applicable or override.block_fuzzy:
            return None
        reference = self._reference(row)
        expected = _float(row.get("extensao_m"))
        try:
            route = self.graph.route(main, row.get("de", ""), row.get("ate", ""), reference=reference, expected_length=expected, codlog=codlog)
        except (AttributeError, KeyError, TypeError, ValueError):
            return None
        geometry, status, metadata = route
        if geometry is None:
            return None
        return self._candidate(
            "HUMAN_REVIEW", geometry, row, main,
            metadata.get("method_de", "UNRESOLVED"), metadata.get("method_ate", "UNRESOLVED"),
            "TOPOLOGICAL", "SAME_COMPONENT", "RECONSTRUCTED_HIGH",
            ["decisao_humana_valida", f"status_rota={status}", "segmentos_reais_geosampa"],
            anchors=(None, None),
        )

    def recover(self, row: Mapping[str, Any], current: Mapping[str, Any] | None = None) -> GeometryRecoveryResult:
        recape_id = _text(row.get("id"))
        current_status = _text((current or {}).get("status_path")) or _text((current or {}).get("categoria_falha")) or "SEM_GEOMETRIA"
        override = self.overrides.for_record(row) if self.overrides is not None else None
        if override is not None and override.valid and override.block_fuzzy:
            return GeometryRecoveryResult(recape_id, current_status, False, None, "UNRESOLVED", 0, None, [], True, "Decisão humana bloqueou a resolução automática")
        main, codlog = self._main_street(row, override)
        if not main or main not in getattr(self.graph, "street_segments", {}):
            return GeometryRecoveryResult(recape_id, current_status, False, None, "UNRESOLVED", 0, None, [], True, "Via principal não encontrada no índice")
        reference = self._reference(row)
        candidates: list[GeometryRecoveryCandidate] = []
        human = self._route_candidate(row, override, main, codlog)
        if human is not None:
            candidates.append(human)

        component = self._component_geometry(main, reference)
        if component is None:
            return GeometryRecoveryResult(recape_id, current_status, bool(candidates), candidates[0].strategy if candidates else None, candidates[0].confidence if candidates else "UNRESOLVED", len(candidates), candidates[0] if candidates else None, candidates[1:], not candidates or candidates[0].confidence not in {"CONFIRMED", "RECONSTRUCTED_HIGH"}, "Geometria da via principal indisponível")

        de_point, de_status = self._street_intersections(main, _text(row.get("de")), reference)
        ate_point, ate_status = self._street_intersections(main, _text(row.get("ate")), reference)
        de_gap_point, de_gap = (None, None) if de_point is not None else self._geometric_intersection(main, _text(row.get("de")), reference)
        ate_gap_point, ate_gap = (None, None) if ate_point is not None else self._geometric_intersection(main, _text(row.get("ate")), reference)
        if de_point is None and de_gap_point is not None:
            de_point = de_gap_point
            de_status = "GEOMETRIC_INTERSECTION" if (de_gap or 0.0) <= 0.01 else "GEOMETRIC_SNAP"
        if ate_point is None and ate_gap_point is not None:
            ate_point = ate_gap_point
            ate_status = "GEOMETRIC_INTERSECTION" if (ate_gap or 0.0) <= 0.01 else "GEOMETRIC_SNAP"
        anchor_de = de_point if de_point is not None else None
        anchor_ate = ate_point if ate_point is not None else None
        de_distance = self._gap_to_street(main, _text(row.get("de")), reference)
        ate_distance = self._gap_to_street(main, _text(row.get("ate")), reference)
        de_distance = de_gap if de_gap is not None else de_distance
        ate_distance = ate_gap if ate_gap is not None else ate_distance

        if de_point is not None and ate_point is not None:
            first, last = sorted((self._projection(component, de_point), self._projection(component, ate_point)))
            projected = self._slice(component, first, last)
            geometric = de_status.startswith("GEOMETRIC_") or ate_status.startswith("GEOMETRIC_")
            snap_used = any(value is not None and value > 0.01 for value in (de_gap, ate_gap))
            max_gap = max((value for value in (de_gap, ate_gap) if value is not None), default=0.0)
            strategy = "GEOMETRIC_GAP_SNAP" if snap_used else ("GEOMETRIC_INTERSECTIONS" if geometric else "PROJECTED_INTERSECTIONS")
            topology = "VIRTUAL_SNAP" if snap_used else ("GEOMETRIC_INTERSECTION" if geometric else "PROJECTED_INTERSECTIONS")
            confidence = "RECONSTRUCTED_HIGH" if max_gap <= 0.5 else ("RECONSTRUCTED_MEDIUM" if max_gap <= 2.0 else "ESTIMATED")
            evidence = ["De_e_Ate_resolvidos", "recorte_na_via_principal", "mesmo_componente"]
            warnings = []
            if geometric:
                evidence.append("interseccao_geometrica_sem_no_topologico")
            if snap_used:
                evidence.append(f"snap_virtual_gap_m={max_gap:.2f}")
                warnings.append("snap_virtual_nao_aplicar_sem_revisao" if max_gap > 2.0 else "snap_virtual")
            candidate = self._candidate(
                strategy, projected, row, main, de_status, ate_status,
                topology, "SAME_COMPONENT", confidence, evidence, warnings=warnings,
                anchors=(anchor_de, anchor_ate), snap_used=snap_used, snap_distances=(de_distance, ate_distance),
            )
            if candidate:
                candidates.append(candidate)
        elif de_point is not None or ate_point is not None:
            anchor = de_point or ate_point
            center = self._projection(component, anchor)
            extension = _float(row.get("extensao_m"))
            if extension and extension > 0:
                projected = self._slice(component, center, min(component.length, center + extension))
                candidate = self._candidate(
                    "ONE_BOUNDARY_EXTENSION", projected, row, main, de_status, ate_status,
                    "PROJECTED_ONE_BOUNDARY", "SAME_COMPONENT", "RECONSTRUCTED_MEDIUM",
                    ["uma_transversal_resolvida", "extensao_informada", "recorte_na_via_principal"],
                    warnings=["direcao_inferida"], anchors=(anchor, anchor), snap_distances=(de_distance, ate_distance),
                )
                if candidate:
                    candidates.append(candidate)

        extension = _float(row.get("extensao_m"))
        special_text = f"{_text(row.get('de'))} {_text(row.get('ate'))}".upper()
        if any(token in special_text for token in ("TODA EXTENSAO", "TODA A EXTENSAO", "FIM DA VIA", "ATE O FIM DA VIA")):
            candidate = self._candidate(
                "SPECIAL_WHOLE_COMPONENT", component, row, main, de_status, ate_status,
                "COMPONENT_WHOLE", "SAME_COMPONENT", "RECONSTRUCTED_MEDIUM",
                ["texto_especial", "componente_selecionado_por_coordenada", "segmentos_reais_geosampa"],
                warnings=["validar_extensao_do_componente"], anchors=(anchor_de, anchor_ate),
            )
            if candidate:
                candidates.append(candidate)

        if reference is not None and extension and extension > 0:
            center = self._projection(component, reference)
            half = extension / 2.0
            centered = self._slice(component, center - half, center + half)
            candidate = self._candidate(
                "COORD_EXTENSION_CENTERED", centered, row, main, de_status, ate_status,
                "VIRTUAL_PROJECTION", "SAME_COMPONENT", "ESTIMATED",
                ["coordenada_valida", "extensao_informada", "via_principal_preservada"],
                warnings=["sem_limites_topologicos_confirmados"], anchors=(None, None), snap_distances=(de_distance, ate_distance),
            )
            if candidate:
                candidates.append(candidate)
            directional = self._slice(component, center, min(component.length, center + extension))
            candidate = self._candidate(
                "COORD_EXTENSION_DIRECTIONAL", directional, row, main, de_status, ate_status,
                "VIRTUAL_PROJECTION", "SAME_COMPONENT", "ESTIMATED",
                ["coordenada_valida", "extensao_informada", "direcao_testada"],
                warnings=["sem_limites_topologicos_confirmados"], anchors=(None, None), snap_distances=(de_distance, ate_distance),
            )
            if candidate:
                candidates.append(candidate)

        nearest = self._slice(component, max(0.0, self._projection(component, reference) - 25.0), min(component.length, self._projection(component, reference) + 25.0)) if reference else None
        candidate = self._candidate(
            "NEAREST_SEGMENT_ESTIMATED", nearest, row, main, de_status, ate_status,
            "SEGMENT_NEAR_COORDINATE", "SAME_COMPONENT", "ESTIMATED",
            ["segmentos_da_via_principal", "proximidade_da_coordenada"],
            warnings=["fallback_estimado", "nao_aplicar_sem_revisao"], anchors=(None, None), snap_distances=(de_distance, ate_distance),
        )
        if candidate:
            candidates.append(candidate)

        unique: dict[str, GeometryRecoveryCandidate] = {}
        for candidate in candidates:
            fingerprint = hashlib.sha1((candidate.strategy + (candidate.geometry_wkt or "")).encode("utf-8")).hexdigest()
            unique.setdefault(fingerprint, candidate)
        candidates = sorted(unique.values(), key=lambda item: (item.score, item.confidence), reverse=True)
        selected = candidates[0] if candidates else None
        recovered = selected is not None
        requires_review = not recovered or selected.confidence not in {"CONFIRMED", "RECONSTRUCTED_HIGH"}
        reason = selected.evidence[0] if selected else "Nenhuma estratégia produziu geometria válida"
        return GeometryRecoveryResult(
            recape_id, current_status, recovered, selected.strategy if selected else None,
            selected.confidence if selected else "UNRESOLVED", len(candidates), selected, candidates[1:], requires_review, reason,
        )


class GeometryQualityShadowEngine(GeometryRecoveryEngine):
    """Extensões conservadoras da auditoria, sem tocar no grafo ou no ETL.

    A engine original continua sendo a fonte dos candidatos topológicos. Esta camada
    acrescenta evidência de nome, GPS e componentes desconectados e mantém todos os
    candidatos para inspeção. Nenhum candidato é aplicado a ``recape_clean``.
    """

    SPATIAL_RADIUS_M = 300.0
    MAIN_CANDIDATE_LIMIT = 8
    COMPONENT_CANDIDATE_LIMIT = 4
    # A rota completa permanece disponível na engine original e é usada somente
    # quando habilitada explicitamente por uma futura revisão. Para a auditoria
    # em massa, o fallback GPS/componente é determinístico e não pode bloquear
    # em uma transversal com milhares de segmentos.
    ENABLE_NESTED_MAIN_ROUTE = False
    ENABLE_BIDIRECTIONAL_BOUNDARY_SEARCH = True
    CONFIDENCE_ORDER = {
        "CONFIRMED": 4,
        "RECONSTRUCTED_HIGH": 3,
        "RECONSTRUCTED_MEDIUM": 2,
        "ESTIMATED": 1,
        "UNRESOLVED": 0,
    }

    def __init__(self, graph: Any, normalizer=normalizar_rua, overrides: HumanReviewOverrides | None = None):
        super().__init__(graph, normalizer, overrides)
        self.shadow_spatial_cache: dict[tuple[float, float], list[dict[str, Any]]] = {}
        self.shadow_distance_cache: dict[tuple[str, float, float], float | None] = {}

    @staticmethod
    def _tree_identifier(graph: Any, hit: Any, identifiers: list[str]) -> str | None:
        if isinstance(hit, Integral):
            index = int(hit)
        else:
            index = (getattr(graph, "_geometry_index", {}) or {}).get(id(hit), -1)
        return identifiers[index] if 0 <= index < len(identifiers) else None

    def _spatial_street_candidates(self, reference: Point | None) -> list[dict[str, Any]]:
        if reference is None or getattr(self.graph, "_tree", None) is None:
            return []
        key = (round(reference.x, 1), round(reference.y, 1))
        if key in self.shadow_spatial_cache:
            return self.shadow_spatial_cache[key]
        identifiers = list(getattr(self.graph, "segments", {}) or {})
        by_street: dict[str, float] = {}
        try:
            hits = self.graph._tree.query(reference.buffer(self.SPATIAL_RADIUS_M))
        except (AttributeError, TypeError, ValueError):
            hits = ()
        for hit in hits:
            identifier = self._tree_identifier(self.graph, hit, identifiers)
            segment = getattr(self.graph, "segments", {}).get(identifier) if identifier else None
            if segment is None:
                continue
            distance = float(segment.geometry.distance(reference))
            old = by_street.get(segment.street_norm)
            if old is None or distance < old:
                by_street[segment.street_norm] = distance
        if not by_street:
            try:
                hit = self.graph._tree.nearest(reference)
                identifier = self._tree_identifier(self.graph, hit, identifiers)
                segment = getattr(self.graph, "segments", {}).get(identifier) if identifier else None
                if segment is not None:
                    by_street[segment.street_norm] = float(segment.geometry.distance(reference))
            except (AttributeError, TypeError, ValueError):
                pass
        result = [
            {"street": street, "distance_m": distance, "source": "GPS_SPATIAL"}
            for street, distance in sorted(by_street.items(), key=lambda item: (item[1], item[0]))[: self.MAIN_CANDIDATE_LIMIT]
        ]
        self.shadow_spatial_cache[key] = result
        return result

    def _street_distance(self, street: str, reference: Point | None) -> float | None:
        if reference is None or not street:
            return None
        key = (street, round(reference.x, 1), round(reference.y, 1))
        if key in self.shadow_distance_cache:
            return self.shadow_distance_cache[key]
        distances = []
        for identifier in getattr(self.graph, "street_segments", {}).get(street, ()):
            segment = getattr(self.graph, "segments", {}).get(identifier)
            if segment is not None:
                distances.append(float(segment.geometry.distance(reference)))
        result = min(distances) if distances else None
        self.shadow_distance_cache[key] = result
        return result

    def _main_street_candidates(self, row: Mapping[str, Any]) -> list[dict[str, Any]]:
        reference = self._reference(row)
        value = next((_text(row.get(name)) for name in ("logradouro_geosampa", "via", "rua_raw", "rua_norm") if _text(row.get(name))), "")
        normalized = self.normalizer(value)
        names = list(getattr(self.graph, "street_names", ()) or ())
        options: dict[str, dict[str, Any]] = {}
        spatial_options = {item["street"]: item for item in self._spatial_street_candidates(reference)}

        def add(street: str, name_score: float, distance: float | None, source: str) -> None:
            if not street or street not in getattr(self.graph, "street_segments", {}):
                return
            option = options.get(street)
            payload = {
                "street": street, "name_score": round(float(name_score), 2),
                "distance_m": distance, "source": source,
            }
            if option is None or (
                (distance is not None and option.get("distance_m") is None)
                or (distance is not None and option.get("distance_m") is not None and distance < option["distance_m"])
                or (float(name_score), source == "EXACT") > (option["name_score"], option.get("source") == "EXACT")
            ):
                options[street] = payload

        if normalized in getattr(self.graph, "street_segments", {}):
            add(normalized, 100.0, self._street_distance(normalized, reference), "EXACT")
        if normalized and names:
            try:
                matches = process.extract(normalized, names, scorer=fuzz.token_sort_ratio, limit=self.MAIN_CANDIDATE_LIMIT)
            except (TypeError, ValueError):
                matches = ()
            for street, score, _ in matches:
                # A distância fuzzy fora do envelope espacial não é evidência
                # melhor que o GPS; não percorra toda a rua para calculá-la.
                add(street, score, spatial_options.get(street, {}).get("distance_m"), "FUZZY_NAME")
        for item in spatial_options.values():
            street = item["street"]
            name_score = float(fuzz.token_sort_ratio(normalized, street)) if normalized else 0.0
            add(street, name_score, item.get("distance_m"), item.get("source", "GPS_SPATIAL"))

        ranked = []
        for option in options.values():
            distance = option.get("distance_m")
            geo_score = 0.0 if distance is None else max(0.0, 1.0 - min(float(distance), 300.0) / 300.0)
            name_score = option["name_score"] / 100.0
            option["ranking_score"] = round((0.55 * name_score + 0.45 * geo_score) * 100.0, 2) if reference is not None else option["name_score"]
            ranked.append(option)
        ranked.sort(key=lambda item: (-item["ranking_score"], item.get("distance_m") if item.get("distance_m") is not None else math.inf, item["street"]))
        return ranked[: self.MAIN_CANDIDATE_LIMIT]

    def _component_options(self, street: str, reference: Point | None) -> list[tuple[Any, int, float | None]]:
        identifiers = list(getattr(self.graph, "street_segments", {}).get(street, ()) or ())
        components = tuple(getattr(self.graph, "street_components", {}).get(street, ()) or ())
        if not components:
            return []
        result = []
        for index, component in enumerate(components):
            component_ids = [
                identifier for identifier in identifiers
                if identifier in getattr(self.graph, "segments", {})
                and (self.graph.segments[identifier].start in component or self.graph.segments[identifier].end in component)
            ]
            segments = [self.graph.segments[identifier] for identifier in component_ids]
            if not segments:
                continue
            nearest = min(segments, key=lambda segment: segment.geometry.distance(reference)) if reference else segments[0]
            try:
                geometry, _ = self.graph._whole_component_geometry(street, nearest.start)
            except (AttributeError, KeyError, TypeError):
                geometry = linemerge([segment.geometry for segment in segments])
            if geometry is not None and geometry.geom_type != "LineString":
                geometry = max(getattr(geometry, "geoms", ()), key=lambda part: part.length, default=None)
            if geometry is not None and not geometry.is_empty:
                result.append((geometry, index, float(geometry.distance(reference)) if reference else None))
        result.sort(key=lambda item: item[2] if item[2] is not None else 0.0)
        return result[: self.COMPONENT_CANDIDATE_LIMIT]

    @staticmethod
    def _same_transversal_values(row: Mapping[str, Any], before: Mapping[str, Any], endpoint: str) -> list[str]:
        names = (endpoint, f"{endpoint}_resolved", f"{endpoint}_candidate")
        values: list[str] = []
        for source in (row, before):
            for name in names:
                value = _text(source.get(name))
                if value and value not in values:
                    values.append(value)
        return values

    def _resolve_same_transversal_name(self, value: str) -> tuple[str, str]:
        normalized = self.normalizer(value) if value else ""
        if not normalized:
            return "", ""
        if normalized in getattr(self.graph, "street_segments", {}):
            return normalized, "EXATO"
        try:
            resolved, _, method = self.graph.resolve_street(normalized)
        except (AttributeError, KeyError, TypeError, ValueError):
            return "", ""
        return _text(resolved), _text(method)

    def _same_transversal_equivalence(self, row: Mapping[str, Any], before: Mapping[str, Any]) -> tuple[bool, str, str]:
        de_values = self._same_transversal_values(row, before, "de")
        ate_values = self._same_transversal_values(row, before, "ate")
        if not de_values or not ate_values:
            return False, "", ""
        normalizer = self.normalizer
        for de in de_values:
            for ate in ate_values:
                if normalizer(de) and normalizer(de) == normalizer(ate):
                    resolved, _ = self._resolve_same_transversal_name(de)
                    return bool(resolved), "NORMALIZED_EQUAL", resolved
        for de in de_values:
            for ate in ate_values:
                if self.normalizer(de) == self.normalizer(ate) and self.normalizer(de):
                    resolved, _ = self._resolve_same_transversal_name(de)
                    return bool(resolved), "CANDIDATE_EQUAL", resolved
        resolved_de = [self._resolve_same_transversal_name(value) for value in de_values]
        resolved_ate = [self._resolve_same_transversal_name(value) for value in ate_values]
        exact_methods = {"EXATO", "CODLOG", "ALIAS", "HUMAN", "EXACT"}
        for de_name, de_method in resolved_de:
            for ate_name, ate_method in resolved_ate:
                if de_name and de_name == ate_name and de_method in exact_methods and ate_method in exact_methods:
                    return True, "RESOLVED_EQUAL", de_name
        return False, "", ""

    @staticmethod
    def _intersection_points(value: Any) -> list[Point]:
        if value is None or value.is_empty:
            return []
        if value.geom_type == "Point":
            return [value]
        if value.geom_type in {"MultiPoint", "GeometryCollection"}:
            points: list[Point] = []
            for item in getattr(value, "geoms", ()):
                points.extend(GeometryQualityShadowEngine._intersection_points(item))
            return points
        # Uma sobreposição linear não fornece dois limites físicos; seus
        # extremos não devem virar falsos pontos de interseção.
        return []

    @staticmethod
    def _deduplicate_intersections(items: list[dict[str, Any]], tolerance: float = INTERSECTION_DEDUP_TOLERANCE_M) -> list[dict[str, Any]]:
        distinct: list[dict[str, Any]] = []
        for item in sorted(items, key=lambda value: (value.get("component_index", 0), value.get("distance_m", 0.0), value["point"].x, value["point"].y)):
            same = next((existing for existing in distinct if existing["component_index"] == item["component_index"] and existing["point"].distance(item["point"]) <= tolerance), None)
            if same is None:
                item = dict(item)
                item["duplicate_count"] = 1
                distinct.append(item)
            else:
                same["duplicate_count"] = int(same.get("duplicate_count", 1)) + 1
                same["gap_m"] = min(float(same.get("gap_m", 0.0)), float(item.get("gap_m", 0.0)))
        return distinct

    def _same_transversal_intersections(
        self,
        main: str,
        transversal: str,
        reference: Point | None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[tuple[Any, int, float | None]], Any]:
        """Coleta todas as interseções, preservando componentes e gaps."""
        components = self._component_options(main, reference)
        if not components:
            main_ids = list(getattr(self.graph, "street_segments", {}).get(main, ()) or ())
            fallback_segments = [self.graph.segments[item] for item in main_ids if item in getattr(self.graph, "segments", {})]
            geometry = linemerge([segment.geometry for segment in fallback_segments]) if fallback_segments else None
            if geometry is not None and geometry.geom_type != "LineString":
                geometry = max(getattr(geometry, "geoms", ()), key=lambda part: part.length, default=None)
            components = [(geometry, 0, float(geometry.distance(reference)) if geometry is not None and reference else None)] if geometry is not None else []
        transversal_ids = list(getattr(self.graph, "street_segments", {}).get(transversal, ()) or ())
        transversal_segments = [self.graph.segments[item] for item in transversal_ids if item in getattr(self.graph, "segments", {})]
        raw: list[dict[str, Any]] = []
        for component, component_index, _ in components:
            if component is None or component.is_empty:
                continue
            component_nodes = None
            graph_components = tuple(getattr(self.graph, "street_components", {}).get(main, ()) or ())
            if component_index < len(graph_components):
                component_nodes = set(graph_components[component_index])
            main_ids = list(getattr(self.graph, "street_segments", {}).get(main, ()) or ())
            main_segments = []
            for identifier in main_ids:
                segment = getattr(self.graph, "segments", {}).get(identifier)
                if segment is None:
                    continue
                if component_nodes is None or segment.start in component_nodes or segment.end in component_nodes:
                    main_segments.append(segment)
            for main_segment in main_segments:
                for transversal_segment in transversal_segments:
                    intersection = main_segment.geometry.intersection(transversal_segment.geometry)
                    points = self._intersection_points(intersection)
                    if points:
                        for point in points:
                            raw.append({"point": point, "component_index": component_index, "gap_m": 0.0, "kind": "EXACT", "main_segment": main_segment.identifier, "transversal_segment": transversal_segment.identifier})
                        continue
                    first, second = nearest_points(main_segment.geometry, transversal_segment.geometry)
                    gap = float(first.distance(second))
                    if gap <= SAME_TRANSVERSAL_MAX_GAP_M:
                        raw.append({"point": first, "component_index": component_index, "gap_m": gap, "kind": "GAP", "main_segment": main_segment.identifier, "transversal_segment": transversal_segment.identifier})
        distinct = self._deduplicate_intersections(raw)
        for item in distinct:
            component = next((value[0] for value in components if value[1] == item["component_index"]), None)
            item["distance_m"] = float(component.project(item["point"])) if component is not None else 0.0
        return raw, distinct, components, transversal_segments

    @staticmethod
    def _legitimate_loop(geometry: Any) -> bool:
        if geometry is None or geometry.is_empty or not hasattr(geometry, "coords"):
            return False
        points = [(round(x, 2), round(y, 2)) for x, y in geometry.coords]
        positions: dict[tuple[float, float], list[int]] = defaultdict(list)
        for index, point in enumerate(points):
            positions[point].append(index)
        return bool(positions) and all(max(indexes) - min(indexes) <= 1 for indexes in positions.values() if len(indexes) > 1)

    def _same_transversal_pair_candidate(
        self,
        row: Mapping[str, Any],
        main: str,
        component: Any,
        component_index: int,
        first: dict[str, Any],
        second: dict[str, Any],
        pair_index: int,
        reference: Point | None,
    ) -> tuple[GeometryRecoveryCandidate | None, float, dict[str, Any]]:
        start, end = sorted((first["distance_m"], second["distance_m"]))
        geometry = self._slice(component, start, end)
        if geometry is None or geometry.length < SAME_TRANSVERSAL_MIN_LENGTH_M:
            return None, 0.0, {"invalid": "TRECHO_QUASE_ZERO"}
        candidate = self._candidate(
            SAME_TRANSVERSAL_STRATEGY, geometry, row, main, "SAME_TRANSVERSAL", "SAME_TRANSVERSAL",
            "SAME_TRANSVERSAL_INTERSECTIONS", "SAME_COMPONENT", "ESTIMATED",
            ["de_e_ate_mesma_transversal", "duas_intersecoes_fisicas", "recorte_somente_na_via_principal"],
            warnings=["same_transversal_candidate"], anchors=(first["point"], second["point"]),
            component_index=component_index,
        )
        if candidate is None:
            return None, 0.0, {"invalid": "GEOMETRIA_INVALIDA"}
        deviation = candidate.deviation_pct
        gps_distance = float(geometry.distance(reference)) if reference is not None else None
        gps_score = max(0.0, 100.0 - min(gps_distance or 300.0, 300.0) / 300.0 * 100.0) if reference is not None else 0.0
        extension_score = max(0.0, 100.0 - min(deviation if deviation is not None else 100.0, 100.0))
        gap = max(float(first.get("gap_m", 0.0)), float(second.get("gap_m", 0.0)))
        topology_score = 100.0 if gap <= 0.5 else max(0.0, 100.0 - gap / SAME_TRANSVERSAL_MAX_GAP_M * 40.0)
        continuous = bool(geometry.is_valid and len(list(geometry.coords)) >= 2)
        continuity_score = 100.0 if continuous and component_index >= 0 else 0.0
        base_score = round(0.30 * gps_score + 0.25 * extension_score + 0.20 * topology_score + 0.15 * continuity_score + 0.10 * 100.0, 2)
        warnings = list(candidate.warnings)
        if gps_distance is None:
            warnings.append("same_transversal_sem_gps")
        if deviation is not None and deviation > SAME_TRANSVERSAL_EXT_MEDIUM_PCT:
            warnings.append("same_transversal_desvio_extensao_alto")
        if candidate.loop_detected and self._legitimate_loop(geometry):
            candidate = replace(candidate, loop_detected=False)
            warnings = [item for item in warnings if item != "LOOP_DETECTADO"]
            warnings.append("LOOP_LEGITIMO_FORMATO_VIA")
        candidate = replace(candidate, score=base_score, warnings=warnings)
        metadata = {
            "pair_index": pair_index, "component_index": component_index, "start": first, "end": second,
            "distance_to_gps_m": gps_distance, "base_score": base_score, "gap_m": gap,
        }
        return candidate, base_score, metadata

    def same_transversal_analysis(self, row: Mapping[str, Any], before: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Analisa a transversal repetida sem alterar grafo, caches ou ETL."""
        before = before or {}
        equivalent, equivalence_reason, transversal = self._same_transversal_equivalence(row, before)
        main = _text(before.get("via_resolvida")) or self.normalizer(_text(row.get("logradouro_geosampa") or row.get("via") or row.get("rua_raw")))
        reference = self._reference(row)
        diagnostics: dict[str, Any] = {
            "eligible": False, "equivalence_reason": equivalence_reason, "main_street": main,
            "transversal_resolvida": transversal, "intersection_count_raw": 0, "intersection_count_distinct": 0,
            "intersection_points": [], "candidate_pair_count": 0, "selected_pair_index": None,
            "selected_start_point": None, "selected_end_point": None, "margin_top2": None,
            "ambiguous": False, "invalid": 0, "reason": "PADRAO_NAO_DETECTADO", "candidates": [],
        }
        if not equivalent or not main or main not in getattr(self.graph, "street_segments", {}) or not transversal:
            return diagnostics
        raw, distinct, components, transversal_segments = self._same_transversal_intersections(main, transversal, reference)
        diagnostics.update({
            "eligible": True, "reason": "PADRAO_DETECTADO", "intersection_count_raw": len(raw),
            "intersection_count_distinct": len(distinct),
            "intersection_points": [
                {"x": round(item["point"].x, 6), "y": round(item["point"].y, 6), "distance_m": round(item.get("distance_m", 0.0), 6), "component_index": item["component_index"], "gap_m": round(float(item.get("gap_m", 0.0)), 6), "kind": item.get("kind"), "duplicate_count": item.get("duplicate_count", 1)}
                for item in distinct
            ],
            "component_count": len(components),
            "main_geometry_wkt": next((component.wkt for component, _, _ in components if component is not None), None),
            "transversal_geometry_wkt": linemerge([segment.geometry for segment in transversal_segments]).wkt if transversal_segments else None,
        })
        if len(distinct) < 2:
            diagnostics["reason"] = "UMA_INTERSECAO_DISTINTA_OU_MENOS"
            return diagnostics
        pairs: list[tuple[GeometryRecoveryCandidate, float, dict[str, Any]]] = []
        pair_index = 0
        for component, component_index, _ in components:
            points = [item for item in distinct if item["component_index"] == component_index]
            points.sort(key=lambda item: item.get("distance_m", 0.0))
            for first, second in itertools.combinations(points, 2):
                candidate, base_score, metadata = self._same_transversal_pair_candidate(row, main, component, component_index, first, second, pair_index, reference)
                pair_index += 1
                if candidate is None:
                    diagnostics["invalid"] += 1
                    continue
                pairs.append((candidate, base_score, metadata))
        diagnostics["candidate_pair_count"] = len(pairs)
        if not pairs:
            diagnostics["reason"] = "NENHUM_PAR_VALIDO"
            return diagnostics
        pairs.sort(key=lambda item: (-item[1], item[2]["component_index"], item[2]["pair_index"]))
        top_base = pairs[0][1]
        second_base = pairs[1][1] if len(pairs) > 1 else None
        margin = round(top_base - second_base, 2) if second_base is not None else 100.0
        ambiguous = len(distinct) > 2 or len({item[2]["component_index"] for item in pairs}) > 1 or (second_base is not None and margin < 5.0)
        for index, (candidate, base_score, metadata) in enumerate(pairs):
            candidate_margin = margin if index == 0 else max(0.0, base_score - (pairs[index + 1][1] if index + 1 < len(pairs) else 0.0))
            gps_distance = metadata["distance_to_gps_m"]
            deviation = candidate.deviation_pct
            gap = metadata["gap_m"]
            high = len(distinct) == 2 and not ambiguous and candidate.component_status == "SAME_COMPONENT" and gap <= 0.5 and (gps_distance is not None and gps_distance <= SAME_TRANSVERSAL_GPS_HIGH_M) and (deviation is None or deviation <= SAME_TRANSVERSAL_EXT_HIGH_PCT) and candidate.loop_detected is False and candidate.geometry_wkt is not None and candidate_margin >= 5.0
            medium = not high and candidate.component_status == "SAME_COMPONENT" and gap <= SAME_TRANSVERSAL_MAX_GAP_M and (gps_distance is None or gps_distance <= SAME_TRANSVERSAL_GPS_MEDIUM_M) and (deviation is None or deviation <= SAME_TRANSVERSAL_EXT_MEDIUM_PCT) and candidate.geometry_wkt is not None
            confidence = "RECONSTRUCTED_HIGH" if high else ("RECONSTRUCTED_MEDIUM" if medium else "ESTIMATED")
            warnings = list(candidate.warnings)
            if len(distinct) > 2:
                warnings.append("same_transversal_mais_de_duas_intersecoes")
            if ambiguous:
                warnings.append("same_transversal_ambigua")
            candidate = replace(candidate, confidence=confidence, score=round(min(100.0, base_score + min(candidate_margin, 100.0) * 0.10), 2), warnings=warnings)
            pairs[index] = (candidate, base_score, metadata)
        selected, _, selected_metadata = pairs[0]
        diagnostics.update({
            "ambiguous": ambiguous, "margin_top2": margin, "selected_pair_index": selected_metadata["pair_index"],
            "selected_start_point": [round(selected_metadata["start"]["point"].x, 6), round(selected_metadata["start"]["point"].y, 6)],
            "selected_end_point": [round(selected_metadata["end"]["point"].x, 6), round(selected_metadata["end"]["point"].y, 6)],
            "after_confidence": selected.confidence, "after_strategy": selected.strategy,
            "after_length_m": selected.length_m, "extension_deviation_pct": selected.deviation_pct,
            "distance_to_gps_m": selected_metadata["distance_to_gps_m"], "score": selected.score,
            "candidates": [candidate for candidate, _, _ in pairs],
            "alternatives": [candidate.as_dict() for candidate, _, _ in pairs[1:]],
        })
        return diagnostics

    def _gps_growth_candidates(self, row: Mapping[str, Any], option: Mapping[str, Any]) -> list[GeometryRecoveryCandidate]:
        street = option["street"]
        reference = self._reference(row)
        result: list[GeometryRecoveryCandidate] = []
        for component, component_index, _ in self._component_options(street, reference):
            center = self._projection(component, reference) if reference else 0.0
            extension = _float(row.get("extensao_m"))
            geometries: list[tuple[str, Any, list[str]]] = []
            if extension and extension > 0:
                half = extension / 2.0
                geometries.extend((
                    ("GPS_SNAP_LINEAR_GROWTH_CENTERED", self._slice(component, center - half, center + half), ["ponto_gps_snapped", "crescimento_linear_centrado"]),
                    ("GPS_SNAP_LINEAR_GROWTH_FORWARD", self._slice(component, center, min(component.length, center + extension)), ["ponto_gps_snapped", "crescimento_linear_direcao_frente"]),
                    ("GPS_SNAP_LINEAR_GROWTH_BACKWARD", self._slice(component, max(0.0, center - extension), center), ["ponto_gps_snapped", "crescimento_linear_direcao_reversa"]),
                ))
            else:
                geometries.append(("GPS_SNAP_NEAREST_SEGMENT", self._slice(component, max(0.0, center - 25.0), min(component.length, center + 25.0)), ["ponto_gps_snapped", "segmento_mais_proximo"]))
            for strategy, geometry, evidence in geometries:
                candidate = self._candidate(
                    strategy, geometry, row, street, "UNRESOLVED", "UNRESOLVED",
                    "GPS_SNAP_LINEAR", "COMPONENT_SELECTED_BY_GPS", "ESTIMATED",
                    ["via_principal_shadow", *evidence, f"match_via_pct={option['name_score']:.2f}", f"distancia_via_gps_m={option.get('distance_m') if option.get('distance_m') is not None else 'NA'}"],
                    warnings=["sem_limites_topologicos_confirmados", "nao_aplicar_sem_revisao"],
                    component_index=component_index, main_match_score=option["name_score"],
                )
                if candidate is not None:
                    result.append(candidate)
        return result

    def _bidirectional_boundary_candidates(self, row: Mapping[str, Any], base: GeometryRecoveryResult) -> list[GeometryRecoveryCandidate]:
        if not self.ENABLE_BIDIRECTIONAL_BOUNDARY_SEARCH:
            return []
        if base.selected_candidate is None or base.confidence not in {"ESTIMATED", "UNRESOLVED"}:
            return []
        selected = base.selected_candidate
        # Vias muito longas tornam a procura de interseções uma operação
        # desproporcional; o crescimento GPS continua disponível nesses casos.
        if (selected.segment_count or 0) > 50:
            return []
        if selected.strategy not in {"ONE_BOUNDARY_EXTENSION", "ONE_BOUNDARY_EXTENSION_FORWARD", "ONE_BOUNDARY_EXTENSION_BACKWARD"}:
            return []
        selected_de_known = selected.de_status not in {"UNRESOLVED", "CAMPO_VAZIO", "NAO_RESOLVIDA"}
        selected_ate_known = selected.ate_status not in {"UNRESOLVED", "CAMPO_VAZIO", "NAO_RESOLVIDA"}
        if selected_de_known == selected_ate_known:
            return []
        main = selected.main_street or self.normalizer(_text(row.get("logradouro_geosampa") or row.get("via") or row.get("rua_raw")))
        if not main or main not in getattr(self.graph, "street_segments", {}):
            return []
        de_point, de_status = (self._street_intersections(main, _text(row.get("de")), self._reference(row)) if selected_de_known else (None, selected.de_status))
        ate_point, ate_status = (self._street_intersections(main, _text(row.get("ate")), self._reference(row)) if selected_ate_known else (None, selected.ate_status))
        if (de_point is None) == (ate_point is None):
            return []
        anchor = de_point or ate_point
        component_options = self._component_options(main, self._reference(row))
        extension = _float(row.get("extensao_m"))
        if anchor is None or not extension or extension <= 0 or not component_options:
            return []
        result = []
        for component, component_index, _ in component_options[:1]:
            center = self._projection(component, anchor)
            for direction, start, end in (
                ("FORWARD", center, min(component.length, center + extension)),
                ("BACKWARD", max(0.0, center - extension), center),
            ):
                geometry = self._slice(component, start, end)
                if geometry is None:
                    continue
                reference = self._reference(row)
                deviation = abs(geometry.length - extension) / extension * 100 if extension else 0.0
                confidence = "RECONSTRUCTED_MEDIUM" if deviation <= 20.0 and (reference is None or geometry.distance(reference) <= 35.0) else "ESTIMATED"
                candidate = self._candidate(
                    f"ONE_BOUNDARY_EXTENSION_{direction}", geometry, row, main, de_status, ate_status,
                    "PROJECTED_ONE_BOUNDARY_DIRECTIONAL", "SAME_COMPONENT", confidence,
                    ["uma_transversal_valida", "extensao_informada", "direcao_testada", "ponto_gps_comparado"],
                    warnings=["limite_inferido", "nao_aplicar_sem_revisao"], anchors=(anchor, anchor),
                    component_index=component_index,
                )
                if candidate is not None:
                    result.append(candidate)
        return result

    @staticmethod
    def _candidate_from_payload(payload: Mapping[str, Any]) -> GeometryRecoveryCandidate | None:
        geometry_wkt = payload.get("geometry_wkt")
        if not _text(geometry_wkt):
            return None
        fields = {field.name for field in GeometryRecoveryCandidate.__dataclass_fields__.values()}
        values = {field: payload.get(field) for field in fields if field in payload}
        values.setdefault("strategy", _text(payload.get("strategy")) or "UNRESOLVED")
        values.setdefault("geometry_wkt", geometry_wkt)
        for field in ("length_m", "deviation_pct", "start_distance_m", "end_distance_m", "snap_distance_de_m", "snap_distance_ate_m", "score", "max_gap_m", "main_match_score", "main_reference_distance_m"):
            if field in values:
                values[field] = _float(values[field])
        for field in ("segment_count", "component_count", "component_index"):
            if field in values:
                number = _float(values[field])
                values[field] = int(number) if number is not None else (0 if field != "component_index" else None)
        for field in ("snap_used", "loop_detected"):
            if field in values:
                values[field] = _bool(values[field])
        for field in ("path_nodes", "evidence", "warnings"):
            if not isinstance(values.get(field), list):
                values[field] = []
        return GeometryRecoveryCandidate(**values)

    def _baseline_result(self, row: Mapping[str, Any], current: Mapping[str, Any], before: Mapping[str, Any]) -> GeometryRecoveryResult:
        selected = self._candidate_from_payload(before)
        if selected is not None:
            selected = replace(
                selected,
                strategy=_text(before.get("strategy_selected")) or selected.strategy,
                confidence=_text(before.get("geometry_confidence")) or selected.confidence,
                score=_float(before.get("geometry_score")) or selected.score,
                length_m=_float(before.get("path_length_m")) or selected.length_m,
                deviation_pct=_float(before.get("extension_deviation_pct")) if _float(before.get("extension_deviation_pct")) is not None else selected.deviation_pct,
                segment_count=int(_float(before.get("segment_count")) or selected.segment_count),
                component_count=int(_float(before.get("component_count")) or selected.component_count),
                main_street=_text(before.get("via_resolvida")) or selected.main_street,
                evidence=selected.evidence or [_text(before.get("reason")) or "linha_de_base"],
                warnings=selected.warnings or [item.strip() for item in _text(before.get("warnings")).split("|") if item.strip()],
            )
        # As alternativas antigas permanecem como JSON no resultado. Só o
        # candidato selecionado precisa ser reidratado para receber novas
        # heurísticas; isso evita parsear milhares de geometrias repetidamente.
        alternatives: list[GeometryRecoveryCandidate] = []
        current_status = _text(current.get("status_path")) or _text(current.get("categoria_falha")) or "SEM_GEOMETRIA"
        confidence = _text(before.get("geometry_confidence")) or "UNRESOLVED"
        return GeometryRecoveryResult(
            _text(row.get("id")), current_status, selected is not None, _text(before.get("strategy_selected")) or None,
            confidence, int(before.get("candidate_count") or (len(alternatives) + (1 if selected else 0))), selected, alternatives,
            _bool(before.get("requires_review")), _text(before.get("reason")) or "linha de base da auditoria anterior",
            _text(before.get("alternatives_json")) or None,
        )

    @staticmethod
    def _rank_candidates(candidates: Iterable[GeometryRecoveryCandidate]) -> list[GeometryRecoveryCandidate]:
        unique: dict[str, GeometryRecoveryCandidate] = {}
        for candidate in candidates:
            fingerprint = hashlib.sha1((candidate.strategy + (candidate.geometry_wkt or "")).encode("utf-8")).hexdigest()
            unique.setdefault(fingerprint, candidate)
        return sorted(
            unique.values(),
            key=lambda item: (
                GeometryQualityShadowEngine.CONFIDENCE_ORDER.get(item.confidence, 0),
                float(item.score),
                float(item.main_match_score or 0.0),
                -float(item.main_reference_distance_m or 999999.0),
            ),
            reverse=True,
        )

    def recover(self, row: Mapping[str, Any], current: Mapping[str, Any] | None = None, baseline_audit: Mapping[str, Any] | None = None) -> GeometryRecoveryResult:
        if baseline_audit is not None and _text(baseline_audit.get("geometry_confidence")) == "ESTIMATED":
            base = self._baseline_result(row, current or {}, baseline_audit)
        else:
            base = super().recover(row, current)
        same_analysis = self.same_transversal_analysis(row, baseline_audit or {}) if base.confidence in {"ESTIMATED", "UNRESOLVED"} else {
            "eligible": False, "reason": "BASELINE_NAO_ESTIMATED", "candidates": [], "ambiguous": False,
        }
        candidates: list[GeometryRecoveryCandidate] = []
        if base.selected_candidate is not None:
            candidates.append(base.selected_candidate)
        candidates.extend(base.alternatives)
        same_candidates = list(same_analysis.get("candidates", []))
        candidates.extend(same_candidates)
        if "Decisão humana bloqueou" in base.reason:
            return base

        needs_shadow = base.confidence in {"ESTIMATED", "UNRESOLVED"}
        # A via já encontrada não precisa passar novamente pelo fuzzy/índice
        # espacial. O custo adicional fica restrito aos casos realmente
        # UNRESOLVED, onde a resolução da via é a evidência faltante.
        main_options = self._main_street_candidates(row) if base.confidence == "UNRESOLVED" else []
        if base.confidence == "UNRESOLVED" and main_options:
            best = main_options[0]
            variant = dict(row)
            variant.update({"via": best["street"], "logradouro_geosampa": best["street"], "rua_raw": best["street"], "rua_norm": best["street"]})
            routed = None
            if self.ENABLE_NESTED_MAIN_ROUTE and best.get("name_score", 0.0) >= 85.0 and (best.get("distance_m") is not None and best.get("distance_m") <= 5.0):
                try:
                    routed = super().recover(variant, current)
                except (AttributeError, KeyError, TypeError, ValueError):
                    routed = None
            if routed is not None and routed.selected_candidate is not None:
                nested = [routed.selected_candidate, *routed.alternatives]
                for candidate in nested:
                    candidates.append(replace(
                        candidate,
                        strategy=f"SHADOW_MAIN_{candidate.strategy}",
                        main_street=best["street"],
                        main_match_score=best["name_score"],
                        main_reference_distance_m=best.get("distance_m"),
                        evidence=["via_principal_resolvida_por_nome_gps", *candidate.evidence],
                        warnings=["via_principal_shadow", *candidate.warnings],
                    ))
            candidates.extend(self._gps_growth_candidates(row, best))
            for option in main_options[1:3]:
                candidates.extend(self._gps_growth_candidates(row, option))
        elif needs_shadow:
            candidates.extend(self._bidirectional_boundary_candidates(row, base))
            for option in main_options[:2]:
                if option.get("street") != (base.selected_candidate.main_street if base.selected_candidate else None):
                    candidates.extend(self._gps_growth_candidates(row, option))

        ranked = self._rank_candidates(candidates)
        if base.selected_candidate is not None and same_candidates:
            best_same = max(same_candidates, key=lambda item: (self.CONFIDENCE_ORDER.get(item.confidence, 0), item.score))
            base_rank = self.CONFIDENCE_ORDER.get(base.selected_candidate.confidence, 0)
            same_rank = self.CONFIDENCE_ORDER.get(best_same.confidence, 0)
            objectively_better = same_rank > base_rank or (same_rank == base_rank and best_same.score >= base.selected_candidate.score + 1.0)
            if not objectively_better:
                ranked = [item for item in ranked if not item.strategy.startswith(SAME_TRANSVERSAL_STRATEGY)]
                if base.selected_candidate not in ranked:
                    ranked.insert(0, base.selected_candidate)
        selected = ranked[0] if ranked else None
        if selected is None:
            return GeometryRecoveryResult(
                base.recape_id, base.current_status, False, None, "UNRESOLVED", 0, None, [], True,
                base.reason or "Nenhuma estratégia shadow produziu geometria válida", base.baseline_alternatives_json,
                {"same_transversal": same_analysis},
            )
        ambiguity = any(item.confidence == selected.confidence and item.score >= selected.score - 5.0 for item in ranked[1:])
        requires_review = ambiguity or selected.confidence not in {"CONFIRMED", "RECONSTRUCTED_HIGH"}
        if selected.main_match_score is not None and (selected.main_match_score < 75.0 or (selected.main_reference_distance_m or 0.0) > 80.0):
            requires_review = True
        return GeometryRecoveryResult(
            base.recape_id, base.current_status, True, selected.strategy, selected.confidence,
            max(len(ranked), base.candidate_count), selected, ranked[1:], requires_review,
            selected.evidence[0] if selected.evidence else base.reason, base.baseline_alternatives_json,
            {"same_transversal": same_analysis},
        )


def _source_signature(paths: Iterable[Path]) -> list[list[Any]]:
    result = []
    for path in paths:
        if path.exists():
            stat = path.stat()
            # JSON checkpoints round-trip lists, not tuples. Keeping the
            # signature JSON-native makes ``--resume`` deterministic.
            result.append([str(path), int(stat.st_size), int(stat.st_mtime_ns)])
    return result


def _load_shadow_graph(graph_path: Path, source_path: Path) -> Any:
    """Carrega o grafo existente sem exigir reconstrução ou persistência.

    Alguns caches antigos foram serializados quando ``transform.py`` era executado
    como ``__main__``. O fallback abaixo apenas reata esses nomes durante o unpickle;
    não salva, corrige ou altera o arquivo do grafo.
    """
    graph = RoadGraph.load_cached(graph_path, source_path, normalizer=normalizar_rua)
    if graph is not None:
        return graph
    with graph_path.open("rb") as stream:
        original_main = sys.modules.get("__main__")
        had_normalizer = hasattr(original_main, "normalizar_rua") if original_main else False
        had_graph = hasattr(original_main, "RoadGraph") if original_main else False
        old_normalizer = getattr(original_main, "normalizar_rua", None) if original_main else None
        old_graph = getattr(original_main, "RoadGraph", None) if original_main else None
        try:
            if original_main is not None:
                original_main.normalizar_rua = normalizar_rua
                original_main.RoadGraph = RoadGraph
            payload = pickle.load(stream)
        finally:
            if original_main is not None:
                if had_normalizer:
                    original_main.normalizar_rua = old_normalizer
                else:
                    original_main.__dict__.pop("normalizar_rua", None)
                if had_graph:
                    original_main.RoadGraph = old_graph
                else:
                    original_main.__dict__.pop("RoadGraph", None)
    if not isinstance(payload, Mapping) or payload.get("version") != getattr(RoadGraph, "CACHE_VERSION", None):
        raise RuntimeError("Cache do RoadGraph incompatível com a auditoria shadow")
    recorded = payload.get("source")
    current = RoadGraph._source_signature(source_path)
    if recorded and current and recorded[0] != current[0]:
        raise RuntimeError("Fonte GeoSampa incompatível com o cache do RoadGraph")
    graph = payload.get("graph")
    if graph is None:
        raise RuntimeError("Cache shadow não contém RoadGraph")
    graph.normalizer = normalizar_rua
    graph._rebuild_spatial_index()
    return graph


def _baseline_index(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    frame = pd.read_csv(path, encoding="utf-8-sig", dtype=str, keep_default_na=True, na_values=[""])
    return {_text(row.get("id")): row.to_dict() for _, row in frame.iterrows() if _text(row.get("id"))}


def _result_row(raw: Mapping[str, Any], baseline: Mapping[str, Any], result: GeometryRecoveryResult) -> dict[str, Any]:
    selected = result.selected_candidate
    ambiguous = bool(
        selected is not None
        and any(candidate.score >= selected.score - 5.0 for candidate in result.alternatives)
    )
    alternatives = [candidate.as_dict() for candidate in result.alternatives]
    alternatives_json = json.dumps(alternatives, ensure_ascii=False, default=_json_default)
    if result.baseline_alternatives_json:
        if not alternatives:
            alternatives_json = result.baseline_alternatives_json
        else:
            try:
                baseline_alternatives = json.loads(result.baseline_alternatives_json)
                if isinstance(baseline_alternatives, list):
                    alternatives_json = json.dumps(baseline_alternatives + alternatives, ensure_ascii=False, default=_json_default)
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
    output = {
        "id": _text(raw.get("id")), "via": _text(raw.get("via") or raw.get("rua_raw")),
        "via_resolvida": _text(raw.get("rua_norm")) or normalizar_rua(_text(raw.get("via") or raw.get("rua_raw"))),
        "codlog": _text(raw.get("codlog") or raw.get("cd_codlog")), "de": _text(raw.get("de")), "ate": _text(raw.get("ate")),
        "latitude": raw.get("latitude"), "longitude": raw.get("longitude"), "extensao_m": raw.get("extensao_m"),
        "status_atual": _text(baseline.get("status_path")), "categoria_falha_atual": _text(baseline.get("categoria_falha")),
        "strategy_selected": result.selected_strategy, "geometry_confidence": result.confidence,
        "geometry_score": selected.score if selected else 0.0, "candidate_count": result.candidate_count,
        "ambiguous_candidates": ambiguous, "recovered": result.recovered,
        "requires_review": result.requires_review, "de_resolved": selected.de_status if selected else "UNRESOLVED",
        "ate_resolved": selected.ate_status if selected else "UNRESOLVED", "de_status": selected.de_status if selected else "UNRESOLVED",
        "ate_status": selected.ate_status if selected else "UNRESOLVED", "de_candidate": _text(raw.get("de")), "ate_candidate": _text(raw.get("ate")),
        "topology_status": selected.topology_status if selected else "UNRESOLVED", "component_status": selected.component_status if selected else "UNRESOLVED",
        "snap_used": selected.snap_used if selected else False, "snap_distance_de_m": selected.snap_distance_de_m if selected else None,
        "snap_distance_ate_m": selected.snap_distance_ate_m if selected else None, "path_length_m": selected.length_m if selected else None,
        "extension_deviation_pct": selected.deviation_pct if selected else None, "segment_count": selected.segment_count if selected else 0,
        "component_count": selected.component_count if selected else 0, "max_gap_m": selected.max_gap_m if selected else None,
        "loop_detected": selected.loop_detected if selected else False, "geometry_wkt": selected.geometry_wkt if selected else None,
        "geometry_geojson": selected.geometry_geojson if selected else None, "reason": result.reason,
        "main_street": selected.main_street if selected else None,
        "main_match_score": selected.main_match_score if selected else None,
        "main_reference_distance_m": selected.main_reference_distance_m if selected else None,
        "component_index": selected.component_index if selected else None,
        "warnings": " | ".join(selected.warnings) if selected else "sem_candidato", "alternatives_json": alternatives_json,
    }
    same = result.diagnostics.get("same_transversal", {}) if isinstance(result.diagnostics, Mapping) else {}
    output.update({
        "same_transversal_eligible": bool(same.get("eligible", False)),
        "same_transversal_strategy": same.get("after_strategy"),
        "same_transversal_after_confidence": same.get("after_confidence"),
        "same_transversal_intersection_count_raw": same.get("intersection_count_raw", 0),
        "same_transversal_intersection_count_distinct": same.get("intersection_count_distinct", 0),
        "same_transversal_intersection_points_json": json.dumps(same.get("intersection_points", []), ensure_ascii=False, default=_json_default),
        "same_transversal_candidate_pair_count": same.get("candidate_pair_count", 0),
        "same_transversal_selected_pair_index": same.get("selected_pair_index"),
        "same_transversal_selected_start_point": json.dumps(same.get("selected_start_point"), ensure_ascii=False),
        "same_transversal_selected_end_point": json.dumps(same.get("selected_end_point"), ensure_ascii=False),
        "same_transversal_margin_top2": same.get("margin_top2"),
        "same_transversal_distance_to_gps_m": same.get("distance_to_gps_m"),
        "same_transversal_ambiguous": bool(same.get("ambiguous", False)),
        "same_transversal_invalid": same.get("invalid", 0),
        "same_transversal_reason": same.get("reason"),
        "same_transversal_main_geometry_wkt": same.get("main_geometry_wkt"),
        "same_transversal_transversal_geometry_wkt": same.get("transversal_geometry_wkt"),
    })
    return output


def _review_row(row: Mapping[str, Any]) -> bool:
    high_gap = any(
        (_float(row.get(name)) or 0.0) > 2.0
        for name in ("snap_distance_de_m", "snap_distance_ate_m", "max_gap_m")
    )
    high_deviation = (_float(row.get("extension_deviation_pct")) or 0.0) > 50.0
    multi_component = int(_float(row.get("component_count")) or 0) > 1
    ambiguous = _bool(row.get("ambiguous_candidates"))
    if not ambiguous:
        try:
            alternatives = json.loads(_text(row.get("alternatives_json")) or "[]")
            score = _float(row.get("geometry_score")) or 0.0
            ambiguous = any((_float(item.get("score")) or 0.0) >= score - 5.0 for item in alternatives if isinstance(item, Mapping))
        except (TypeError, ValueError, json.JSONDecodeError):
            ambiguous = False
    return (
        _text(row.get("geometry_confidence")) in {"RECONSTRUCTED_MEDIUM", "ESTIMATED"}
        or _bool(row.get("requires_review"))
        or ambiguous
        or high_gap or high_deviation or multi_component or _bool(row.get("loop_detected"))
    )


def _quality_root_causes(raw: Mapping[str, Any], current: Mapping[str, Any], result: GeometryRecoveryResult) -> list[str]:
    """Classifica causas observáveis; uma linha pode ter mais de uma causa."""
    selected = result.selected_candidate
    failure = _text(current.get("categoria_falha"))
    de_status = selected.de_status if selected else "UNRESOLVED"
    ate_status = selected.ate_status if selected else "UNRESOLVED"
    de_known = de_status not in {"UNRESOLVED", "CAMPO_VAZIO", "NAO_RESOLVIDA"}
    ate_known = ate_status not in {"UNRESOLVED", "CAMPO_VAZIO", "NAO_RESOLVIDA"}
    text_blob = " ".join(_text(raw.get(name)) for name in ("via", "logradouro_geosampa", "de", "ate", "Complemento")).upper()
    causes: list[str] = []

    def add(value: str) -> None:
        if value not in causes:
            causes.append(value)

    if any(token in text_blob for token in ("TODA EXTENSAO", "TODA A EXTENSAO", "VIA INTEIRA", "FIM DA VIA", "ATE O FIM DA VIA")):
        add("VIA_INTEIRA")
    if de_known ^ ate_known:
        add("APENAS_UMA_TRANSVERSAL_CONHECIDA")
    if not de_known or not ate_known:
        add("TRANSVERSAL_INEXISTENTE")
    if failure in {"SEM_INTERSECAO_DE", "SEM_INTERSECAO_ATE"}:
        add("AUSENCIA_DE_INTERSECAO")
    if failure == "SEM_CAMINHO":
        add("PROBLEMA_TOPOLOGICO")
    if "MARG" in text_blob:
        add("MARGINAIS")
    if any(token in text_blob for token in ("PISTA", "SENTIDO", "CRESCENTE", "DECRESCENTE", "IDA E VOLTA")):
        add("PISTAS_PARALELAS")
    if "AVENIDA" in text_blob and any(token in text_blob for token in ("PISTA", "TRECHO", "SENTIDO")):
        add("AVENIDA_DIVIDIDA")
    if "ROTATOR" in text_blob or "ROTATÓ" in text_blob:
        add("ROTATORIA")
    if "ALCA" in text_blob:
        add("ALCA")
    if "ACESSO" in text_blob:
        add("ACESSO")

    match_score = selected.main_match_score if selected else None
    reference_distance = selected.main_reference_distance_m if selected else None
    if match_score is not None and match_score < 92.0:
        add("NOME_INCOMPLETO")
    if any(token in text_blob.split() for token in ("R.", "AV.", "AL.", "ESTR.", "ROD.")):
        add("NOME_ABREVIADO")
    if reference_distance is not None and reference_distance > 30.0:
        add("COORDENADA_DISTANTE")
    if raw.get("latitude") in (None, "") or raw.get("longitude") in (None, ""):
        add("AUSENCIA_DE_GPS")
    deviation = _float(selected.deviation_pct) if selected else None
    if deviation is not None and deviation > 20.0:
        add("EXTENSAO_INCOMPATIVEL")
    if selected is not None and (selected.component_count > 1 or selected.component_status not in {"SAME_COMPONENT", "COMPONENT_SELECTED_BY_GPS"}):
        add("COMPONENTE_DESCONECTADO")
    if selected is not None and (selected.loop_detected or (selected.max_gap_m is not None and selected.max_gap_m > 0.01)):
        add("PROBLEMA_TOPOLOGICO")
    if result.candidate_count > 1 or (selected is not None and selected.segment_count > 1):
        add("MULTIPLOS_SEGMENTOS_POSSIVEIS")
    if not result.recovered or selected is None or "não encontrada" in result.reason.casefold() or "indisponível" in result.reason.casefold():
        add("RUA_SEM_GEOMETRIA")
    cep_values = [_text(raw.get(name)) for name in ("cep", "cep_de", "cep_ate", "cep_logradouro") if _text(raw.get(name))]
    if len(set(cep_values)) > 1:
        add("CEP_DIVERGENTE")
    if not causes:
        add("LIMITACAO_DE_EVIDENCIA")
    return causes


def _quality_status(result: Mapping[str, Any] | None) -> str:
    return _text((result or {}).get("geometry_confidence")) or "UNRESOLVED"


def _quality_strategy_summary(rows: list[dict[str, Any]], before_by_id: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, int]]:
    summary: dict[str, dict[str, int]] = {}
    for row in rows:
        strategy = _text(row.get("strategy_selected")) or "UNRESOLVED"
        before = _quality_status(before_by_id.get(_text(row.get("id"))))
        after = _quality_status(row)
        item = summary.setdefault(strategy, {
            "cases": 0, "resolved": 0, "continued_estimated": 0,
            "passed_high": 0, "passed_medium": 0, "remained_unresolved": 0,
            "newly_recovered_from_unresolved": 0, "promoted_from_estimated": 0,
            "after_high": 0, "after_medium": 0, "after_estimated": 0, "after_unresolved": 0,
        })
        item["cases"] += 1
        if after != "UNRESOLVED":
            item["resolved"] += 1
        if after == "ESTIMATED":
            item["continued_estimated"] += 1
            item["after_estimated"] += 1
        if after == "RECONSTRUCTED_HIGH":
            item["after_high"] += 1
        if after == "RECONSTRUCTED_MEDIUM":
            item["after_medium"] += 1
        if before == "UNRESOLVED" and after == "RECONSTRUCTED_HIGH":
            item["passed_high"] += 1
        if before == "UNRESOLVED" and after == "RECONSTRUCTED_MEDIUM":
            item["passed_medium"] += 1
        if after == "UNRESOLVED":
            item["remained_unresolved"] += 1
            item["after_unresolved"] += 1
        if before == "UNRESOLVED" and after != "UNRESOLVED":
            item["newly_recovered_from_unresolved"] += 1
        if before == "ESTIMATED" and after in {"RECONSTRUCTED_HIGH", "RECONSTRUCTED_MEDIUM"}:
            item["promoted_from_estimated"] += 1
    return dict(sorted(summary.items(), key=lambda item: (-item[1]["newly_recovered_from_unresolved"], -item[1]["resolved"], item[0])))


def run_quality_shadow_audit(
    clean_path: Path | str = DEFAULT_CLEAN_PATH,
    baseline_audit_path: Path | str = DEFAULT_AUDIT_PATH,
    review_path: Path | str = DEFAULT_HUMAN_REVIEW_PATH,
    output_path: Path | str = DEFAULT_QUALITY_AUDIT_PATH,
    report_path: Path | str = DEFAULT_QUALITY_REPORT_PATH,
    review_output_path: Path | str = DEFAULT_QUALITY_REVIEW_PATH,
    checkpoint_path: Path | str = DEFAULT_QUALITY_CHECKPOINT_PATH,
    resume: bool = False,
    reset_cache: bool = False,
    sample: int | None = None,
) -> dict[str, Any]:
    """Executa a segunda etapa somente para ESTIMATED/UNRESOLVED.

    Todos os caminhos de saída são artefatos novos da camada shadow. O CSV limpo,
    o ETL, o dashboard e os artefatos oficiais são somente fontes de leitura.
    """
    started = time.perf_counter()
    tracemalloc.start()
    clean_path, baseline_audit_path, output_path, report_path, review_output_path, checkpoint_path = map(
        Path, (clean_path, baseline_audit_path, output_path, report_path, review_output_path, checkpoint_path)
    )
    raw = load_recape()
    baseline = _baseline_index(clean_path)
    if not baseline_audit_path.exists():
        raise RuntimeError(f"Auditoria anterior não encontrada: {baseline_audit_path}")
    before_frame = pd.read_csv(baseline_audit_path, encoding="utf-8-sig", dtype=str)
    before_by_id = {_text(row.get("id")): row.to_dict() for _, row in before_frame.iterrows() if _text(row.get("id"))}
    target_ids = {
        identifier for identifier, item in before_by_id.items()
        if _text(item.get("geometry_confidence")) in {"ESTIMATED", "UNRESOLVED"}
    }
    targets = []
    for _, row in raw.iterrows():
        identifier = _text(row.get("id"))
        if identifier in target_ids:
            targets.append((row.to_dict(), baseline.get(identifier, {})))
    targets.sort(key=lambda item: _text(item[0].get("id")))
    if sample is not None:
        targets = targets[: max(0, sample)]

    graph_path = Path(CACHE_DIR) / "geosampa_road_graph.pkl"
    graph = _load_shadow_graph(graph_path, Path(GEOSAMPA_SEGMENTOS))
    overrides = load_human_review_overrides(graph, normalizar_rua, review_path=review_path)
    engine = GeometryQualityShadowEngine(graph, normalizar_rua, overrides)
    signature = {
        "version": QUALITY_VERSION,
        "sources": _source_signature((clean_path, baseline_audit_path, Path(review_path), graph_path, Path(GEOSAMPA_SEGMENTOS))),
        "target_count": len(targets),
    }
    completed: dict[str, dict[str, Any]] = {}
    if reset_cache and checkpoint_path.exists():
        checkpoint_path.unlink()
    if resume and checkpoint_path.exists():
        try:
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            if checkpoint.get("signature") == signature:
                completed = checkpoint.get("results", {})
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            completed = {}

    # Repara checkpoints v2.4 que foram gerados antes da correção do mapeamento
    # ``strategy_selected``/``geometry_confidence``. A reparação é determinística
    # e usa somente a linha de base, sem recalcular geometrias.
    raw_by_id = {_text(row.get("id")): row.to_dict() for _, row in raw.iterrows()}
    for identifier, record in list(completed.items()):
        if (
            _text(record.get("before_geometry_confidence")) == "ESTIMATED"
            and _text(record.get("geometry_confidence")) == "UNRESOLVED"
            and identifier in before_by_id
            and identifier in raw_by_id
        ):
            repaired = engine._baseline_result(raw_by_id[identifier], baseline.get(identifier, {}), before_by_id[identifier])
            repaired_record = _result_row(raw_by_id[identifier], baseline.get(identifier, {}), repaired)
            causes = _quality_root_causes(raw_by_id[identifier], baseline.get(identifier, {}), repaired)
            repaired_record.update({
                "before_geometry_confidence": "ESTIMATED",
                "before_strategy": _text(before_by_id[identifier].get("strategy_selected")) or "UNRESOLVED",
                "root_cause_primary": causes[0],
                "root_causes": " | ".join(causes),
                "shadow_version": QUALITY_VERSION,
            })
            completed[identifier] = repaired_record

    records: list[dict[str, Any]] = []
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    for position, (row, current) in enumerate(targets, 1):
        identifier = _text(row.get("id"))
        if identifier in completed:
            records.append(completed[identifier])
            continue
        recovery = engine.recover(row, current, baseline_audit=before_by_id.get(identifier))
        record = _result_row(row, current, recovery)
        before = before_by_id.get(identifier, {})
        causes = _quality_root_causes(row, current, recovery)
        record.update({
            "before_geometry_confidence": _text(before.get("geometry_confidence")) or "UNRESOLVED",
            "before_strategy": _text(before.get("strategy_selected")) or "UNRESOLVED",
            "root_cause_primary": causes[0],
            "root_causes": " | ".join(causes),
            "shadow_version": QUALITY_VERSION,
        })
        completed[identifier] = record
        records.append(record)
        if position % 250 == 0 or position == len(targets):
            checkpoint_path.write_text(json.dumps({"signature": signature, "results": completed, "updated_at": time.time()}, ensure_ascii=False, default=_json_default), encoding="utf-8")
    records.sort(key=lambda item: _text(item.get("id")))
    shadow_frame = pd.DataFrame(records)
    _atomic_write_csv(shadow_frame, output_path)
    review_frame = shadow_frame[shadow_frame.apply(_review_row, axis=1)].copy() if not shadow_frame.empty else shadow_frame.copy()
    for column in ("decision", "manual_strategy", "review_notes", "approved", "reviewed_at", "reviewed_by"):
        review_frame[column] = pd.NA
    _atomic_write_csv(review_frame, review_output_path)

    official_count = sum(_valid_path(value.get("path")) for value in baseline.values())
    total = len(raw)
    before_confidence = Counter(_text(row.get("geometry_confidence")) or "UNRESOLVED" for row in before_by_id.values())
    after_confidence = Counter(before_confidence)
    for record in records:
        old = _text(record.get("before_geometry_confidence")) or "UNRESOLVED"
        new = _text(record.get("geometry_confidence")) or "UNRESOLVED"
        after_confidence[old] -= 1
        after_confidence[new] += 1
    old_projected_cases = sum(before_confidence.get(value, 0) for value in ("CONFIRMED", "RECONSTRUCTED_HIGH", "RECONSTRUCTED_MEDIUM", "ESTIMATED"))
    new_projected_cases = sum(after_confidence.get(value, 0) for value in ("CONFIRMED", "RECONSTRUCTED_HIGH", "RECONSTRUCTED_MEDIUM", "ESTIMATED"))
    old_projected_pct = (official_count + old_projected_cases) / total * 100 if total else 0.0
    new_projected_pct = (official_count + new_projected_cases) / total * 100 if total else 0.0
    absolute_gain = new_projected_pct - old_projected_pct
    relative_gain = absolute_gain / old_projected_pct * 100 if old_projected_pct else 0.0

    transitions = Counter()
    for record in records:
        transitions[f"{_text(record.get('before_geometry_confidence')) or 'UNRESOLVED'}->{_text(record.get('geometry_confidence')) or 'UNRESOLVED'}"] += 1
    root_stats: dict[str, dict[str, int]] = defaultdict(lambda: {
        "cases": 0, "before_estimated": 0, "before_unresolved": 0,
        "after_high": 0, "after_medium": 0, "after_estimated": 0,
        "after_unresolved": 0, "recovered": 0,
    })
    for record in records:
        before_status = _text(record.get("before_geometry_confidence")) or "UNRESOLVED"
        after_status = _text(record.get("geometry_confidence")) or "UNRESOLVED"
        for cause in filter(None, (_text(value) for value in _text(record.get("root_causes")).split("|"))):
            item = root_stats[cause]
            item["cases"] += 1
            item["before_estimated"] += before_status == "ESTIMATED"
            item["before_unresolved"] += before_status == "UNRESOLVED"
            item["after_high"] += after_status == "RECONSTRUCTED_HIGH"
            item["after_medium"] += after_status == "RECONSTRUCTED_MEDIUM"
            item["after_estimated"] += after_status == "ESTIMATED"
            item["after_unresolved"] += after_status == "UNRESOLVED"
            item["recovered"] += after_status != "UNRESOLVED"
    root_stats = dict(sorted(root_stats.items(), key=lambda item: (-item[1]["before_unresolved"], -item[1]["cases"], item[0])))
    strategy_summary = _quality_strategy_summary(records, before_by_id)
    strategy_ranking = [
        {"strategy": strategy, **summary}
        for strategy, summary in strategy_summary.items()
    ]
    remaining = int(after_confidence.get("UNRESOLVED", 0))
    structural_limits = []
    if new_projected_pct < 98.0:
        unresolved_records = [record for record in records if _text(record.get("geometry_confidence")) == "UNRESOLVED"]
        unresolved_by_reason = Counter(_text(record.get("reason")) or "sem_motivo" for record in unresolved_records)
        structural_limits.append({
            "limit": "CASOS_AINDA_UNRESOLVED",
            "cases": remaining,
            "reasons": dict(unresolved_by_reason),
            "technical_solution": "resolver ou cadastrar a via principal/transversal ausente no GeoSampa, ou fornecer ponto/extensão confiáveis; uma geometria não pode ser inferida com segurança sem esse vínculo.",
        })
    report = {
        "version": QUALITY_VERSION,
        "mode": "shadow_diagnostic_only",
        "scope": {"total_recapes": total, "audited_estimated_or_unresolved": len(records), "official_geometry_count": official_count},
        "before": {
            "confidence": {str(key): int(value) for key, value in sorted(before_confidence.items()) if value},
            "projected_coverage_with_estimated_pct": round(old_projected_pct, 6),
            "projected_coverage_cases": int(official_count + old_projected_cases),
        },
        "after": {
            "confidence": {str(key): int(value) for key, value in sorted(after_confidence.items()) if value},
            "projected_coverage_with_estimated_pct": round(new_projected_pct, 6),
            "projected_coverage_cases": int(official_count + new_projected_cases),
        },
        "gain": {
            "absolute_cases": int(new_projected_cases - old_projected_cases),
            "absolute_percentage_points": round(absolute_gain, 6),
            "relative_percent": round(relative_gain, 6),
        },
        "transitions": {str(key): int(value) for key, value in sorted(transitions.items())},
        "root_causes": root_stats,
        "strategies": strategy_summary,
        "strategy_ranking": strategy_ranking,
        "remaining_unresolved": remaining,
        "structural_limits": structural_limits,
        "timings": {"elapsed_seconds": round(time.perf_counter() - started, 3)},
        "cache": {"graph_loaded": True, "shadow_spatial_cache": len(engine.shadow_spatial_cache), "projection_cache": engine.cache_hits["projection"], "checkpoint": str(checkpoint_path), "resumed": bool(resume and completed)},
        "memory": {"peak_tracemalloc_mb": round(tracemalloc.get_traced_memory()[1] / 1024 / 1024, 2)},
        "artifacts": {"audit": str(output_path), "review": str(review_output_path), "report": str(report_path)},
    }
    tracemalloc.stop()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = report_path.with_suffix(report_path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    os.replace(temporary, report_path)
    return report


def _same_transversal_prefilter(row: Mapping[str, Any], before: Mapping[str, Any], normalizer=normalizar_rua) -> bool:
    """Pré-filtro barato: só valores equivalentes, nunca similaridade fuzzy."""
    de_values = [
        _text(row.get("de")), _text(before.get("de")), _text(before.get("de_resolved")), _text(before.get("de_candidate")),
    ]
    ate_values = [
        _text(row.get("ate")), _text(before.get("ate")), _text(before.get("ate_resolved")), _text(before.get("ate_candidate")),
    ]
    de_values = [normalizer(value) for value in de_values if value]
    ate_values = [normalizer(value) for value in ate_values if value]
    return bool(de_values and ate_values and any(de == ate for de in de_values for ate in ate_values if de))


def _confidence_rank(value: Any) -> int:
    return {"UNRESOLVED": 0, "ESTIMATED": 1, "RECONSTRUCTED_MEDIUM": 2, "RECONSTRUCTED_HIGH": 3, "CONFIRMED": 4}.get(_text(value), 0)


def _same_transversal_result_row(
    raw: Mapping[str, Any],
    before_audit: Mapping[str, Any],
    current_shadow: Mapping[str, Any],
    analysis: Mapping[str, Any],
) -> dict[str, Any]:
    candidates = list(analysis.get("candidates", []))
    proposed = candidates[0] if candidates else None
    before_confidence = _text(current_shadow.get("geometry_confidence")) or _text(before_audit.get("geometry_confidence")) or "UNRESOLVED"
    before_strategy = _text(current_shadow.get("strategy_selected")) or _text(before_audit.get("strategy_selected")) or "UNRESOLVED"
    before_wkt = _text(current_shadow.get("geometry_wkt")) or _text(before_audit.get("geometry_wkt"))
    before_length = _float(current_shadow.get("path_length_m")) or _float(before_audit.get("path_length_m"))
    better = bool(proposed is not None and (_confidence_rank(proposed.confidence) > _confidence_rank(before_confidence) or (_confidence_rank(proposed.confidence) == _confidence_rank(before_confidence) and proposed.score >= (_float(current_shadow.get("geometry_score")) or _float(before_audit.get("geometry_score")) or 0.0) + 1.0)))
    after_confidence = proposed.confidence if better and proposed is not None else before_confidence
    after_strategy = proposed.strategy if better and proposed is not None else before_strategy
    after_wkt = proposed.geometry_wkt if better and proposed is not None else before_wkt
    after_length = proposed.length_m if better and proposed is not None else before_length
    after_deviation = proposed.deviation_pct if better and proposed is not None else _float(current_shadow.get("extension_deviation_pct"))
    after_score = proposed.score if better and proposed is not None else (_float(current_shadow.get("geometry_score")) or _float(before_audit.get("geometry_score")))
    promoted = better and _confidence_rank(after_confidence) > _confidence_rank(before_confidence) and after_confidence in {"RECONSTRUCTED_HIGH", "RECONSTRUCTED_MEDIUM"}
    points = analysis.get("intersection_points", [])
    alternatives = [candidate.as_dict() for candidate in candidates[1:]]
    return {
        "id": _text(raw.get("id")), "via": _text(raw.get("via") or raw.get("rua_raw")),
        "via_resolvida": _text(raw.get("rua_norm")) or _text(before_audit.get("via_resolvida")) or normalizar_rua(_text(raw.get("via") or raw.get("rua_raw"))),
        "de": _text(raw.get("de")), "ate": _text(raw.get("ate")),
        "transversal_resolvida": _text(analysis.get("transversal_resolvida")),
        "latitude": raw.get("latitude"), "longitude": raw.get("longitude"), "extensao_m": raw.get("extensao_m"),
        "intersection_count_raw": analysis.get("intersection_count_raw", 0),
        "intersection_count_distinct": analysis.get("intersection_count_distinct", 0),
        "intersection_points_json": json.dumps(points, ensure_ascii=False, default=_json_default),
        "component_count": analysis.get("component_count", 0),
        "candidate_pair_count": analysis.get("candidate_pair_count", 0),
        "selected_pair_index": analysis.get("selected_pair_index"),
        "selected_start_point": json.dumps(analysis.get("selected_start_point"), ensure_ascii=False),
        "selected_end_point": json.dumps(analysis.get("selected_end_point"), ensure_ascii=False),
        "before_confidence": before_confidence, "before_strategy": before_strategy, "before_length_m": before_length,
        "after_confidence": after_confidence, "after_strategy": after_strategy, "after_length_m": after_length,
        "extension_deviation_pct": after_deviation, "distance_to_gps_m": analysis.get("distance_to_gps_m"),
        "score": after_score, "margin_top2": analysis.get("margin_top2"), "promoted": promoted,
        "requires_review": bool(analysis.get("ambiguous", False) or after_confidence != "RECONSTRUCTED_HIGH"),
        "reason": analysis.get("reason"),
        "warnings": " | ".join(sorted({warning for candidate in candidates for warning in candidate.warnings})),
        "alternatives_json": json.dumps(alternatives, ensure_ascii=False, default=_json_default),
        "geometry_wkt": after_wkt, "geometry_geojson": proposed.geometry_geojson if better and proposed is not None else _text(current_shadow.get("geometry_geojson")),
        "same_transversal_ambiguous": bool(analysis.get("ambiguous", False)),
        "same_transversal_invalid": analysis.get("invalid", 0),
        "main_geometry_wkt": analysis.get("main_geometry_wkt"),
        "transversal_geometry_wkt": analysis.get("transversal_geometry_wkt"),
    }


def _same_transversal_summary(records: list[Mapping[str, Any]], all_quality: pd.DataFrame | None = None) -> dict[str, Any]:
    frame = pd.DataFrame(records)
    if frame.empty:
        return {
            "same_transversal_cases": 0, "same_transversal_with_two_intersections": 0,
            "same_transversal_with_multiple_intersections": 0, "same_transversal_with_two_or_more_intersections": 0,
            "same_transversal_promoted_high": 0,
            "same_transversal_promoted_medium": 0, "same_transversal_kept_estimated": 0,
            "same_transversal_ambiguous": 0, "same_transversal_invalid": 0,
            "same_transversal_false_positive": 0, "same_transversal_valid_pair_cases": 0,
            "same_transversal_average_score": None, "same_transversal_average_extension_deviation": None,
            "same_transversal_strategies": {}, "same_transversal_strategy_details": {},
            "same_transversal_strategy_ranking": [],
        }
    distinct = pd.to_numeric(frame["intersection_count_distinct"], errors="coerce").fillna(0)
    after = frame["after_confidence"].fillna("UNRESOLVED")
    scores = pd.to_numeric(frame["score"], errors="coerce").dropna()
    deviations = pd.to_numeric(frame["extension_deviation_pct"], errors="coerce").dropna()
    strategies = frame["after_strategy"].fillna("UNRESOLVED").value_counts().to_dict()
    promotions = frame[frame["promoted"].map(_bool)]
    false_positive = (distinct < 2) | (pd.to_numeric(frame["candidate_pair_count"], errors="coerce").fillna(0) == 0)
    strategy_details: dict[str, dict[str, int]] = {}
    for strategy, group in frame.groupby(frame["after_strategy"].fillna("UNRESOLVED"), dropna=False):
        after_values = group["after_confidence"].fillna("UNRESOLVED")
        strategy_details[str(strategy)] = {
            "cases": int(len(group)),
            "resolved": int((~after_values.isin({"ESTIMATED", "UNRESOLVED"})).sum()),
            "continued_estimated": int((after_values == "ESTIMATED").sum()),
            "reconstructed_high": int((after_values == "RECONSTRUCTED_HIGH").sum()),
            "reconstructed_medium": int((after_values == "RECONSTRUCTED_MEDIUM").sum()),
            "unresolved": int((after_values == "UNRESOLVED").sum()),
        }
    return {
        "same_transversal_cases": int(len(frame)),
        "same_transversal_with_two_intersections": int((distinct == 2).sum()),
        "same_transversal_with_multiple_intersections": int((distinct > 2).sum()),
        "same_transversal_with_two_or_more_intersections": int((distinct >= 2).sum()),
        "same_transversal_promoted_high": int((promotions["after_confidence"] == "RECONSTRUCTED_HIGH").sum()),
        "same_transversal_promoted_medium": int((promotions["after_confidence"] == "RECONSTRUCTED_MEDIUM").sum()),
        "same_transversal_kept_estimated": int((after == "ESTIMATED").sum()),
        "same_transversal_ambiguous": int(frame["same_transversal_ambiguous"].map(_bool).sum()),
        "same_transversal_invalid": int(pd.to_numeric(frame["same_transversal_invalid"], errors="coerce").fillna(0).sum()),
        "same_transversal_false_positive": int(false_positive.sum()),
        "same_transversal_valid_pair_cases": int((~false_positive).sum()),
        "same_transversal_average_score": round(float(scores.mean()), 4) if not scores.empty else None,
        "same_transversal_average_extension_deviation": round(float(deviations.mean()), 4) if not deviations.empty else None,
        "same_transversal_strategies": {str(key): int(value) for key, value in sorted(strategies.items(), key=lambda item: (-item[1], item[0]))},
        "same_transversal_strategy_details": dict(sorted(strategy_details.items(), key=lambda item: (-item[1]["resolved"], -item[1]["cases"], item[0]))),
        "same_transversal_strategy_ranking": [
            {"strategy": strategy, **detail}
            for strategy, detail in sorted(strategy_details.items(), key=lambda item: (-item[1]["resolved"], -item[1]["reconstructed_high"], -item[1]["reconstructed_medium"], -item[1]["cases"], item[0]))
        ],
    }


def run_same_transversal_audit(
    clean_path: Path | str = DEFAULT_CLEAN_PATH,
    baseline_audit_path: Path | str = DEFAULT_AUDIT_PATH,
    quality_shadow_path: Path | str = DEFAULT_QUALITY_AUDIT_PATH,
    output_path: Path | str = DEFAULT_SAME_TRANSVERSAL_AUDIT_PATH,
    report_path: Path | str = DEFAULT_SAME_TRANSVERSAL_REPORT_PATH,
    checkpoint_path: Path | str = DEFAULT_SAME_TRANSVERSAL_CHECKPOINT_PATH,
    sample: int | None = None,
    resume: bool = False,
    reset_cache: bool = False,
) -> dict[str, Any]:
    """Executa somente a categoria De=Até e grava artefatos diagnósticos novos."""
    started = time.perf_counter()
    clean_path, baseline_audit_path, quality_shadow_path, output_path, report_path, checkpoint_path = map(Path, (clean_path, baseline_audit_path, quality_shadow_path, output_path, report_path, checkpoint_path))
    raw = load_recape()
    baseline = _baseline_index(clean_path)
    before_frame = pd.read_csv(baseline_audit_path, encoding="utf-8-sig", dtype=str)
    before_by_id = {_text(row.get("id")): row.to_dict() for _, row in before_frame.iterrows() if _text(row.get("id"))}
    shadow_by_id: dict[str, dict[str, Any]] = {}
    if quality_shadow_path.exists():
        quality_frame = pd.read_csv(quality_shadow_path, encoding="utf-8-sig", dtype=str)
        shadow_by_id = {_text(row.get("id")): row.to_dict() for _, row in quality_frame.iterrows() if _text(row.get("id"))}
    prefiltered = []
    for _, row in raw.iterrows():
        identifier = _text(row.get("id"))
        before = before_by_id.get(identifier, {})
        if _text(before.get("geometry_confidence")) not in {"ESTIMATED", "UNRESOLVED"}:
            continue
        if _same_transversal_prefilter(row, before):
            prefiltered.append((row.to_dict(), before, shadow_by_id.get(identifier, {})))
    prefiltered.sort(key=lambda item: _text(item[0].get("id")))
    if sample is not None:
        prefiltered = prefiltered[: max(0, int(sample))]
    graph_path = Path(CACHE_DIR) / "geosampa_road_graph.pkl"
    graph = _load_shadow_graph(graph_path, Path(GEOSAMPA_SEGMENTOS))
    engine = GeometryQualityShadowEngine(graph, normalizar_rua)
    signature = {"version": QUALITY_VERSION, "sources": _source_signature((clean_path, baseline_audit_path, quality_shadow_path, graph_path, Path(GEOSAMPA_SEGMENTOS))), "target_count": len(prefiltered)}
    completed: dict[str, dict[str, Any]] = {}
    if reset_cache and checkpoint_path.exists():
        checkpoint_path.unlink()
    if resume and checkpoint_path.exists():
        try:
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            if checkpoint.get("signature") == signature:
                completed = checkpoint.get("results", {})
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            completed = {}
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    for position, (row, before, current_shadow) in enumerate(prefiltered, 1):
        identifier = _text(row.get("id"))
        if identifier in completed:
            records.append(completed[identifier])
            continue
        analysis = engine.same_transversal_analysis(row, before)
        record = _same_transversal_result_row(row, before, current_shadow, analysis)
        completed[identifier] = record
        records.append(record)
        if position % 25 == 0 or position == len(prefiltered):
            checkpoint_path.write_text(json.dumps({"signature": signature, "results": completed, "updated_at": time.time()}, ensure_ascii=False, default=_json_default), encoding="utf-8")
    records.sort(key=lambda item: _text(item.get("id")))
    _atomic_write_csv(pd.DataFrame(records), output_path)
    summary = _same_transversal_summary(records)
    total_cases = len(raw)
    official_count = max(0, total_cases - len(before_by_id))
    original_before = Counter(_text(item.get("geometry_confidence")) or "UNRESOLVED" for item in before_by_id.values())
    combined_before: Counter[str] = Counter()
    for identifier, item in before_by_id.items():
        status = _text(shadow_by_id.get(identifier, {}).get("geometry_confidence")) or _text(item.get("geometry_confidence")) or "UNRESOLVED"
        combined_before[status] += 1
    combined_after = Counter(combined_before)
    for record in records:
        if not _bool(record.get("promoted")):
            continue
        identifier = _text(record.get("id"))
        old = _text(shadow_by_id.get(identifier, {}).get("geometry_confidence")) or _text(before_by_id.get(identifier, {}).get("geometry_confidence")) or "UNRESOLVED"
        new = _text(record.get("after_confidence")) or old
        combined_after[old] -= 1
        combined_after[new] += 1
    covered_classes = {"CONFIRMED", "RECONSTRUCTED_HIGH", "RECONSTRUCTED_MEDIUM", "ESTIMATED"}
    original_covered = official_count + sum(original_before.get(value, 0) for value in covered_classes)
    same_before_covered = official_count + sum(combined_before.get(value, 0) for value in covered_classes)
    same_after_covered = official_count + sum(combined_after.get(value, 0) for value in covered_classes)
    original_before_pct = original_covered / total_cases * 100 if total_cases else 0.0
    same_before_pct = same_before_covered / total_cases * 100 if total_cases else 0.0
    same_after_pct = same_after_covered / total_cases * 100 if total_cases else 0.0
    coverage = {
        "original_before_quality_shadow": {
            "confidence": {str(key): int(value) for key, value in sorted(original_before.items()) if value},
            "projected_coverage_with_estimated_pct": round(original_before_pct, 6),
            "projected_coverage_cases": int(original_covered),
        },
        "before_same_transversal": {
            "confidence": {str(key): int(value) for key, value in sorted(combined_before.items()) if value},
            "projected_coverage_with_estimated_pct": round(same_before_pct, 6),
            "projected_coverage_cases": int(same_before_covered),
        },
        "after_same_transversal": {
            "confidence": {str(key): int(value) for key, value in sorted(combined_after.items()) if value},
            "projected_coverage_with_estimated_pct": round(same_after_pct, 6),
            "projected_coverage_cases": int(same_after_covered),
        },
        "gain": {
            "absolute_cases": int(same_after_covered - same_before_covered),
            "absolute_percentage_points": round(same_after_pct - same_before_pct, 6),
            "relative_percent": round((same_after_pct - same_before_pct) / same_before_pct * 100, 6) if same_before_pct else 0.0,
        },
    }
    reconstructed_classes = {"CONFIRMED", "RECONSTRUCTED_HIGH", "RECONSTRUCTED_MEDIUM"}
    for snapshot, counts in (
        (coverage["original_before_quality_shadow"], original_before),
        (coverage["before_same_transversal"], combined_before),
        (coverage["after_same_transversal"], combined_after),
    ):
        reconstructed_cases = official_count + sum(counts.get(value, 0) for value in reconstructed_classes)
        snapshot["official_coverage_pct"] = round(official_count / total_cases * 100, 6) if total_cases else 0.0
        snapshot["projected_coverage_with_reconstructed_pct"] = round(reconstructed_cases / total_cases * 100, 6) if total_cases else 0.0
    before_reconstructed = coverage["before_same_transversal"]["projected_coverage_with_reconstructed_pct"]
    after_reconstructed = coverage["after_same_transversal"]["projected_coverage_with_reconstructed_pct"]
    coverage["gain"]["reconstructed_percentage_points"] = round(after_reconstructed - before_reconstructed, 6)
    overall_before: dict[str, int] = {}
    overall_after: dict[str, int] = {}
    if shadow_by_id:
        for item in shadow_by_id.values():
            confidence = _text(item.get("geometry_confidence")) or "UNRESOLVED"
            overall_before[confidence] = overall_before.get(confidence, 0) + 1
        overall_after = dict(overall_before)
        for record in records:
            identifier = _text(record.get("id"))
            old = _text(shadow_by_id.get(identifier, {}).get("geometry_confidence")) or "UNRESOLVED"
            new = _text(record.get("after_confidence")) or old
            if _bool(record.get("promoted")):
                overall_after[old] = overall_after.get(old, 0) - 1
                overall_after[new] = overall_after.get(new, 0) + 1
    report = {
        "version": "route-geometry-same-transversal-v1", "mode": "shadow_diagnostic_only",
        "prefiltered_cases": len(prefiltered), "processed_cases": len(records), **summary,
        "coverage": coverage,
        "overall_quality_shadow_before": overall_before, "overall_quality_shadow_after": overall_after,
        "timings": {"elapsed_seconds": round(time.perf_counter() - started, 3)},
        "cache": {"graph_loaded": True, "checkpoint": str(checkpoint_path), "resumed": bool(resume and completed)},
        "artifacts": {"audit": str(output_path), "report": str(report_path)},
        "official_application": False, "official_outputs_modified": False,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = report_path.with_suffix(report_path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    os.replace(temporary, report_path)
    return report


def repair_quality_shadow_outputs(
    clean_path: Path | str = DEFAULT_CLEAN_PATH,
    baseline_audit_path: Path | str = DEFAULT_AUDIT_PATH,
    output_path: Path | str = DEFAULT_QUALITY_AUDIT_PATH,
    report_path: Path | str = DEFAULT_QUALITY_REPORT_PATH,
    review_output_path: Path | str = DEFAULT_QUALITY_REVIEW_PATH,
) -> dict[str, Any]:
    """Corrige o artefato completo sem repetir cálculo geométrico.

    É usado quando a execução integral já terminou, mas uma versão anterior do
    desserializador rebaixou candidatos antigos. A correção só copia a decisão
    selecionada da auditoria anterior para os casos ``ESTIMATED -> UNRESOLVED``;
    não inventa uma geometria nova.
    """
    clean_path, baseline_audit_path, output_path, report_path, review_output_path = map(
        Path, (clean_path, baseline_audit_path, output_path, report_path, review_output_path)
    )
    raw = load_recape()
    baseline = _baseline_index(clean_path)
    before_frame = pd.read_csv(baseline_audit_path, encoding="utf-8-sig", dtype=str)
    before_by_id = {_text(row.get("id")): row.to_dict() for _, row in before_frame.iterrows() if _text(row.get("id"))}
    shadow_frame = pd.read_csv(output_path, encoding="utf-8-sig", dtype=str)
    records = [row.to_dict() for _, row in shadow_frame.iterrows()]
    raw_by_id = {_text(row.get("id")): row.to_dict() for _, row in raw.iterrows()}
    repaired_count = 0
    helper = GeometryQualityShadowEngine.__new__(GeometryQualityShadowEngine)
    for record in records:
        identifier = _text(record.get("id"))
        before = before_by_id.get(identifier, {})
        if _text(before.get("geometry_confidence")) != "ESTIMATED" or _text(record.get("geometry_confidence")) != "UNRESOLVED":
            continue
        old = before
        selected = helper._candidate_from_payload(old)
        if selected is None:
            continue
        selected = replace(
            selected,
            strategy=_text(old.get("strategy_selected")) or selected.strategy,
            confidence="ESTIMATED",
            score=_float(old.get("geometry_score")) or selected.score,
            length_m=_float(old.get("path_length_m")) or selected.length_m,
            deviation_pct=_float(old.get("extension_deviation_pct")) if _float(old.get("extension_deviation_pct")) is not None else selected.deviation_pct,
            main_street=_text(old.get("via_resolvida")) or selected.main_street,
        )
        for new_name, old_name in {
            "strategy_selected": "strategy_selected", "geometry_confidence": "geometry_confidence",
            "geometry_score": "geometry_score", "candidate_count": "candidate_count", "de_resolved": "de_resolved",
            "ate_resolved": "ate_resolved", "de_status": "de_status", "ate_status": "ate_status",
            "topology_status": "topology_status", "component_status": "component_status", "snap_used": "snap_used",
            "snap_distance_de_m": "snap_distance_de_m", "snap_distance_ate_m": "snap_distance_ate_m",
            "path_length_m": "path_length_m", "extension_deviation_pct": "extension_deviation_pct",
            "segment_count": "segment_count", "component_count": "component_count", "max_gap_m": "max_gap_m",
            "loop_detected": "loop_detected", "geometry_wkt": "geometry_wkt", "geometry_geojson": "geometry_geojson",
            "reason": "reason", "warnings": "warnings", "alternatives_json": "alternatives_json",
        }.items():
            record[new_name] = old.get(old_name)
        record.update({
            "recovered": "True", "requires_review": "True", "main_street": selected.main_street,
            "main_match_score": selected.main_match_score, "main_reference_distance_m": selected.main_reference_distance_m,
        })
        result = GeometryRecoveryResult(identifier, _text(baseline.get(identifier, {}).get("status_path")) or "SEM_GEOMETRIA", True, selected.strategy, "ESTIMATED", int(_float(old.get("candidate_count")) or 1), selected, [], True, _text(old.get("reason")) or "linha de base preservada")
        causes = _quality_root_causes(raw_by_id.get(identifier, {}), baseline.get(identifier, {}), result)
        record.update({"before_geometry_confidence": "ESTIMATED", "before_strategy": _text(old.get("strategy_selected")) or "UNRESOLVED", "root_cause_primary": causes[0], "root_causes": " | ".join(causes), "shadow_version": QUALITY_VERSION})
        repaired_count += 1

    official_count = sum(_valid_path(value.get("path")) for value in baseline.values())
    total = len(raw)
    before_confidence = Counter(_text(row.get("geometry_confidence")) or "UNRESOLVED" for row in before_by_id.values())
    after_confidence = Counter(before_confidence)
    for record in records:
        old = _text(record.get("before_geometry_confidence")) or "UNRESOLVED"
        new = _text(record.get("geometry_confidence")) or "UNRESOLVED"
        after_confidence[old] -= 1
        after_confidence[new] += 1
    covered = {"CONFIRMED", "RECONSTRUCTED_HIGH", "RECONSTRUCTED_MEDIUM", "ESTIMATED"}
    old_cases = sum(before_confidence.get(value, 0) for value in covered)
    new_cases = sum(after_confidence.get(value, 0) for value in covered)
    old_pct = (official_count + old_cases) / total * 100 if total else 0.0
    new_pct = (official_count + new_cases) / total * 100 if total else 0.0
    transitions = Counter(f"{_text(row.get('before_geometry_confidence')) or 'UNRESOLVED'}->{_text(row.get('geometry_confidence')) or 'UNRESOLVED'}" for row in records)
    root_stats: dict[str, dict[str, int]] = defaultdict(lambda: {"cases": 0, "before_estimated": 0, "before_unresolved": 0, "after_high": 0, "after_medium": 0, "after_estimated": 0, "after_unresolved": 0, "recovered": 0})
    for record in records:
        before_status = _text(record.get("before_geometry_confidence")) or "UNRESOLVED"
        after_status = _text(record.get("geometry_confidence")) or "UNRESOLVED"
        for cause in filter(None, (_text(value) for value in _text(record.get("root_causes")).split("|"))):
            item = root_stats[cause]
            item["cases"] += 1; item["before_estimated"] += before_status == "ESTIMATED"; item["before_unresolved"] += before_status == "UNRESOLVED"
            item["after_high"] += after_status == "RECONSTRUCTED_HIGH"; item["after_medium"] += after_status == "RECONSTRUCTED_MEDIUM"; item["after_estimated"] += after_status == "ESTIMATED"; item["after_unresolved"] += after_status == "UNRESOLVED"; item["recovered"] += after_status != "UNRESOLVED"
    root_stats = dict(sorted(root_stats.items(), key=lambda item: (-item[1]["before_unresolved"], -item[1]["cases"], item[0])))
    strategy_summary = _quality_strategy_summary(records, before_by_id)
    review_frame = shadow_frame.copy()
    for index, record in enumerate(records):
        if index < len(review_frame):
            for key, value in record.items():
                review_frame.at[index, key] = value
    review_frame = review_frame[review_frame.apply(_review_row, axis=1)].copy() if not review_frame.empty else review_frame.copy()
    for column in ("decision", "manual_strategy", "review_notes", "approved", "reviewed_at", "reviewed_by"):
        review_frame[column] = pd.NA
    shadow_frame = pd.DataFrame(records)
    _atomic_write_csv(shadow_frame, output_path); _atomic_write_csv(review_frame, review_output_path)
    report = {
        "version": QUALITY_VERSION, "mode": "shadow_diagnostic_only", "repair_applied": True, "repaired_records": repaired_count,
        "scope": {"total_recapes": total, "audited_estimated_or_unresolved": len(records), "official_geometry_count": official_count},
        "before": {"confidence": {str(k): int(v) for k, v in sorted(before_confidence.items()) if v}, "projected_coverage_with_estimated_pct": round(old_pct, 6), "projected_coverage_cases": int(official_count + old_cases)},
        "after": {"confidence": {str(k): int(v) for k, v in sorted(after_confidence.items()) if v}, "projected_coverage_with_estimated_pct": round(new_pct, 6), "projected_coverage_cases": int(official_count + new_cases)},
        "gain": {"absolute_cases": int(new_cases - old_cases), "absolute_percentage_points": round(new_pct - old_pct, 6), "relative_percent": round((new_pct - old_pct) / old_pct * 100, 6) if old_pct else 0.0},
        "transitions": {str(k): int(v) for k, v in sorted(transitions.items())}, "root_causes": root_stats, "strategies": strategy_summary,
        "strategy_ranking": [{"strategy": strategy, **summary} for strategy, summary in strategy_summary.items()],
        "remaining_unresolved": int(after_confidence.get("UNRESOLVED", 0)), "structural_limits": [],
        "artifacts": {"audit": str(output_path), "review": str(review_output_path), "report": str(report_path)},
    }
    report_path.parent.mkdir(parents=True, exist_ok=True); temporary = report_path.with_suffix(report_path.suffix + ".tmp"); temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8"); os.replace(temporary, report_path)
    return report


def run_audit(
    sample: int | None = None,
    resume: bool = False,
    reset_cache: bool = False,
    only_failure: str | None = None,
    clean_path: Path | str = DEFAULT_CLEAN_PATH,
    review_path: Path | str = DEFAULT_HUMAN_REVIEW_PATH,
    output_path: Path | str = DEFAULT_AUDIT_PATH,
    report_path: Path | str = DEFAULT_REPORT_PATH,
    review_output_path: Path | str = DEFAULT_REVIEW_PATH,
    checkpoint_path: Path | str = DEFAULT_CHECKPOINT_PATH,
) -> dict[str, Any]:
    started = time.perf_counter()
    tracemalloc.start()
    clean_path, output_path, report_path, review_output_path, checkpoint_path = map(Path, (clean_path, output_path, report_path, review_output_path, checkpoint_path))
    raw = load_recape()
    baseline = _baseline_index(clean_path)
    failed = []
    for _, row in raw.iterrows():
        current = baseline.get(_text(row.get("id")), {})
        if _valid_path(current.get("path")):
            continue
        if only_failure and _text(current.get("categoria_falha")) != only_failure:
            continue
        failed.append((row.to_dict(), current))
    failed.sort(key=lambda item: _text(item[0].get("id")))
    if sample is not None:
        failed = failed[: max(0, sample)]

    previous_full_elapsed = None
    if resume and report_path.exists():
        try:
            previous_report = json.loads(Path(report_path).read_text(encoding="utf-8"))
            previous_timings = previous_report.get("timings", {})
            previous_full_elapsed = previous_timings.get("full_audit_elapsed_seconds")
            if previous_full_elapsed is None and not previous_report.get("cache", {}).get("resumed"):
                previous_full_elapsed = previous_timings.get("elapsed_seconds")
        except (OSError, ValueError, TypeError):
            previous_full_elapsed = None

    graph_path = Path(CACHE_DIR) / "geosampa_road_graph.pkl"
    graph = RoadGraph.load_cached(graph_path, GEOSAMPA_SEGMENTOS, normalizer=normalizar_rua)
    if graph is None:
        raise RuntimeError("Grafo GeoSampa em cache não está disponível; o modo diagnóstico não constrói nem altera caches oficiais.")
    overrides = load_human_review_overrides(graph, normalizar_rua, review_path=review_path)
    engine = GeometryRecoveryEngine(graph, normalizar_rua, overrides)
    signature = {"version": VERSION, "sources": _source_signature((clean_path, Path(review_path), graph_path, Path(GEOSAMPA_SEGMENTOS)))}
    completed: dict[str, dict[str, Any]] = {}
    if reset_cache and checkpoint_path.exists():
        checkpoint_path.unlink()
    if resume and checkpoint_path.exists():
        try:
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            if checkpoint.get("signature") == signature:
                completed = checkpoint.get("results", {})
        except (OSError, ValueError, TypeError):
            completed = {}

    records = []
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    for position, (row, current) in enumerate(failed, 1):
        recape_id = _text(row.get("id"))
        if recape_id in completed:
            records.append(completed[recape_id])
            continue
        recovery = engine.recover(row, current)
        record = _result_row(row, current, recovery)
        completed[recape_id] = record
        records.append(record)
        if position % 25 == 0 or position == len(failed):
            checkpoint_path.write_text(json.dumps({"signature": signature, "results": completed, "updated_at": time.time()}, ensure_ascii=False, default=_json_default), encoding="utf-8")
    records.sort(key=lambda item: _text(item.get("id")))
    audit_frame = pd.DataFrame(records)
    _atomic_write_csv(audit_frame, output_path)
    review_frame = audit_frame[audit_frame.apply(_review_row, axis=1)].copy() if not audit_frame.empty else audit_frame.copy()
    for column in ("decision", "manual_strategy", "review_notes", "approved", "reviewed_at", "reviewed_by"):
        review_frame[column] = pd.NA
    _atomic_write_csv(review_frame, review_output_path)

    confidence_counts = audit_frame["geometry_confidence"].value_counts().to_dict() if not audit_frame.empty else {}
    strategy_counts = audit_frame["strategy_selected"].fillna("UNRESOLVED").value_counts().to_dict() if not audit_frame.empty else {}
    failure_counts = audit_frame["categoria_falha_atual"].fillna("SEM_GEOMETRIA").value_counts().to_dict() if not audit_frame.empty else {}
    all_current = len(raw)
    current_with_geometry = sum(_valid_path(value.get("path")) for value in baseline.values())
    recovered_confirmed = int((audit_frame["geometry_confidence"] == "CONFIRMED").sum()) if not audit_frame.empty else 0
    recovered_high = int((audit_frame["geometry_confidence"] == "RECONSTRUCTED_HIGH").sum()) if not audit_frame.empty else 0
    recovered_medium = int((audit_frame["geometry_confidence"] == "RECONSTRUCTED_MEDIUM").sum()) if not audit_frame.empty else 0
    recovered_estimated = int((audit_frame["geometry_confidence"] == "ESTIMATED").sum()) if not audit_frame.empty else 0
    unresolved = int((audit_frame["geometry_confidence"] == "UNRESOLVED").sum()) if not audit_frame.empty else len(failed)
    report = {
        "version": VERSION, "total_recapes": all_current, "audited_without_geometry": len(failed),
        "current_with_geometry": current_with_geometry, "current_coverage_pct": current_with_geometry / all_current * 100 if all_current else 0.0,
        "total_without_geometry": all_current - current_with_geometry, "recovered_confirmed": recovered_confirmed,
        "recovered_high": recovered_high, "recovered_medium": recovered_medium, "recovered_estimated": recovered_estimated,
        "unresolved": unresolved, "projected_coverage_confirmed_pct": (current_with_geometry + recovered_confirmed) / all_current * 100 if all_current else 0.0,
        "projected_coverage_with_reconstructed_pct": (current_with_geometry + recovered_confirmed + recovered_high + recovered_medium) / all_current * 100 if all_current else 0.0,
        "projected_coverage_with_estimated_pct": (current_with_geometry + recovered_confirmed + recovered_high + recovered_medium + recovered_estimated) / all_current * 100 if all_current else 0.0,
        "strategies": {str(key): int(value) for key, value in strategy_counts.items()},
        "failure_reasons": {str(key): int(value) for key, value in failure_counts.items()},
        "confidence": {str(key): int(value) for key, value in confidence_counts.items()},
        "timings": {
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "full_audit_elapsed_seconds": round(float(previous_full_elapsed), 3) if previous_full_elapsed is not None else round(time.perf_counter() - started, 3),
        },
        "cache": {"graph_loaded": True, "context_hits": engine.cache_hits, "checkpoint": str(checkpoint_path), "resumed": bool(resume and completed)},
        "memory": {"peak_tracemalloc_mb": round(tracemalloc.get_traced_memory()[1] / 1024 / 1024, 2)},
        "review_rows": len(review_frame), "sample": sample, "only_failure": only_failure,
    }
    tracemalloc.stop()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = report_path.with_suffix(report_path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    os.replace(temporary, report_path)
    return report


def main(argv: list[str] | None = None) -> dict[str, Any]:
    parser = argparse.ArgumentParser(description="Auditoria diagnóstica de recuperação de geometrias")
    parser.add_argument("--sample", type=int, default=None)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--reset-cache", action="store_true")
    parser.add_argument("--only-failure", default=None)
    parser.add_argument("--quality-shadow", action="store_true", help="executa a segunda etapa sobre ESTIMATED e UNRESOLVED")
    parser.add_argument("--only-same-transversal", action="store_true", help="executa somente a categoria De=Até")
    parser.add_argument("--quality-repair", action="store_true", help="repara um CSV shadow integral já calculado, sem recalcular geometrias")
    args = parser.parse_args(argv)
    if args.quality_repair:
        report = repair_quality_shadow_outputs()
    elif args.only_same_transversal:
        report = run_same_transversal_audit(resume=args.resume, reset_cache=args.reset_cache, sample=args.sample)
    elif args.quality_shadow:
        report = run_quality_shadow_audit(resume=args.resume, reset_cache=args.reset_cache, sample=args.sample)
    else:
        report = run_audit(sample=args.sample, resume=args.resume, reset_cache=args.reset_cache, only_failure=args.only_failure)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


if __name__ == "__main__":
    main()
