"""Índice topológico persistente dos segmentos reais do GeoSampa.

O módulo separa a construção (cara) do índice do roteamento (barato). Todas as
geometrias devolvidas são segmentos originais; o roteamento apenas orienta e
concatena esses segmentos, sem recorte, ``substring`` ou ``nearest_points``.
"""
from __future__ import annotations

import math
import os
import pickle
from collections import defaultdict
from dataclasses import dataclass
from numbers import Integral
from typing import Callable

import networkx as nx
from rapidfuzz import fuzz, process
from shapely.geometry import LineString, MultiLineString, Point
from shapely.strtree import STRtree


def _parts(geometry):
    if geometry is None or geometry.is_empty:
        return []
    if isinstance(geometry, LineString):
        return [geometry]
    if isinstance(geometry, MultiLineString):
        return [p for p in geometry.geoms if not p.is_empty]
    return [p for p in getattr(geometry, "geoms", ()) if isinstance(p, LineString)]


def _point_key(x, y, precision=2):
    return round(float(x), precision), round(float(y), precision)


def _intersection_points(geometry):
    if geometry is None or geometry.is_empty:
        return []
    if geometry.geom_type == "Point":
        return [geometry]
    if geometry.geom_type == "MultiPoint":
        return list(geometry.geoms)
    if geometry.geom_type == "GeometryCollection":
        result = []
        for item in geometry.geoms:
            result.extend(_intersection_points(item))
        return result
    if geometry.geom_type in {"LineString", "MultiLineString"}:
        return [Point(c) for part in _parts(geometry) for c in (part.coords[0], part.coords[-1])]
    return []


@dataclass
class Segment:
    identifier: str
    geometry: LineString
    codlog: str
    street_name: str
    street_norm: str
    order: str
    start: tuple
    end: tuple


class RoadGraph:
    """Grafo único e caches de resolução/interseção/rota.

    ``build()`` é chamado uma vez. Depois disso, ``route()`` somente consulta
    índices imutáveis e preenche caches persistentes. O objeto é seguro para
    leituras concorrentes; a persistência é feita no processo coordenador.
    """

    CACHE_VERSION = 4

    def __init__(self, normalizer: Callable[[str], str], endpoint_precision=2):
        self.normalizer = normalizer
        self.endpoint_precision = endpoint_precision
        self.graph = nx.MultiGraph()
        self.segments = {}
        self.street_segments = defaultdict(list)
        self.codlog_to_street = {}
        self.street_names = []
        self.street_graphs = {}
        self.street_components = {}
        self.intersection_cache = {}
        self.resolution_cache = {}
        self.route_cache = {}
        self.path_cache = {}
        self._tree = None
        self._tree_geometries = []
        self._geometry_index = {}

    @classmethod
    def from_geodataframe(cls, roads, normalizer, progress=None):
        result = cls(normalizer)
        total = len(roads)
        for pos, (feature_number, row) in enumerate(roads.iterrows(), 1):
            geometry = row.get("geometry")
            codlog = str(row.get("codlog", "") or "").strip()
            street_name = str(row.get("nm_logradouro", "") or "").strip()
            street_norm = normalizer(street_name)
            if not street_norm:
                continue
            order = str(row.get("cd_numero_ordem_segmento", "") or "").strip()
            for part_number, part in enumerate(_parts(geometry)):
                if len(part.coords) < 2 or part.length <= 0:
                    continue
                start = _point_key(*part.coords[0], result.endpoint_precision)
                end = _point_key(*part.coords[-1], result.endpoint_precision)
                identifier = f"{feature_number}:{part_number}"
                segment = Segment(identifier, part, codlog, street_name, street_norm, order, start, end)
                result.segments[identifier] = segment
                result.street_segments[street_norm].append(identifier)
                result.graph.add_edge(start, end, key=identifier, identifier=identifier,
                                      geometry=part, length=float(part.length), codlog=codlog,
                                      street_name=street_name, street_norm=street_norm, order=order)
                if codlog:
                    result.codlog_to_street.setdefault(codlog, street_norm)
            if progress and (pos == total or pos % 1000 == 0):
                progress(pos, total)
        result.street_segments = dict(result.street_segments)
        result.street_names = sorted(result.street_segments)
        result._build_derived_indexes(progress)
        return result

    def _build_derived_indexes(self, progress=None):
        self._tree_geometries = [s.geometry for s in self.segments.values()]
        self._geometry_index = {id(g): i for i, g in enumerate(self._tree_geometries)}
        self._tree = STRtree(self._tree_geometries) if self._tree_geometries else None
        streets = list(self.street_segments)
        for pos, street in enumerate(streets, 1):
            graph = nx.Graph()
            for identifier in self.street_segments[street]:
                segment = self.segments[identifier]
                old = graph.get_edge_data(segment.start, segment.end)
                if old is None or segment.geometry.length < old["length"]:
                    graph.add_edge(segment.start, segment.end, identifier=identifier,
                                   length=float(segment.geometry.length))
            self.street_graphs[street] = graph
            self.street_components[street] = tuple(frozenset(c) for c in nx.connected_components(graph))
            if progress and (pos == len(streets) or pos % 250 == 0):
                progress(pos, len(streets))

    def _rebuild_spatial_index(self):
        self._tree_geometries = [s.geometry for s in self.segments.values()]
        self._geometry_index = {id(g): i for i, g in enumerate(self._tree_geometries)}
        self._tree = STRtree(self._tree_geometries) if self._tree_geometries else None
        if not self.street_graphs:
            self._build_derived_indexes()

    @staticmethod
    def _source_signature(source_path):
        if not source_path or not os.path.exists(source_path):
            return None
        stat = os.stat(source_path)
        return stat.st_size, stat.st_mtime_ns

    def save(self, path, source_path=None):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        payload = {"version": self.CACHE_VERSION, "source": self._source_signature(source_path), "graph": self}
        temp = f"{path}.tmp"
        with open(temp, "wb") as stream:
            pickle.dump(payload, stream, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(temp, path)

    def __getstate__(self):
        state = self.__dict__.copy()
        try:
            pickle.dumps(state.get("normalizer"))
        except (pickle.PicklingError, TypeError, AttributeError):
            state["normalizer"] = None
        # STRtree is a native object and is cheap to rebuild in each process.
        state["_tree"] = None
        state["_tree_geometries"] = []
        state["_geometry_index"] = {}
        return state

    @classmethod
    def load_cached(cls, path, source_path=None, normalizer=None):
        try:
            with open(path, "rb") as stream:
                payload = pickle.load(stream)
            if payload.get("version") != cls.CACHE_VERSION or payload.get("source") != cls._source_signature(source_path):
                return None
            graph = payload["graph"]
            graph.normalizer = normalizer or graph.normalizer
            graph._rebuild_spatial_index()
            return graph
        except (OSError, EOFError, pickle.PickleError, AttributeError, KeyError, ValueError, ImportError):
            return None

    def _candidate_ids(self, geometry):
        if self._tree is None:
            return []
        try:
            hits = self._tree.query(geometry, predicate="intersects")
        except TypeError:
            hits = self._tree.query(geometry)
        result = []
        identifiers = list(self.segments)
        for hit in hits:
            if isinstance(hit, Integral):
                result.append(identifiers[int(hit)])
            else:
                index = self._geometry_index.get(id(hit))
                if index is not None:
                    result.append(identifiers[index])
        return result

    def resolve_street(self, value, codlog=None, threshold=84):
        codlog = str(codlog or "").strip()
        if codlog:
            exact = self.codlog_to_street.get(codlog)
            if exact:
                return exact, 100.0, "CODLOG"
        normalized = self.normalizer(value or "")
        key = (normalized, int(threshold))
        if key in self.resolution_cache:
            return self.resolution_cache[key]
        if not normalized:
            result = (None, 0.0, "SEM_NOME")
        elif normalized in self.street_segments:
            result = (normalized, 100.0, "EXATO")
        else:
            match = process.extractOne(normalized, self.street_names, scorer=fuzz.token_set_ratio)
            result = ((match[0], float(match[1]), "FUZZY") if match and match[1] >= threshold
                      else (None, float(match[1]) if match else 0.0, "SEM_GEOMETRIA"))
        self.resolution_cache[key] = result
        return result

    def _raw_intersections(self, street_a, street_b):
        pair = tuple(sorted((street_a, street_b)))
        if pair in self.intersection_cache:
            return self.intersection_cache[pair]
        ids_b = set(self.street_segments.get(street_b, ()))
        points = []
        seen = set()
        for identifier in self.street_segments.get(street_a, ()):
            segment = self.segments[identifier]
            for other_id in self._candidate_ids(segment.geometry):
                if other_id not in ids_b or other_id == identifier:
                    continue
                for point in _intersection_points(segment.geometry.intersection(self.segments[other_id].geometry)):
                    key = _point_key(point.x, point.y, 1)
                    if key not in seen:
                        seen.add(key)
                        points.append(key)
        self.intersection_cache[pair] = tuple(points)
        return self.intersection_cache[pair]

    def intersections(self, main_street, other_street):
        key = (main_street, other_street)
        if key in self.path_cache:
            return self.path_cache[key]
        result = []
        for x, y in self._raw_intersections(main_street, other_street):
            point = Point(x, y)
            node = self._node_for_intersection(main_street, point)
            if node is not None:
                result.append((point, node))
        self.path_cache[key] = tuple(result)
        return self.path_cache[key]

    def _node_for_intersection(self, street, point, tolerance=8.0):
        candidates = []
        for identifier in self.street_segments.get(street, ()):
            segment = self.segments[identifier]
            for node in (segment.start, segment.end):
                distance = math.hypot(node[0] - point.x, node[1] - point.y)
                if distance <= tolerance:
                    candidates.append((distance, node))
        return min(candidates)[1] if candidates else None

    def _component(self, street, node):
        graph = self.street_graphs.get(street)
        if graph is None or node not in graph:
            return graph, set()
        for component in self.street_components.get(street, ()):
            if node in component:
                return graph, component
        return graph, set()

    def _oriented_geometry(self, identifier, start, end):
        geometry = self.segments[identifier].geometry
        first = geometry.coords[0]
        if math.hypot(first[0] - start[0], first[1] - start[1]) <= math.hypot(first[0] - end[0], first[1] - end[1]):
            return geometry
        return LineString(list(geometry.coords)[::-1])

    def _path_geometry(self, street, nodes):
        graph = self.street_graphs[street]
        coordinates, identifiers = [], []
        for start, end in zip(nodes, nodes[1:]):
            edge = graph.get_edge_data(start, end)
            if not edge or edge["identifier"] in identifiers:
                continue
            geometry = self._oriented_geometry(edge["identifier"], start, end)
            points = list(geometry.coords)
            coordinates.extend(points if not coordinates else points[1:] if coordinates[-1] == points[0] else points)
            identifiers.append(edge["identifier"])
        return (LineString(coordinates) if len(coordinates) >= 2 else None), identifiers

    def _shortest_candidates(self, street, start, end, limit=8):
        cache_key = (street, start, end, limit)
        if cache_key in self.path_cache:
            return self.path_cache[cache_key]
        graph, component = self._component(street, start)
        if graph is None or end not in component:
            self.path_cache[cache_key] = ()
            return ()
        candidates = []
        try:
            for nodes in nx.shortest_simple_paths(graph, start, end, weight="length"):
                geometry, identifiers = self._path_geometry(street, nodes)
                if geometry is not None:
                    candidates.append((geometry, identifiers, tuple(nodes)))
                if len(candidates) >= limit:
                    break
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            pass
        self.path_cache[cache_key] = tuple(candidates)
        return self.path_cache[cache_key]

    def _whole_component_geometry(self, street, start):
        graph, component = self._component(street, start)
        if graph is None or not component:
            return None, []
        coordinates, identifiers = [], []
        for node_a, node_b in nx.dfs_edges(graph, start):
            edge = graph[node_a][node_b]
            geometry = self._oriented_geometry(edge["identifier"], node_a, node_b)
            points = list(geometry.coords)
            if coordinates and coordinates[-1] != points[0]:
                coordinates.append(points[0])
            coordinates.extend(points if not coordinates else points[1:])
            identifiers.append(edge["identifier"])
        return (LineString(coordinates) if len(coordinates) >= 2 else None), identifiers

    def route(self, via, de, ate, reference=None, expected_length=None, codlog=None):
        expected = float(expected_length) if expected_length is not None and math.isfinite(float(expected_length)) else None
        key = (self.normalizer(via or ""), self.normalizer(de or ""), self.normalizer(ate or ""), str(codlog or "").strip(), expected)
        if key in self.route_cache:
            return self.route_cache[key]
        codlog_text = str(codlog or "").strip()
        codlog_status = "NAO_INFORMADO" if not codlog_text else (
            "EXISTENTE" if codlog_text in self.codlog_to_street else "INEXISTENTE"
        )
        street, score, method = self.resolve_street(via, codlog=codlog)
        meta = {
            "score_via": score,
            "method_via": method,
            "rua_via_resolvida": street,
            "segment_count_via": len(self.street_segments.get(street, ())) if street else 0,
            "intersection_count_de": 0,
            "intersection_count_ate": 0,
            "component_connected": False,
            "path_found": False,
            "codlog_status": codlog_status,
        }
        if street is None:
            result = (None, "SEM_RUA_GEOM", meta)
            self.route_cache[key] = result
            return result
        de_special = self.normalizer(de or "")
        ate_special = self.normalizer(ate or "")
        if "TODA" in de_special and "EXTENSAO" in de_special or "TODA" in ate_special and "EXTENSAO" in ate_special:
            node = self._closest_node(street, reference)
            geometry, identifiers = self._whole_component_geometry(street, node)
            meta["component_connected"] = bool(node is not None)
            meta["path_found"] = geometry is not None and not geometry.is_empty
            result = self._to_result(geometry, "TODA_EXTENSAO", identifiers, score)
            result[2].update(meta)
            self.route_cache[key] = result
            return result
        de_street, de_score, de_method = self.resolve_street(de)
        meta.update(score_de=de_score, method_de=de_method, rua_de_resolvida=de_street,
                    segment_count_de=len(self.street_segments.get(de_street, ())) if de_street else 0)
        if de_street is None:
            result = (None, "SEM_RUA_DE", meta)
            self.route_cache[key] = result
            return result
        starts = self.intersections(street, de_street)
        meta["intersection_count_de"] = len(starts)
        if not starts:
            result = (None, "SEM_INTERSECAO_DE", meta)
            self.route_cache[key] = result
            return result
        start = self._choose_intersection(starts, reference)
        _, start_component = self._component(street, start[1])
        meta["component_connected"] = bool(start_component)
        if "FIM DA VIA" in ate_special or "ATE O FIM DA VIA" in ate_special:
            graph, component = self._component(street, start[1])
            endpoints = [n for n in component if graph.degree(n) <= 1] or list(component)
            candidates = [c for endpoint in endpoints for c in self._shortest_candidates(street, start[1], endpoint, 2)]
        else:
            ate_street, ate_score, ate_method = self.resolve_street(ate)
            meta.update(score_ate=ate_score, method_ate=ate_method, rua_ate_resolvida=ate_street,
                        segment_count_ate=len(self.street_segments.get(ate_street, ())) if ate_street else 0)
            if ate_street is None:
                result = (None, "SEM_RUA_ATE", meta)
                self.route_cache[key] = result
                return result
            ends = self.intersections(street, ate_street)
            meta["intersection_count_ate"] = len(ends)
            if not ends:
                result = (None, "SEM_INTERSECAO_ATE", meta)
                self.route_cache[key] = result
                return result
            end = self._choose_intersection(ends, reference)
            _, end_component = self._component(street, end[1])
            meta["component_connected"] = bool(start_component and end_component and start[1] in end_component)
            candidates = self._shortest_candidates(street, start[1], end[1], 12)
        if not candidates:
            result = (None, "SEM_CAMINHO", meta)
        else:
            geometry, identifiers, _ = self._choose_by_length(candidates, expected)
            status = "OK" if expected is None or abs(geometry.length - expected) / max(expected, 1) <= .20 else "OK_FORA_EXTENSAO"
            result = self._to_result(geometry, status, identifiers, score)
            result[2].update(meta, path_found=True)
        self.route_cache[key] = result
        return result

    def _closest_node(self, street, reference):
        graph = self.street_graphs.get(street)
        if graph is None or not graph:
            return None
        if reference is None:
            return next(iter(graph.nodes))
        return min(graph.nodes, key=lambda n: math.hypot(n[0] - reference.x, n[1] - reference.y))

    @staticmethod
    def _choose_intersection(items, reference):
        return items[0] if reference is None else min(items, key=lambda item: item[0].distance(reference))

    @staticmethod
    def _choose_by_length(candidates, expected):
        if expected is None or expected <= 0:
            return candidates[0]
        return min(candidates, key=lambda item: abs(item[0].length - expected))

    @staticmethod
    def _to_result(geometry, status, identifiers, score):
        if geometry is None or geometry.is_empty:
            return None, "SEM_GEOMETRIA", {"segment_count": 0, "score_via": score}
        return geometry, status, {"segment_count": len(identifiers), "score_via": score, "identifiers": identifiers}
