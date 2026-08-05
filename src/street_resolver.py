"""Auditoria diagnóstica de resolução contextual de logradouros.

Este módulo é deliberadamente separado de :mod:`road_graph`.  Ele consulta os
índices já construídos pelo ``RoadGraph``, mas não chama ``route()`` e não
persiste alterações nos caches oficiais.  A resolução aqui produz evidências e
recomendações para revisão humana; ela não substitui a resolução usada pelo
ETL.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import pickle
import re
import shutil
import sys
import time
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import pandas as pd
from rapidfuzz import fuzz, process

try:
    import geopandas as gpd
    from pyproj import Transformer
    from shapely.geometry import Point
except ImportError:  # pragma: no cover - dependências opcionais do ETL
    gpd = None
    Transformer = None
    Point = None


try:
    from road_graph import RoadGraph
except ImportError:  # permite ``python -m src.street_resolver``
    from .road_graph import RoadGraph
    # O cache oficial pode ter sido criado executando ``src/transform.py``
    # diretamente, portanto seus pickles referenciam o módulo top-level.
    sys.modules.setdefault("road_graph", sys.modules[RoadGraph.__module__])


PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_ALIAS_PATH = PROJECT_DIR / "data" / "config" / "street_aliases.csv"
DEFAULT_PROCESSED_DIR = PROJECT_DIR / "data" / "processed"
DEFAULT_CACHE_PATH = PROJECT_DIR / "data" / "cache" / "street_resolution_diagnostic.pkl"
DEFAULT_GRAPH_CACHE = PROJECT_DIR / "data" / "cache" / "geosampa_road_graph.pkl"
DEFAULT_GEOSAMPA_PATH = PROJECT_DIR / "data" / "cache" / "geosampa_segmento_logradouro.geojson"

ALIAS_COLUMNS = [
    "original_norm",
    "resolved_norm",
    "codlog",
    "scope",
    "source",
    "notes",
    "active",
]
CACHE_VERSION = 2
SPECIAL_REFERENCE_TERMS = (
    "TODA EXTENSAO",
    "TODA A EXTENSAO",
    "EM TODA EXTENSAO",
    "ATE O FIM DA VIA",
    "FIM DA VIA",
)


@dataclass
class StreetResolverConfig:
    """Pesos e limites centralizados da camada diagnóstica."""

    fuzzy_candidate_limit: int = 10
    fuzzy_min_score: float = 60.0
    reference_min_score: float = 84.0
    ambiguous_margin: float = 8.0
    medium_margin: float = 4.0
    high_score: float = 84.0
    medium_score: float = 72.0
    lexical_weight: float = 0.45
    geographic_weight: float = 0.25
    de_weight: float = 0.15
    ate_weight: float = 0.15
    ratio_weight: float = 0.30
    token_sort_weight: float = 0.25
    wratio_weight: float = 0.20
    token_coverage_weight: float = 0.15
    length_weight: float = 0.10
    incomplete_penalty: float = 12.0
    coordinate_round_digits: int = 5
    distance_very_strong_m: float = 30.0
    distance_strong_m: float = 80.0
    distance_moderate_m: float = 150.0
    distance_weak_m: float = 300.0
    transversal_fuzzy_min_score: float = 75.0
    transversal_candidate_limit: int = 5
    transversal_geo_coherent_m: float = 300.0
    lexical_shortlist_limit: int = 32
    context_candidate_limit: int = 3
    geographic_rounding_m: float = 10.0
    checkpoint_every: int = 250
    preposition_tokens: tuple[str, ...] = ("DE", "DA", "DO", "DOS", "DAS")
    optional_title_tokens: tuple[str, ...] = (
        "GENERAL", "CORONEL", "CAPITAO", "DOUTOR", "DOUTORA",
        "PROFESSOR", "PROFESSORA", "PRESIDENTE", "DEPUTADO",
        "ENGENHEIRO", "PADRE", "SANTO", "SAO",
    )


@dataclass
class StreetCandidate:
    street_norm: str
    street_name: str
    codlog: str | None = None
    name_score: float = 0.0
    token_sort_score: float = 0.0
    token_set_score: float = 0.0
    wratio_score: float = 0.0
    distance_m: float | None = None
    intersects_de: bool | None = None
    intersects_ate: bool | None = None
    final_score: float = 0.0
    evidence: list[str] = field(default_factory=list)
    partial_score: float = 0.0
    token_coverage_original: float = 0.0
    token_coverage_candidate: float = 0.0
    length_ratio: float = 0.0
    missing_tokens: list[str] = field(default_factory=list)
    extra_tokens: list[str] = field(default_factory=list)
    critical_missing_tokens: list[str] = field(default_factory=list)
    lexical_score: float = 0.0
    geographic_score: float | None = None
    de_score: float | None = None
    ate_score: float | None = None
    intersection_count_de: int = 0
    intersection_count_ate: int = 0
    component_connected: bool | None = None
    segment_count: int = 0
    codlogs: list[str] = field(default_factory=list)
    de_resolution_confidence: str = "UNAVAILABLE"
    ate_resolution_confidence: str = "UNAVAILABLE"
    de_resolution_status: str = "CAMPO_VAZIO"
    ate_resolution_status: str = "CAMPO_VAZIO"
    de_candidate: str | None = None
    ate_candidate: str | None = None
    de_distance_m: float | None = None
    ate_distance_m: float | None = None
    de_intersection_status: str = "INDISPONIVEL"
    ate_intersection_status: str = "INDISPONIVEL"
    de_intersection_count: int = 0
    ate_intersection_count: int = 0
    de_alternatives: list[str] = field(default_factory=list)
    ate_alternatives: list[str] = field(default_factory=list)

    @property
    def token_coverage(self) -> float:
        return self.token_coverage_original

    @property
    def incomplete(self) -> bool:
        return bool(self.critical_missing_tokens)


@dataclass
class StreetResolution:
    original: str
    normalized: str
    resolved_street: str | None = None
    resolved_codlog: str | None = None
    method: str = "SEM_RESOLUCAO"
    confidence: str = "UNRESOLVED"
    final_score: float = 0.0
    margin_top2: float | None = None
    candidates: list[StreetCandidate] = field(default_factory=list)
    reason: str = ""
    invalid_alias: bool = False
    codlog_invalid: bool = False
    review_reasons: list[str] = field(default_factory=list)
    street_confidence: str = "UNRESOLVED"
    street_requires_review: bool = False
    street_review_reasons: list[str] = field(default_factory=list)
    route_context_status: str = "TRANSVERSALS_UNRESOLVED"
    route_requires_review: bool = False
    route_review_reasons: list[str] = field(default_factory=list)
    de_resolution_confidence: str = "UNAVAILABLE"
    ate_resolution_confidence: str = "UNAVAILABLE"
    de_resolution_status: str = "CAMPO_VAZIO"
    ate_resolution_status: str = "CAMPO_VAZIO"
    de_candidate: str | None = None
    ate_candidate: str | None = None
    de_distance_m: float | None = None
    ate_distance_m: float | None = None
    de_intersection_status: str = "INDISPONIVEL"
    ate_intersection_status: str = "INDISPONIVEL"
    de_intersection_count: int = 0
    ate_intersection_count: int = 0
    de_alternatives_json: str = "[]"
    ate_alternatives_json: str = "[]"


@dataclass
class _TransversalResult:
    normalized: str
    confidence: str = "UNAVAILABLE"
    status: str = "CAMPO_VAZIO"
    method: str = ""
    candidate: str | None = None
    distance_m: float | None = None
    intersection_status: str = "INDISPONIVEL"
    intersects: bool | None = None
    intersection_count: int = 0
    margin_top2: float | None = None
    alternatives: list[str] = field(default_factory=list)
    review_reasons: list[str] = field(default_factory=list)


class AuditInterrupted(RuntimeError):
    """Interrupção controlada usada para testar e retomar checkpoints."""


@dataclass(frozen=True)
class StreetResolutionContext:
    """Campos necessários para resolver uma linha de recape."""

    via_original: str = ""
    logradouro_geosampa_original: str = ""
    de_original: str = ""
    ate_original: str = ""
    codlog: str = ""
    latitude: float | None = None
    longitude: float | None = None
    reference: Any = None

    @property
    def name_used(self) -> str:
        return (
            _text(self.logradouro_geosampa_original)
            or _text(self.via_original)
        )


@dataclass
class _Alias:
    original_norm: str
    resolved_norm: str
    codlog: str | None
    scope: str
    source: str
    notes: str


def _text(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value).strip()


def _safe_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _source_signature(path: str | os.PathLike[str] | None) -> tuple[Any, ...] | None:
    if not path:
        return None
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return (int(stat.st_size), int(stat.st_mtime_ns))


def _json_fingerprint(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _intersection_points(geometry: Any) -> list[Any]:
    if geometry is None or geometry.is_empty or Point is None:
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
        parts = [geometry] if geometry.geom_type == "LineString" else list(geometry.geoms)
        return [Point(coordinate) for part in parts for coordinate in (part.coords[0], part.coords[-1])]
    return []


def _unique_points(points: Iterable[Any]) -> list[Any]:
    result = []
    seen = set()
    for point in points:
        key = (round(float(point.x), 2), round(float(point.y), 2))
        if key not in seen:
            seen.add(key)
            result.append(point)
    return result


class StreetResolver:
    """Resolve logradouros com evidências, sem alterar a rota oficial."""

    VERSION = "street-resolver-diagnostic-v9"

    def __init__(
        self,
        graph: RoadGraph,
        *,
        normalizer: Callable[[str], str] | None = None,
        text_corrector: Callable[[Any], Any] | None = None,
        aliases_path: str | os.PathLike[str] = DEFAULT_ALIAS_PATH,
        cache_path: str | os.PathLike[str] = DEFAULT_CACHE_PATH,
        source_path: str | os.PathLike[str] | None = DEFAULT_GEOSAMPA_PATH,
        config: StreetResolverConfig | None = None,
    ) -> None:
        self.graph = graph
        self.normalizer = normalizer or self._load_project_normalizer()
        self.text_corrector = text_corrector or self._load_project_corrector()
        self.aliases_path = Path(aliases_path)
        self.cache_path = Path(cache_path)
        self.source_path = Path(source_path) if source_path else None
        self.config = config or StreetResolverConfig()
        self._transformer = (
            Transformer.from_crs("EPSG:4326", "EPSG:31983", always_xy=True)
            if Transformer is not None else None
        )
        self.invalid_aliases: dict[str, str] = {}
        self.aliases: dict[str, _Alias] = {}
        self.alias_file_errors: list[str] = []
        self._lexical_cache: dict[str, list[StreetCandidate]] = {}
        self._primary_cache: dict[tuple[str, str], tuple[str, list[StreetCandidate]]] = {}
        self._reference_cache: dict[str, str | None] = {}
        self._geographic_cache: dict[tuple[Any, ...], float | None] = {}
        self._intersection_cache: dict[tuple[str, str], tuple[list[Any], str | None]] = {}
        self._transversal_cache: dict[tuple[Any, ...], _TransversalResult] = {}
        self._seen_transversal_names: set[str] = set()
        self._context_cache: dict[tuple[Any, ...], dict[str, Any]] = {}
        self._resolution_cache: dict[tuple[Any, ...], StreetResolution] = {}
        self._lexical_index: dict[str, set[str]] = {}
        self._street_token_counts: dict[str, int] = {}
        self.cache_hits = 0
        self.cache_misses = 0
        self.cache_stats: dict[str, int] = {
            "lexical_cache_hits": 0,
            "lexical_cache_misses": 0,
            "geographic_cache_hits": 0,
            "geographic_cache_misses": 0,
            "transversal_cache_hits": 0,
            "transversal_cache_misses": 0,
            "intersection_cache_hits": 0,
            "intersection_cache_misses": 0,
            "official_intersection_cache_hits": 0,
            "context_cache_hits": 0,
            "context_cache_misses": 0,
            "checkpoint_reused_records": 0,
            "contexts_deduplicated": 0,
            "exact_fast_paths": 0,
            "fuzzy_primary_resolutions": 0,
            "transversal_evaluations_skipped": 0,
            "intersection_queries": 0,
            "candidates_scored": 0,
            "unique_primary_names": 0,
            "unique_transversal_names": 0,
        }
        self.stage_seconds: dict[str, float] = {
            "lexical": 0.0,
            "geographic": 0.0,
            "intersections": 0.0,
            "street_resolution": 0.0,
            "transversal_resolution": 0.0,
            "route_context": 0.0,
            "load_graph": 0.0,
            "build_lexical_index": 0.0,
            "primary_exact": 0.0,
            "primary_fuzzy": 0.0,
            "transversal_lexical": 0.0,
            "report_write": 0.0,
            "checkpoint": 0.0,
        }
        self._load_aliases()
        self._build_lexical_index()
        self._cache_identity = self._make_cache_identity()
        self._load_cache()

    @staticmethod
    def _load_project_normalizer() -> Callable[[str], str]:
        try:
            from transform import normalizar_rua
        except ImportError:  # pragma: no cover - import path alternativo
            from .transform import normalizar_rua
        return normalizar_rua

    @staticmethod
    def _load_project_corrector() -> Callable[[Any], Any]:
        try:
            from transform import corrigir_texto
        except ImportError:  # pragma: no cover - import path alternativo
            from .transform import corrigir_texto
        return corrigir_texto

    def _normalize(self, value: Any) -> str:
        corrected = self.text_corrector(value)
        return _text(self.normalizer(corrected))

    @staticmethod
    def _active(value: Any) -> bool:
        return _text(value).lower() not in {"", "0", "false", "nao", "não", "no", "inativo"}

    def _load_aliases(self) -> None:
        path = self.aliases_path
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            pd.DataFrame(columns=ALIAS_COLUMNS).to_csv(path, index=False, encoding="utf-8-sig")
            return
        try:
            frame = pd.read_csv(path, dtype=str, encoding="utf-8-sig", keep_default_na=False)
        except (OSError, UnicodeError, pd.errors.ParserError) as exc:
            self.alias_file_errors.append(f"arquivo de aliases inválido: {exc}")
            return
        missing = [column for column in ALIAS_COLUMNS if column not in frame.columns]
        if missing:
            self.alias_file_errors.append(f"colunas ausentes no arquivo de aliases: {', '.join(missing)}")
            return
        for _, row in frame.iterrows():
            if not self._active(row.get("active")):
                continue
            original_norm = self._normalize(row.get("original_norm"))
            resolved_norm = self._normalize(row.get("resolved_norm"))
            codlog = _text(row.get("codlog")) or None
            target_by_codlog = self.graph.codlog_to_street.get(codlog) if codlog else None
            target_by_name = resolved_norm if resolved_norm in self.graph.street_segments else None
            invalid_reason = None
            if codlog and target_by_codlog is None:
                invalid_reason = f"CODLOG de destino inexistente: {codlog}"
            elif target_by_codlog and target_by_name and target_by_codlog != target_by_name:
                invalid_reason = "CODLOG e resolved_norm apontam para vias diferentes"
            elif not target_by_codlog and not target_by_name:
                invalid_reason = f"destino inexistente: {resolved_norm or codlog or '(vazio)'}"
            if not original_norm:
                invalid_reason = "original_norm vazio"
            if invalid_reason:
                if original_norm:
                    self.invalid_aliases[original_norm] = invalid_reason
                continue
            target = target_by_codlog or target_by_name
            self.aliases.setdefault(
                original_norm,
                _Alias(
                    original_norm=original_norm,
                    resolved_norm=target or "",
                    codlog=codlog,
                    scope=_text(row.get("scope")),
                    source=_text(row.get("source")),
                    notes=_text(row.get("notes")),
                ),
            )

    def _build_lexical_index(self) -> None:
        """Constrói um índice leve de tokens, uma vez por instância."""
        started = time.perf_counter()
        self._lexical_index.clear()
        self._street_token_counts.clear()
        for street in self.graph.street_names:
            normalized = _text(street)
            tokens = normalized.split()
            self._street_token_counts[normalized] = len(tokens)
            for token in set(tokens):
                self._lexical_index.setdefault(token, set()).add(normalized)
        self.cache_stats["unique_primary_names"] = len(self.graph.street_names)
        self.stage_seconds["build_lexical_index"] += time.perf_counter() - started

    def _graph_identity(self) -> str:
        return _json_fingerprint({
            "street_names": list(self.graph.street_names),
            "codlog_to_street": sorted(self.graph.codlog_to_street.items()),
            "segment_count": len(getattr(self.graph, "segments", {})),
        })

    def _make_cache_identity(self) -> dict[str, Any]:
        config = asdict(self.config)
        return {
            "version": self.VERSION,
            "cache_version": CACHE_VERSION,
            "source": _source_signature(self.source_path),
            "aliases": _source_signature(self.aliases_path),
            "graph": self._graph_identity(),
            "config": _json_fingerprint(config),
        }

    def _cache_key(self, context: StreetResolutionContext) -> tuple[Any, ...]:
        lat = _safe_float(context.latitude)
        lon = _safe_float(context.longitude)
        digits = self.config.coordinate_round_digits
        return (
            self.VERSION,
            self._cache_identity["source"],
            self._cache_identity["aliases"],
            self._cache_identity["config"],
            self._normalize(context.name_used),
            _text(context.codlog),
            round(lat, digits) if lat is not None else None,
            round(lon, digits) if lon is not None else None,
            self._normalize(context.de_original),
            self._normalize(context.ate_original),
        )

    def _load_cache(self) -> None:
        try:
            with self.cache_path.open("rb") as stream:
                payload = pickle.load(stream)
            if not isinstance(payload, Mapping):
                return
            if payload.get("identity") != self._cache_identity:
                return
            resolutions = {}
            for key, value in payload.get("resolutions", {}).items():
                if isinstance(value, StreetResolution):
                    resolutions[key] = value
                    continue
                if not isinstance(value, Mapping):
                    continue
                candidate_values = value.get("candidates", [])
                candidates = [self._candidate_from_payload(item) for item in candidate_values if isinstance(item, Mapping)]
                names = {item.name for item in fields(StreetResolution)}
                resolution_values = {
                    name: value[name]
                    for name in names
                    if name != "candidates" and name in value
                }
                resolution_values["candidates"] = candidates
                resolutions[key] = StreetResolution(**resolution_values)
            lexical = {}
            for key, values in payload.get("lexical", {}).items():
                candidates = []
                for value in values:
                    if isinstance(value, StreetCandidate):
                        candidates.append(value)
                    elif isinstance(value, Mapping):
                        candidates.append(self._candidate_from_payload(value))
                lexical[key] = candidates
            geographic = dict(payload.get("geographic", {}))
            intersections = {}
            for key, value in payload.get("intersections", {}).items():
                if isinstance(value, tuple) and len(value) == 2:
                    intersections[key] = (list(value[0] or []), value[1])
                elif isinstance(value, Mapping):
                    intersections[key] = (
                        list(value.get("points", []) or []),
                        value.get("component"),
                    )
            transversals = {}
            transversal_names = {item.name for item in fields(_TransversalResult)}
            for key, value in payload.get("transversals", {}).items():
                if isinstance(value, _TransversalResult):
                    transversals[key] = value
                elif isinstance(value, Mapping):
                    transversals[key] = _TransversalResult(**{
                        name: value[name]
                        for name in transversal_names
                        if name in value
                    })
            self._resolution_cache = resolutions
            self._lexical_cache = lexical
            primary = {}
            for key, value in payload.get("primary", {}).items():
                if isinstance(value, tuple) and len(value) == 2:
                    method, values = value
                elif isinstance(value, Mapping):
                    method, values = value.get("method", "SEM_RESOLUCAO"), value.get("candidates", [])
                else:
                    continue
                primary[key] = (
                    str(method),
                    [self._candidate_from_payload(item) if isinstance(item, Mapping) else item for item in values],
                )
            self._primary_cache = primary
            self._reference_cache = dict(payload.get("references", {}))
            self._geographic_cache = geographic
            self._intersection_cache = intersections
            self._transversal_cache = transversals
            self._context_cache = dict(payload.get("contexts", {}))
        except (OSError, EOFError, pickle.PickleError, AttributeError, KeyError, ValueError, TypeError, ImportError):
            return

    @staticmethod
    def _candidate_from_payload(value: Mapping[str, Any]) -> StreetCandidate:
        names = {item.name for item in fields(StreetCandidate)}
        return StreetCandidate(**{name: value.get(name) for name in names if name in value})

    def save_cache(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "identity": self._cache_identity,
            # Apenas dicionários/listas simples são persistidos. Isso torna o
            # cache compatível entre ``python src/street_resolver.py`` e
            # ``python -m src.street_resolver``.
            "resolutions": {key: asdict(value) for key, value in self._resolution_cache.items()},
            "lexical": {
                key: [asdict(value) for value in values]
                for key, values in self._lexical_cache.items()
            },
            "primary": {
                key: {
                    "method": method,
                    "candidates": [asdict(value) for value in values],
                }
                for key, (method, values) in self._primary_cache.items()
            },
            "references": dict(self._reference_cache),
            "geographic": dict(self._geographic_cache),
            "intersections": dict(self._intersection_cache),
            "transversals": {
                key: asdict(value)
                for key, value in self._transversal_cache.items()
            },
            "contexts": dict(self._context_cache),
        }
        temporary = self.cache_path.with_suffix(self.cache_path.suffix + ".tmp")
        with temporary.open("wb") as stream:
            pickle.dump(payload, stream, protocol=pickle.HIGHEST_PROTOCOL)
        _atomic_replace(temporary, self.cache_path)

    def _street_metadata(self, street_norm: str) -> tuple[str, str | None, int, list[str]]:
        identifiers = sorted(self.graph.street_segments.get(street_norm, ()))
        if not identifiers:
            return street_norm, None, 0, []
        segments = [self.graph.segments[identifier] for identifier in identifiers]
        names = sorted({_text(segment.street_name) for segment in segments if _text(segment.street_name)})
        codlogs = sorted({_text(segment.codlog) for segment in segments if _text(segment.codlog)})
        return names[0] if names else street_norm, (codlogs[0] if codlogs else None), len(identifiers), codlogs

    def _token_metrics(self, original: str, candidate: str) -> dict[str, Any]:
        original_tokens = original.split()
        candidate_tokens = candidate.split()
        missing = []
        covered_original = 0
        for token in original_tokens:
            if token in candidate_tokens:
                covered_original += 1
            elif any(fuzz.ratio(token, other) >= 92 for other in candidate_tokens):
                covered_original += 1
            else:
                missing.append(token)
        covered_candidate = sum(
            1 for token in candidate_tokens
            if token in original_tokens or any(fuzz.ratio(token, other) >= 92 for other in original_tokens)
        )
        extra = [token for token in candidate_tokens if token not in original_tokens]
        original_length = len(original.replace(" ", ""))
        candidate_length = len(candidate.replace(" ", ""))
        return {
            "token_coverage_original": covered_original / len(original_tokens) if original_tokens else 0.0,
            "token_coverage_candidate": covered_candidate / len(candidate_tokens) if candidate_tokens else 0.0,
            "length_ratio": min(original_length, candidate_length) / max(original_length, candidate_length, 1),
            "missing_tokens": missing,
            "extra_tokens": extra,
        }

    def _lexical_candidate(self, original: str, street_norm: str) -> StreetCandidate:
        street_name, codlog, segment_count, codlogs = self._street_metadata(street_norm)
        metrics = self._token_metrics(original, street_norm)
        candidate = StreetCandidate(
            street_norm=street_norm,
            street_name=street_name,
            codlog=codlog,
            name_score=float(fuzz.ratio(original, street_norm)),
            token_sort_score=float(fuzz.token_sort_ratio(original, street_norm)),
            token_set_score=float(fuzz.token_set_ratio(original, street_norm)),
            wratio_score=float(fuzz.WRatio(original, street_norm)),
            partial_score=float(fuzz.partial_ratio(original, street_norm)),
            segment_count=segment_count,
            codlogs=codlogs,
            **metrics,
        )
        candidate.critical_missing_tokens = self._critical_missing_tokens(candidate)
        candidate.lexical_score = self._lexical_score(candidate)
        return candidate

    def _lexical_score(self, candidate: StreetCandidate) -> float:
        config = self.config
        score = (
            config.ratio_weight * candidate.name_score
            + config.token_sort_weight * candidate.token_sort_score
            + config.wratio_weight * candidate.wratio_score
            + config.token_coverage_weight * candidate.token_coverage_original * 100
            + config.length_weight * candidate.length_ratio * 100
        )
        if candidate.critical_missing_tokens:
            score -= config.incomplete_penalty * len(candidate.critical_missing_tokens) / max(len(candidate.street_norm.split()), 1)
        return max(0.0, min(100.0, score))

    def _fuzzy_candidates(self, original: str) -> list[StreetCandidate]:
        if original in self._lexical_cache:
            self.cache_stats["lexical_cache_hits"] += 1
            return [self._copy_candidate(item) for item in self._lexical_cache[original]]
        self.cache_stats["lexical_cache_misses"] += 1
        if not original or not self.graph.street_names:
            return []
        started = time.perf_counter()
        limit = max(5, min(10, int(self.config.fuzzy_candidate_limit)))
        selected: set[str] = set()
        low_information = set(self.config.preposition_tokens) | {"E"}
        blocking_tokens = [token for token in original.split() if token not in low_information]
        blocked_names: set[str] = set()
        for token in blocking_tokens:
            blocked_names.update(self._lexical_index.get(token, ()))
        # Mantemos o universo original para preservar exatamente o shortlist
        # historico; o indice continua disponivel para fases futuras e para
        # instrumentacao, enquanto a deduplicacao/cache elimina o retrabalho.
        scoring_names = sorted(self.graph.street_names)
        # A união dos melhores resultados de vários scorers evita que um
        # candidato parcial token_set_ratio=100 seja a única alternativa vista.
        for scorer in (fuzz.ratio, fuzz.token_sort_ratio, fuzz.token_set_ratio, fuzz.WRatio, fuzz.partial_ratio):
            matches = process.extract(original, scoring_names, scorer=scorer, limit=limit)
            for name, _, _ in matches:
                selected.add(name)
        candidates = [self._lexical_candidate(original, street) for street in selected]
        candidates.sort(key=lambda item: (-item.lexical_score, -item.name_score, item.street_norm))
        candidates = candidates[:limit]
        self.cache_stats["candidates_scored"] += len(candidates)
        self._lexical_cache[original] = [self._copy_candidate(item) for item in candidates]
        self.stage_seconds["lexical"] += time.perf_counter() - started
        return candidates

    @staticmethod
    def _copy_candidate(candidate: StreetCandidate) -> StreetCandidate:
        values = asdict(candidate)
        values["evidence"] = list(candidate.evidence)
        values["missing_tokens"] = list(candidate.missing_tokens)
        values["extra_tokens"] = list(candidate.extra_tokens)
        return StreetCandidate(**values)

    def _reference_street(self, value: Any) -> str | None:
        normalized = self._normalize(value)
        if not normalized or any(term in normalized for term in SPECIAL_REFERENCE_TERMS):
            return None
        if normalized in self.graph.street_segments:
            return normalized
        if normalized in self._reference_cache:
            return self._reference_cache[normalized]
        matches = process.extract(
            normalized,
            self.graph.street_names,
            scorer=fuzz.token_sort_ratio,
            limit=3,
        )
        matches = sorted(matches, key=lambda item: (-float(item[1]), item[0]))
        result = matches[0][0] if matches and float(matches[0][1]) >= self.config.reference_min_score else None
        self._reference_cache[normalized] = result
        return result

    def _transversal_key(self, main_street: str, value: Any, reference: Any) -> tuple[Any, ...]:
        normalized = self._normalize(value)
        coordinates = self._rounded_reference_key(reference)
        return main_street, normalized, *coordinates

    def _rounded_reference_key(self, reference: Any) -> tuple[float | None, float | None]:
        if reference is None:
            return None, None
        grid = max(float(self.config.geographic_rounding_m), 0.1)
        try:
            return (
                round(round(float(reference.x) / grid) * grid, 3),
                round(round(float(reference.y) / grid) * grid, 3),
            )
        except (AttributeError, TypeError, ValueError):
            return None, None

    def _street_distance(self, street: str | None, reference: Any) -> float | None:
        if not street or reference is None:
            return None
        key = (street, *self._rounded_reference_key(reference))
        if key in self._geographic_cache:
            self.cache_stats["geographic_cache_hits"] += 1
            return self._geographic_cache[key]
        self.cache_stats["geographic_cache_misses"] += 1
        segments = [self.graph.segments[item] for item in self.graph.street_segments.get(street, ())]
        if not segments:
            self._geographic_cache[key] = None
            return None
        distance = min(float(segment.geometry.distance(reference)) for segment in segments)
        self._geographic_cache[key] = distance
        return distance

    def _critical_missing_tokens(self, candidate: StreetCandidate) -> list[str]:
        return [
            token for token in candidate.missing_tokens
            if token not in self.config.preposition_tokens
            and token not in self.config.optional_title_tokens
        ]

    @staticmethod
    def _copy_transversal(result: _TransversalResult) -> _TransversalResult:
        return _TransversalResult(
            normalized=result.normalized,
            confidence=result.confidence,
            status=result.status,
            method=result.method,
            candidate=result.candidate,
            distance_m=result.distance_m,
            intersection_status=result.intersection_status,
            intersects=result.intersects,
            intersection_count=result.intersection_count,
            margin_top2=result.margin_top2,
            alternatives=list(result.alternatives),
            review_reasons=list(result.review_reasons),
        )

    def _resolve_transversal(
        self,
        value: Any,
        *,
        main_street: str,
        reference: Any,
    ) -> _TransversalResult:
        """Resolve De/Até conservadoramente e exige interseção para fuzzy."""
        normalized = self._normalize(value)
        if normalized:
            self._seen_transversal_names.add(normalized)
            self.cache_stats["unique_transversal_names"] = len(self._seen_transversal_names)
        key = self._transversal_key(main_street, value, reference)
        if key in self._transversal_cache:
            self.cache_stats["transversal_cache_hits"] += 1
            return self._copy_transversal(self._transversal_cache[key])
        self.cache_stats["transversal_cache_misses"] += 1
        if not normalized:
            result = _TransversalResult(normalized=normalized, status="CAMPO_VAZIO")
            self._transversal_cache[key] = result
            return self._copy_transversal(result)
        if any(term in normalized for term in SPECIAL_REFERENCE_TERMS):
            result = _TransversalResult(normalized=normalized, status="CAMPO_ESPECIAL")
            self._transversal_cache[key] = result
            return self._copy_transversal(result)

        alias = self.aliases.get(normalized)
        exact_street = alias.resolved_norm if alias else normalized if normalized in self.graph.street_segments else None
        if exact_street:
            points, _ = self._reference_intersections(main_street, exact_street)
            result = _TransversalResult(
                normalized=normalized,
                confidence="HIGH",
                status="ALIAS" if alias else "EXATA",
                method="ALIAS" if alias else "EXATO",
                candidate=exact_street,
                distance_m=self._street_distance(exact_street, reference),
                intersection_status="INTERSECCAO_CONFIRMADA" if points else "SEM_INTERSECAO",
                intersects=bool(points),
                intersection_count=len(points),
                alternatives=[exact_street],
            )
            self._transversal_cache[key] = result
            return self._copy_transversal(result)

        transversal_lexical_started = time.perf_counter()
        lexical = [
            candidate for candidate in self._fuzzy_candidates(normalized)
            if candidate.lexical_score >= self.config.transversal_fuzzy_min_score
            and not self._critical_missing_tokens(candidate)
        ][:max(1, min(5, self.config.transversal_candidate_limit))]
        self.stage_seconds["transversal_lexical"] += time.perf_counter() - transversal_lexical_started
        alternatives = [candidate.street_norm for candidate in lexical]
        scored = []
        for candidate in lexical:
            points, _ = self._reference_intersections(main_street, candidate.street_norm)
            distance = self._street_distance(candidate.street_norm, reference)
            geo_score = self._distance_score(distance) if distance is not None else 0.0
            intersection_score = 100.0 if points else 0.0
            contextual_score = (
                candidate.lexical_score * 0.55
                + geo_score * 0.20
                + intersection_score * 0.25
            )
            scored.append((bool(points), contextual_score, candidate.street_norm, distance, len(points)))
        scored.sort(key=lambda item: (-int(item[0]), -item[1], item[2]))
        confirmed = [item for item in scored if item[0]]
        if confirmed:
            selected = confirmed[0]
            second = confirmed[1] if len(confirmed) > 1 else None
            margin_top2 = selected[1] - second[1] if second else None
            if margin_top2 is not None and margin_top2 < self.config.medium_margin:
                result = _TransversalResult(
                    normalized=normalized,
                    confidence="LOW",
                    status="AMBIGUA",
                    method="FUZZY",
                    intersection_status="INTERSECCAO_CONFIRMADA",
                    alternatives=alternatives,
                    margin_top2=margin_top2,
                    review_reasons=["TRANSVERSAL_AMBIGUA"],
                )
            else:
                result = _TransversalResult(
                    normalized=normalized,
                    confidence="HIGH" if selected[1] >= self.config.high_score else "MEDIUM",
                    status="FUZZY_CONFIRMADA_POR_INTERSECAO",
                    method="FUZZY",
                    candidate=selected[2],
                    distance_m=selected[3],
                    intersection_status="INTERSECCAO_CONFIRMADA",
                    intersects=True,
                    intersection_count=selected[4],
                    margin_top2=margin_top2,
                    alternatives=alternatives,
                )
        else:
            result = _TransversalResult(
                normalized=normalized,
                confidence="UNRESOLVED",
                status="NAO_RESOLVIDA",
                method="FUZZY",
                candidate=None,
                intersection_status="SEM_INTERSECAO" if lexical else "INDISPONIVEL",
                intersects=None,
                alternatives=alternatives,
                review_reasons=["TRANSVERSAL_NAO_RESOLVIDA"],
            )
        self._transversal_cache[key] = result
        return self._copy_transversal(result)

    def _reference_intersections(self, main_street: str, other_street: str) -> tuple[list[Any], str | None]:
        key = (main_street, other_street)
        if key in self._intersection_cache:
            self.cache_stats["intersection_cache_hits"] += 1
            return self._intersection_cache[key]
        official_key = tuple(sorted((main_street, other_street)))
        official_cache = getattr(self.graph, "intersection_cache", {})
        if official_key in official_cache:
            raw_points = official_cache.get(official_key) or ()
            points = [Point(x, y) for x, y in raw_points] if Point is not None else []
            result = (points, None)
            self._intersection_cache[key] = result
            self.cache_stats["intersection_cache_hits"] += 1
            self.cache_stats["official_intersection_cache_hits"] += 1
            return result
        self.cache_stats["intersection_cache_misses"] += 1
        self.cache_stats["intersection_queries"] += 1
        other_ids = set(self.graph.street_segments.get(other_street, ()))
        points = []
        for identifier in self.graph.street_segments.get(main_street, ()):
            segment = self.graph.segments[identifier]
            candidate_ids = self.graph._candidate_ids(segment.geometry) if getattr(self.graph, "_tree", None) is not None else other_ids
            for other_id in candidate_ids:
                if other_id not in other_ids or other_id == identifier:
                    continue
                other = self.graph.segments[other_id]
                if not segment.geometry.intersects(other.geometry):
                    continue
                points.extend(_intersection_points(segment.geometry.intersection(other.geometry)))
        points = _unique_points(points)
        component_connected = None
        if points and hasattr(self.graph, "_node_for_intersection"):
            nodes = [self._node_for_point(main_street, point) for point in points]
            nodes = [node for node in nodes if node is not None]
            if nodes:
                _, component = self.graph._component(main_street, nodes[0])
                component_connected = bool(component)
        result = (points, "SIM" if component_connected else "NAO" if component_connected is False else None)
        self._intersection_cache[key] = result
        return result

    def _node_for_point(self, street: str, point: Any) -> Any:
        """Obtém um nó mesmo quando a interseção cai no meio de uma aresta."""
        if hasattr(self.graph, "_node_for_intersection"):
            node = self.graph._node_for_intersection(street, point)
            if node is not None:
                return node
        candidates = []
        for identifier in self.graph.street_segments.get(street, ()):
            segment = self.graph.segments[identifier]
            if segment.geometry.distance(point) <= 1e-6:
                candidates.extend((segment.start, segment.end))
        return candidates[0] if candidates else None

    def _distance_score(self, distance: float) -> float:
        config = self.config
        if distance <= config.distance_very_strong_m:
            return 100.0
        if distance <= config.distance_strong_m:
            return 80.0 - 20.0 * (distance - config.distance_very_strong_m) / max(config.distance_strong_m - config.distance_very_strong_m, 1)
        if distance <= config.distance_moderate_m:
            return 60.0 - 25.0 * (distance - config.distance_strong_m) / max(config.distance_moderate_m - config.distance_strong_m, 1)
        if distance <= config.distance_weak_m:
            return 35.0 - 35.0 * (distance - config.distance_moderate_m) / max(config.distance_weak_m - config.distance_moderate_m, 1)
        return 0.0

    def _evaluate_candidate(
        self,
        candidate: StreetCandidate,
        *,
        original: str,
        reference: Any,
        de: Any,
        ate: Any,
        evaluate_transversals: bool = True,
    ) -> StreetCandidate:
        graph_segments = [self.graph.segments[item] for item in self.graph.street_segments.get(candidate.street_norm, ())]
        candidate.evidence.append(f"segmentos={candidate.segment_count}")
        if len(candidate.codlogs) > 1:
            candidate.evidence.append(f"codlogs_multiplos={','.join(candidate.codlogs)}")
        if candidate.missing_tokens:
            candidate.evidence.append(f"tokens_ausentes={','.join(candidate.missing_tokens)}")
        if candidate.extra_tokens:
            candidate.evidence.append(f"tokens_extras={','.join(candidate.extra_tokens)}")
        candidate.evidence.append(f"cobertura_tokens={candidate.token_coverage_original:.3f}")

        geographic_started = time.perf_counter()
        if reference is not None and graph_segments:
            candidate.distance_m = self._street_distance(candidate.street_norm, reference)
            candidate.geographic_score = self._distance_score(candidate.distance_m)
            candidate.evidence.append(f"distancia_m={candidate.distance_m:.2f}")
        else:
            candidate.evidence.append("evidencia_geografica=indisponivel")
        self.stage_seconds["geographic"] += time.perf_counter() - geographic_started

        if not evaluate_transversals:
            candidate.de_resolution_confidence = "SKIPPED"
            candidate.ate_resolution_confidence = "SKIPPED"
            candidate.de_resolution_status = "NAO_AVALIADO"
            candidate.ate_resolution_status = "NAO_AVALIADO"
            candidate.de_intersection_status = "NAO_AVALIADA"
            candidate.ate_intersection_status = "NAO_AVALIADA"
            candidate.intersects_de = None
            candidate.intersects_ate = None
            available = [(candidate.lexical_score, self.config.lexical_weight)]
            if candidate.geographic_score is not None:
                available.append((candidate.geographic_score, self.config.geographic_weight))
            weight_total = sum(weight for _, weight in available)
            candidate.final_score = sum(score * weight for score, weight in available) / max(weight_total, 1e-9)
            candidate.final_score = max(0.0, min(100.0, candidate.final_score))
            return candidate

        intersections_started = time.perf_counter()
        transversal_started = time.perf_counter()
        for field_name, value in (("de", de), ("ate", ate)):
            transversal = self._resolve_transversal(
                value,
                main_street=candidate.street_norm,
                reference=reference,
            )
            if field_name == "de":
                candidate.de_resolution_confidence = transversal.confidence
                candidate.de_resolution_status = transversal.status
                candidate.de_candidate = transversal.candidate
                candidate.de_distance_m = transversal.distance_m
                candidate.de_intersection_status = transversal.intersection_status
                candidate.de_alternatives = transversal.alternatives
                candidate.intersects_de = transversal.intersects
                candidate.intersection_count_de = transversal.intersection_count
                candidate.de_score = (
                    100.0 if transversal.intersection_status == "INTERSECCAO_CONFIRMADA"
                    else 0.0 if transversal.status in {"EXATA", "ALIAS"} and transversal.intersects is False
                    else None
                )
            else:
                candidate.ate_resolution_confidence = transversal.confidence
                candidate.ate_resolution_status = transversal.status
                candidate.ate_candidate = transversal.candidate
                candidate.ate_distance_m = transversal.distance_m
                candidate.ate_intersection_status = transversal.intersection_status
                candidate.ate_alternatives = transversal.alternatives
                candidate.intersects_ate = transversal.intersects
                candidate.intersection_count_ate = transversal.intersection_count
                candidate.ate_score = (
                    100.0 if transversal.intersection_status == "INTERSECCAO_CONFIRMADA"
                    else 0.0 if transversal.status in {"EXATA", "ALIAS"} and transversal.intersects is False
                    else None
                )
            candidate.evidence.append(
                f"{field_name}_status={transversal.status};"
                f"candidato={transversal.candidate or '-'};"
                f"intersecao={transversal.intersection_status}"
            )
        self.stage_seconds["intersections"] += time.perf_counter() - intersections_started
        self.stage_seconds["transversal_resolution"] += time.perf_counter() - transversal_started

        if candidate.intersects_de and candidate.intersects_ate:
            de_points, _ = self._reference_intersections(candidate.street_norm, candidate.de_candidate or "")
            ate_points, _ = self._reference_intersections(candidate.street_norm, candidate.ate_candidate or "")
            nodes_de = [self._node_for_point(candidate.street_norm, point) for point in de_points]
            nodes_ate = [self._node_for_point(candidate.street_norm, point) for point in ate_points]
            nodes_de = [node for node in nodes_de if node is not None]
            nodes_ate = [node for node in nodes_ate if node is not None]
            if nodes_de and nodes_ate:
                _, component = self.graph._component(candidate.street_norm, nodes_de[0])
                candidate.component_connected = bool(component and nodes_ate[0] in component)
                candidate.evidence.append(
                    f"componente_conectado={'SIM' if candidate.component_connected else 'NAO'}"
                )

        available = [(candidate.lexical_score, self.config.lexical_weight)]
        if candidate.geographic_score is not None:
            available.append((candidate.geographic_score, self.config.geographic_weight))
        if candidate.de_score is not None:
            available.append((candidate.de_score, self.config.de_weight))
        if candidate.ate_score is not None:
            available.append((candidate.ate_score, self.config.ate_weight))
        weight_total = sum(weight for _, weight in available)
        candidate.final_score = sum(score * weight for score, weight in available) / max(weight_total, 1e-9)
        candidate.final_score = max(0.0, min(100.0, candidate.final_score))
        return candidate

    def _reference_point(self, context: StreetResolutionContext) -> Any:
        if context.reference is not None:
            return context.reference
        lat = _safe_float(context.latitude)
        lon = _safe_float(context.longitude)
        if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return None
        if self._transformer is None or Point is None:
            return None
        try:
            x, y = self._transformer.transform(lon, lat)
            return Point(x, y)
        except (TypeError, ValueError):
            return None

    def _route_context(self, context: StreetResolutionContext, candidate: StreetCandidate | None) -> dict[str, Any]:
        context_key = (
            candidate.street_norm if candidate else None,
            candidate.de_candidate if candidate else self._normalize(context.de_original),
            candidate.ate_candidate if candidate else self._normalize(context.ate_original),
            candidate.de_resolution_status if candidate else "UNRESOLVED",
            candidate.ate_resolution_status if candidate else "UNRESOLVED",
            candidate.component_connected if candidate else None,
        )
        if context_key in self._context_cache:
            self.cache_stats["context_cache_hits"] += 1
            cached = self._context_cache[context_key]
            return {"status": cached["status"], "requires_review": cached["requires_review"], "reasons": list(cached["reasons"])}
        self.cache_stats["context_cache_misses"] += 1
        if candidate is None:
            reasons = []
            if self._normalize(context.de_original):
                reasons.append("DE_NAO_RESOLVIDO")
            if self._normalize(context.ate_original):
                reasons.append("ATE_NAO_RESOLVIDO")
            result = {
                "status": "TRANSVERSALS_UNRESOLVED",
                "requires_review": bool(reasons),
                "reasons": reasons,
            }
            self._context_cache[context_key] = result
            return result

        de_status = candidate.de_resolution_status
        ate_status = candidate.ate_resolution_status
        de_present = de_status not in {"CAMPO_VAZIO", "CAMPO_ESPECIAL"}
        ate_present = ate_status not in {"CAMPO_VAZIO", "CAMPO_ESPECIAL"}
        de_confirmed = candidate.intersects_de is True
        ate_confirmed = candidate.intersects_ate is True
        reasons = []

        if de_status == "NAO_RESOLVIDA":
            reasons.append("DE_NAO_RESOLVIDO")
        elif de_present and de_status not in {"NAO_RESOLVIDA", "AMBIGUA"} and candidate.intersection_count_de == 0:
            reasons.append("SEM_INTERSECAO_DE")
        if ate_status == "NAO_RESOLVIDA":
            reasons.append("ATE_NAO_RESOLVIDO")
        elif ate_present and ate_status not in {"NAO_RESOLVIDA", "AMBIGUA"} and candidate.intersection_count_ate == 0:
            reasons.append("SEM_INTERSECAO_ATE")
        if de_status == "AMBIGUA":
            reasons.append("TRANSVERSAL_AMBIGUA_DE")
        if ate_status == "AMBIGUA":
            reasons.append("TRANSVERSAL_AMBIGUA_ATE")
        if candidate.component_connected is False:
            reasons.append("COMPONENTE_DESCONECTADO")

        if candidate.component_connected is False:
            status = "CONTEXT_CONTRADICTORY"
        elif de_confirmed and ate_confirmed:
            status = "BOTH_INTERSECTIONS_CONFIRMED"
        elif de_status == "CAMPO_ESPECIAL" or ate_status == "CAMPO_ESPECIAL":
            status = "SPECIAL_ROUTE_CONTEXT"
        elif de_confirmed and not ate_present:
            status = "DE_CONFIRMED_ATE_UNAVAILABLE"
        elif ate_confirmed and not de_present:
            status = "ATE_CONFIRMED_DE_UNAVAILABLE"
        elif de_confirmed and ate_status == "NAO_RESOLVIDA":
            status = "DE_CONFIRMED_ATE_NOT_FOUND"
        elif ate_confirmed and de_status == "NAO_RESOLVIDA":
            status = "ATE_CONFIRMED_DE_NOT_FOUND"
        elif not de_present and not ate_present:
            status = "SPECIAL_ROUTE_CONTEXT"
        elif reasons:
            status = "INTERSECTIONS_NOT_CONFIRMED"
        else:
            status = "TRANSVERSALS_UNRESOLVED"
        result = {
            "status": status,
            "requires_review": bool(reasons),
            "reasons": list(dict.fromkeys(reasons)),
        }
        self._context_cache[context_key] = result
        return result

    @staticmethod
    def _populate_resolution_context(resolution: StreetResolution, candidate: StreetCandidate | None) -> None:
        if candidate is None:
            return
        resolution.de_resolution_confidence = candidate.de_resolution_confidence
        resolution.ate_resolution_confidence = candidate.ate_resolution_confidence
        resolution.de_resolution_status = candidate.de_resolution_status
        resolution.ate_resolution_status = candidate.ate_resolution_status
        resolution.de_candidate = candidate.de_candidate
        resolution.ate_candidate = candidate.ate_candidate
        resolution.de_distance_m = candidate.de_distance_m
        resolution.ate_distance_m = candidate.ate_distance_m
        resolution.de_intersection_status = candidate.de_intersection_status
        resolution.ate_intersection_status = candidate.ate_intersection_status
        resolution.de_intersection_count = candidate.intersection_count_de
        resolution.ate_intersection_count = candidate.intersection_count_ate
        resolution.de_alternatives_json = json.dumps(candidate.de_alternatives, ensure_ascii=False)
        resolution.ate_alternatives_json = json.dumps(candidate.ate_alternatives, ensure_ascii=False)

    def _primary_candidates(self, normalized: str, codlog: str) -> tuple[str, list[StreetCandidate]]:
        key = (normalized, codlog)
        if key in self._primary_cache:
            method, values = self._primary_cache[key]
            self.cache_stats["lexical_cache_hits"] += 1
            return method, [self._copy_candidate(value) for value in values]
        alias = self.aliases.get(normalized)
        if alias is not None:
            candidates = [self._lexical_candidate(normalized, alias.resolved_norm)]
            method = "ALIAS"
        elif codlog and codlog in self.graph.codlog_to_street:
            candidates = [self._lexical_candidate(normalized, self.graph.codlog_to_street[codlog])]
            method = "CODLOG"
        elif normalized and normalized in self.graph.street_segments:
            candidates = [self._lexical_candidate(normalized, normalized)]
            method = "EXATO"
        else:
            candidates = self._fuzzy_candidates(normalized)
            method = "FUZZY" if candidates else "SEM_RESOLUCAO"
            if method == "FUZZY":
                self.cache_stats["fuzzy_primary_resolutions"] += 1
        self.cache_stats["candidates_scored"] += len(candidates)
        self._primary_cache[key] = (method, [self._copy_candidate(value) for value in candidates])
        return method, [self._copy_candidate(value) for value in candidates]

    def _resolve_context_optimized(
        self,
        context: StreetResolutionContext,
        *,
        evaluate_route_context: bool = True,
    ) -> StreetResolution:
        started = time.perf_counter()
        key = self._cache_key(context) + (bool(evaluate_route_context),)
        if context.reference is None and key in self._resolution_cache:
            self.cache_hits += 1
            self.cache_stats["context_cache_hits"] += 1
            return self._resolution_cache[key]
        self.cache_misses += 1
        self.cache_stats["context_cache_misses"] += 1
        original = context.name_used
        normalized = self._normalize(original)
        codlog = _text(context.codlog)
        invalid_alias = normalized in self.invalid_aliases
        codlog_invalid = bool(codlog and codlog not in self.graph.codlog_to_street)
        primary_started = time.perf_counter()
        method, candidates = self._primary_candidates(normalized, codlog)
        if method in {"ALIAS", "CODLOG", "EXATO"}:
            self.stage_seconds["primary_exact"] += time.perf_counter() - primary_started
            self.cache_stats["exact_fast_paths"] += 1
        else:
            self.stage_seconds["primary_fuzzy"] += time.perf_counter() - primary_started

        reference = self._reference_point(context)
        for candidate in candidates:
            self._evaluate_candidate(
                candidate,
                original=normalized,
                reference=reference,
                de=context.de_original,
                ate=context.ate_original,
                evaluate_transversals=False,
            )
        candidates.sort(key=lambda value: (-value.final_score, -value.lexical_score, value.street_norm))
        primary_top = candidates[0] if candidates else None
        context_indexes: set[int] = set()
        if evaluate_route_context and primary_top is not None:
            context_indexes.add(0)
            if method == "FUZZY" and len(candidates) > 1:
                primary_margin = primary_top.final_score - candidates[1].final_score
                if (
                    primary_margin < self.config.ambiguous_margin + self.config.medium_margin
                    or primary_top.final_score < self.config.high_score
                    or primary_top.distance_m is None
                ):
                    limit = max(2, int(self.config.context_candidate_limit))
                    context_indexes.update(range(min(len(candidates), limit)))
        if evaluate_route_context:
            for index in sorted(context_indexes):
                self._evaluate_candidate(
                    candidates[index],
                    original=normalized,
                    reference=reference,
                    de=context.de_original,
                    ate=context.ate_original,
                    evaluate_transversals=True,
                )
            self.cache_stats["transversal_evaluations_skipped"] += max(0, len(candidates) - len(context_indexes))
        else:
            self.cache_stats["transversal_evaluations_skipped"] += len(candidates)

        if method in {"ALIAS", "CODLOG", "EXATO"}:
            for candidate in candidates:
                candidate.final_score = 100.0
        candidates.sort(key=lambda value: (-value.final_score, -value.lexical_score, value.street_norm))
        top = candidates[0] if candidates else None
        second = candidates[1] if len(candidates) > 1 else None
        margin = top.final_score - second.final_score if top and second else None
        resolution = StreetResolution(
            original=original,
            normalized=normalized,
            candidates=candidates,
            margin_top2=margin,
            invalid_alias=invalid_alias,
            codlog_invalid=codlog_invalid,
        )
        street_reasons: list[str] = []
        if top is None or top.final_score < self.config.fuzzy_min_score:
            resolution.reason = "Nenhum candidato atingiu o score minimo configurado."
            street_reasons.append("NENHUM_CANDIDATO_ACEITAVEL")
        else:
            resolution.resolved_street = top.street_norm
            resolution.resolved_codlog = codlog if method == "CODLOG" else top.codlog
            resolution.method = method
            resolution.final_score = top.final_score
            contextual_positive = (
                (top.distance_m is not None and top.distance_m <= self.config.distance_moderate_m)
                or top.intersects_de is True
                or top.intersects_ate is True
            )
            if method in {"ALIAS", "CODLOG", "EXATO"}:
                resolution.confidence = "HIGH"
                resolution.reason = f"{method} validamente confirmado no indice GeoSampa."
            elif (
                top.final_score >= self.config.high_score
                and margin is not None
                and margin >= self.config.ambiguous_margin
                and contextual_positive
                and not top.incomplete
            ):
                resolution.confidence = "HIGH"
                resolution.reason = "Score lexical/contextual alto, margem suficiente e evidencia contextual positiva."
            elif top.final_score >= self.config.medium_score and (
                contextual_positive or (margin is not None and margin >= self.config.medium_margin)
            ):
                resolution.confidence = "MEDIUM"
                resolution.reason = "Score bom com evidencia contextual parcial ou margem moderada."
            else:
                resolution.confidence = "LOW"
                resolution.reason = "Candidato fuzzy razoavel, mas sem margem ou evidencia suficiente."
                street_reasons.append("CONFIANCA_BAIXA")
            if margin is not None and margin < self.config.ambiguous_margin:
                street_reasons.append("MARGEM_TOP2_BAIXA")
            if top.incomplete:
                street_reasons.append("CANDIDATO_INCOMPLETO")
            if top.distance_m is not None and top.distance_m > self.config.distance_weak_m:
                street_reasons.append("DISTANCIA_INCOMPATIVEL")
        if invalid_alias:
            street_reasons.append("ALIAS_INVALIDO")
        if codlog_invalid:
            street_reasons.append("CODLOG_INVALIDO")
        street_reasons = list(dict.fromkeys(street_reasons))
        resolution.street_confidence = resolution.confidence
        resolution.street_review_reasons = street_reasons
        resolution.street_requires_review = bool(street_reasons)
        resolution.review_reasons = street_reasons
        self._populate_resolution_context(resolution, top if resolution.resolved_street else None)
        if not resolution.resolved_street and evaluate_route_context:
            if self._normalize(context.de_original):
                resolution.de_resolution_status = "NAO_RESOLVIDA"
                resolution.de_resolution_confidence = "UNRESOLVED"
            if self._normalize(context.ate_original):
                resolution.ate_resolution_status = "NAO_RESOLVIDA"
                resolution.ate_resolution_confidence = "UNRESOLVED"
        if evaluate_route_context:
            route_started = time.perf_counter()
            route_context = self._route_context(context, top if resolution.resolved_street else None)
            self.stage_seconds["route_context"] += time.perf_counter() - route_started
            resolution.route_context_status = route_context["status"]
            resolution.route_requires_review = route_context["requires_review"]
            resolution.route_review_reasons = route_context["reasons"]
        else:
            resolution.route_context_status = "SKIPPED_ROUTE_CONTEXT"
            resolution.route_requires_review = False
            resolution.route_review_reasons = []
        if context.reference is None:
            self._resolution_cache[key] = resolution
        self.stage_seconds["street_resolution"] += time.perf_counter() - started
        return resolution

    def resolve_context(
        self,
        context: StreetResolutionContext,
        *,
        evaluate_route_context: bool = True,
    ) -> StreetResolution:
        return self._resolve_context_optimized(context, evaluate_route_context=evaluate_route_context)
        resolution_started = time.perf_counter()
        key = self._cache_key(context)
        if context.reference is None and key in self._resolution_cache:
            self.cache_hits += 1
            return self._resolution_cache[key]
        self.cache_misses += 1
        original = context.name_used
        normalized = self._normalize(original)
        codlog = _text(context.codlog)
        invalid_alias = normalized in self.invalid_aliases
        codlog_invalid = bool(codlog and codlog not in self.graph.codlog_to_street)
        candidates: list[StreetCandidate] = []
        method = "SEM_RESOLUCAO"

        alias = self.aliases.get(normalized)
        if alias is not None:
            candidates = [self._lexical_candidate(normalized, alias.resolved_norm)]
            method = "ALIAS"
        elif codlog and codlog in self.graph.codlog_to_street:
            candidates = [self._lexical_candidate(normalized, self.graph.codlog_to_street[codlog])]
            method = "CODLOG"
        elif normalized and normalized in self.graph.street_segments:
            candidates = [self._lexical_candidate(normalized, normalized)]
            method = "EXATO"
        else:
            candidates = self._fuzzy_candidates(normalized)
            method = "FUZZY" if candidates else "SEM_RESOLUCAO"

        reference = self._reference_point(context)
        for candidate in candidates:
            self._evaluate_candidate(
                candidate,
                original=normalized,
                reference=reference,
                de=context.de_original,
                ate=context.ate_original,
            )
            if method in {"ALIAS", "CODLOG", "EXATO"}:
                candidate.final_score = 100.0

        candidates.sort(key=lambda item: (-item.final_score, -item.lexical_score, item.street_norm))
        top = candidates[0] if candidates else None
        second = candidates[1] if len(candidates) > 1 else None
        margin = top.final_score - second.final_score if top and second else None
        resolution = StreetResolution(
            original=original,
            normalized=normalized,
            candidates=candidates,
            margin_top2=margin,
            invalid_alias=invalid_alias,
            codlog_invalid=codlog_invalid,
        )
        street_reasons = []
        if top is None or top.final_score < self.config.fuzzy_min_score:
            resolution.reason = "Nenhum candidato atingiu o score mínimo configurado."
            street_reasons.append("NENHUM_CANDIDATO_ACEITAVEL")
        else:
            resolution.resolved_street = top.street_norm
            resolution.resolved_codlog = codlog if method == "CODLOG" else top.codlog
            resolution.method = method
            resolution.final_score = top.final_score
            contextual_positive = (
                (top.distance_m is not None and top.distance_m <= self.config.distance_moderate_m)
                or top.intersects_de is True
                or top.intersects_ate is True
            )
            if method in {"ALIAS", "CODLOG", "EXATO"}:
                resolution.confidence = "HIGH"
                resolution.reason = f"{method} validamente confirmado no índice GeoSampa."
            elif (
                top.final_score >= self.config.high_score
                and margin is not None
                and margin >= self.config.ambiguous_margin
                and contextual_positive
                and not top.incomplete
            ):
                resolution.confidence = "HIGH"
                resolution.reason = "Score lexical/contextual alto, margem suficiente e evidência contextual positiva."
            elif top.final_score >= self.config.medium_score and (contextual_positive or (margin is not None and margin >= self.config.medium_margin)):
                resolution.confidence = "MEDIUM"
                resolution.reason = "Score bom com evidência contextual parcial ou margem moderada."
            else:
                resolution.confidence = "LOW"
                resolution.reason = "Candidato fuzzy razoável, mas sem margem ou evidência suficiente para confiança maior."
                street_reasons.append("CONFIANCA_BAIXA")

            if margin is not None and margin < self.config.ambiguous_margin:
                street_reasons.append("MARGEM_TOP2_BAIXA")
            if top.incomplete:
                street_reasons.append("CANDIDATO_INCOMPLETO")
            if top.distance_m is not None and top.distance_m > self.config.distance_weak_m:
                street_reasons.append("DISTANCIA_INCOMPATIVEL")
        if invalid_alias:
            street_reasons.append("ALIAS_INVALIDO")
            resolution.reason += f" Alias inválido: {self.invalid_aliases[normalized]}."
        if codlog_invalid:
            street_reasons.append("CODLOG_INVALIDO")
            resolution.reason += f" CODLOG informado não existe no índice: {codlog}."
        street_reasons = list(dict.fromkeys(street_reasons))
        resolution.street_confidence = resolution.confidence
        resolution.street_review_reasons = street_reasons
        resolution.street_requires_review = bool(street_reasons)
        resolution.review_reasons = street_reasons
        self._populate_resolution_context(resolution, top if resolution.resolved_street else None)
        if not resolution.resolved_street:
            if self._normalize(context.de_original):
                resolution.de_resolution_status = "NAO_RESOLVIDA"
                resolution.de_resolution_confidence = "UNRESOLVED"
            if self._normalize(context.ate_original):
                resolution.ate_resolution_status = "NAO_RESOLVIDA"
                resolution.ate_resolution_confidence = "UNRESOLVED"
        route_started = time.perf_counter()
        route_context = self._route_context(context, top if resolution.resolved_street else None)
        self.stage_seconds["route_context"] += time.perf_counter() - route_started
        resolution.route_context_status = route_context["status"]
        resolution.route_requires_review = route_context["requires_review"]
        resolution.route_review_reasons = route_context["reasons"]
        if context.reference is None:
            self._resolution_cache[key] = resolution
        self.stage_seconds["street_resolution"] += time.perf_counter() - resolution_started
        return resolution

    def resolve(
        self,
        original: Any,
        *,
        codlog: Any = "",
        latitude: Any = None,
        longitude: Any = None,
        de: Any = "",
        ate: Any = "",
        reference: Any = None,
        evaluate_route_context: bool = True,
    ) -> StreetResolution:
        context = StreetResolutionContext(
            via_original=_text(original),
            codlog=_text(codlog),
            latitude=_safe_float(latitude),
            longitude=_safe_float(longitude),
            de_original=_text(de),
            ate_original=_text(ate),
            reference=reference,
        )
        return self.resolve_context(context, evaluate_route_context=evaluate_route_context)


def _current_resolution(graph: RoadGraph, context: StreetResolutionContext) -> tuple[str | None, float, str]:
    try:
        street, score, method = graph.resolve_street(context.name_used, codlog=context.codlog)
        return street, float(score), method
    except (AttributeError, TypeError, ValueError):
        return None, 0.0, "SEM_RESOLUCAO"


def _candidate_to_dict(candidate: StreetCandidate) -> dict[str, Any]:
    values = asdict(candidate)
    values["token_coverage"] = candidate.token_coverage
    values["incomplete"] = candidate.incomplete
    return values


def _context_from_row(row: Mapping[str, Any]) -> StreetResolutionContext:
    return StreetResolutionContext(
        via_original=_text(row.get("via")),
        logradouro_geosampa_original=_text(row.get("logradouro_geosampa")),
        de_original=_text(row.get("de")),
        ate_original=_text(row.get("ate")),
        codlog=_text(row.get("codlog") or row.get("cd_codlog")),
        latitude=_safe_float(row.get("latitude")),
        longitude=_safe_float(row.get("longitude")),
    )


def _dataframe_signature(df: pd.DataFrame) -> str:
    columns = [
        column for column in (
            "id", "numero_processo", "via", "logradouro_geosampa", "codlog",
            "cd_codlog", "de", "ate", "latitude", "longitude",
        ) if column in df.columns
    ]
    records = []
    for _, row in df[columns].iterrows():
        records.append([_text(row.get(column)) for column in columns])
    return _json_fingerprint({"columns": columns, "records": records})


def _checkpoint_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as stream:
        pickle.dump(dict(payload), stream, protocol=pickle.HIGHEST_PROTOCOL)
    _atomic_replace(temporary, path)


def _checkpoint_read(path: Path) -> Mapping[str, Any] | None:
    try:
        with path.open("rb") as stream:
            payload = pickle.load(stream)
        return payload if isinstance(payload, Mapping) else None
    except (OSError, EOFError, pickle.PickleError, AttributeError, KeyError, ValueError, TypeError, ImportError):
        return None


def _atomic_replace(temporary: Path, target: Path) -> None:
    try:
        os.replace(temporary, target)
        return
    except PermissionError:
        # OneDrive/antivirus pode manter o destino aberto por alguns ms.
        # O fallback continua escrevendo apenas o artefato diagnóstico.
        with temporary.open("rb") as source, target.open("wb") as destination:
            shutil.copyfileobj(source, destination)
        try:
            temporary.unlink()
        except OSError:
            pass


def _atomic_write_dataframe(frame: pd.DataFrame, path: Path) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    frame.to_csv(temporary, index=False, encoding="utf-8-sig")
    _atomic_replace(temporary, path)


def _audit_row(
    row: Mapping[str, Any],
    index: Any,
    current: tuple[str | None, float, str],
    resolution: StreetResolution,
) -> dict[str, Any]:
    context = _context_from_row(row)
    top = resolution.candidates[0] if resolution.candidates else None
    candidate_2 = resolution.candidates[1] if len(resolution.candidates) > 1 else None
    candidate_3 = resolution.candidates[2] if len(resolution.candidates) > 2 else None
    current_street, current_score, current_method = current
    diverges = current_street != resolution.resolved_street
    street_reasons = list(resolution.street_review_reasons or resolution.review_reasons)
    route_reasons = list(resolution.route_review_reasons)
    strong_divergence = (
        resolution.confidence == "HIGH"
        and bool(top)
        and not top.incomplete
        and (
            resolution.method in {"EXATO", "ALIAS", "CODLOG"}
            or top.distance_m is not None and top.distance_m <= 300
            or top.intersects_de is True
            or top.intersects_ate is True
        )
    )
    if diverges and not strong_divergence:
        street_reasons.append("DIVERGE_RESOLUCAO_ATUAL")
    if current_street and current_street in {candidate.street_norm for candidate in resolution.candidates}:
        current_candidate = next(candidate for candidate in resolution.candidates if candidate.street_norm == current_street)
        if current_candidate.incomplete and not strong_divergence:
            street_reasons.append("ATUAL_POSSIVELMENTE_INCOMPLETO")
    street_reasons = list(dict.fromkeys(street_reasons))
    route_reasons = list(dict.fromkeys(route_reasons))
    street_requires_review = bool(street_reasons)
    route_requires_review = bool(route_reasons)
    reasons = list(dict.fromkeys(street_reasons + route_reasons))
    return {
        "id": row.get("id", index),
        "numero_processo": row.get("numero_processo"),
        "via_original": context.via_original,
        "logradouro_geosampa_original": context.logradouro_geosampa_original,
        "nome_usado_atualmente": context.name_used,
        "nome_normalizado": resolution.normalized,
        "codlog_informado": context.codlog,
        "latitude": context.latitude,
        "longitude": context.longitude,
        "de_original": context.de_original,
        "ate_original": context.ate_original,
        "resolucao_atual": current_street,
        "metodo_atual": current_method,
        "score_atual": current_score,
        "candidato_recomendado": resolution.resolved_street,
        "codlog_recomendado": resolution.resolved_codlog,
        "metodo_recomendado": resolution.method,
        "confianca": resolution.confidence,
        "street_confidence": resolution.street_confidence,
        "street_requires_review": street_requires_review,
        "street_review_reasons": ";".join(street_reasons),
        "score_final": resolution.final_score,
        "margem_top2": resolution.margin_top2,
        "distance_m": top.distance_m if top else None,
        "intersects_de": top.intersects_de if top else None,
        "intersects_ate": top.intersects_ate if top else None,
        "component_connected": top.component_connected if top else None,
        "token_coverage": top.token_coverage if top else None,
        "length_ratio": top.length_ratio if top else None,
        "motivo_recomendacao": resolution.reason,
        "route_context_status": resolution.route_context_status,
        "route_requires_review": route_requires_review,
        "route_review_reasons": ";".join(route_reasons),
        "de_resolution_confidence": resolution.de_resolution_confidence,
        "ate_resolution_confidence": resolution.ate_resolution_confidence,
        "de_resolution_status": resolution.de_resolution_status,
        "ate_resolution_status": resolution.ate_resolution_status,
        "de_candidate": resolution.de_candidate,
        "ate_candidate": resolution.ate_candidate,
        "de_distance_m": resolution.de_distance_m,
        "ate_distance_m": resolution.ate_distance_m,
        "de_intersection_status": resolution.de_intersection_status,
        "ate_intersection_status": resolution.ate_intersection_status,
        "de_intersection_count": resolution.de_intersection_count,
        "ate_intersection_count": resolution.ate_intersection_count,
        "de_alternatives_json": resolution.de_alternatives_json,
        "ate_alternatives_json": resolution.ate_alternatives_json,
        "candidato_2": candidate_2.street_norm if candidate_2 else None,
        "score_candidato_2": candidate_2.final_score if candidate_2 else None,
        "distancia_candidato_2_m": candidate_2.distance_m if candidate_2 else None,
        "candidato_3": candidate_3.street_norm if candidate_3 else None,
        "score_candidato_3": candidate_3.final_score if candidate_3 else None,
        "distancia_candidato_3_m": candidate_3.distance_m if candidate_3 else None,
        "diverge_resolucao_atual": diverges,
        "requer_revisao": bool(street_requires_review or route_requires_review),
        "motivos_revisao": ";".join(reasons),
        "alternativas_json": json.dumps(
            [_candidate_to_dict(candidate) for candidate in resolution.candidates],
            ensure_ascii=False,
            sort_keys=True,
        ),
    }


def _write_normalization_candidates(
    df_recape: pd.DataFrame,
    output_path: str | os.PathLike[str],
    *,
    normalizer: Callable[[str], str],
    text_corrector: Callable[[Any], Any],
) -> pd.DataFrame:
    suggestions = {
        "MIN": "MINISTRO",
        "SR": "SENHOR",
        "MAL": "MARECHAL",
        "COM": "COMENDADOR",
        "CONS": "CONSELHEIRO",
        "VER": "VEREADOR",
        "PE": "PADRE",
        "BRIG": "BRIGADEIRO",
    }
    candidate_tokens = set(suggestions)
    frequencies: Counter[str] = Counter()
    examples: dict[str, list[str]] = {}
    columns = [column for column in ("via", "logradouro_geosampa", "de", "ate") if column in df_recape.columns]
    for _, row in df_recape.iterrows():
        for column in columns:
            raw = _text(text_corrector(row.get(column)))
            if not raw:
                continue
            raw_upper = unicodedata.normalize("NFKC", raw).upper()
            for token in re.findall(r"[A-ZÀ-ÖØ-Ý]+", raw_upper):
                if token not in candidate_tokens:
                    continue
                # O token deve ter sido observado na entrada; nenhum alias ou
                # abreviação é aplicado nesta etapa.
                frequencies[token] += 1
                examples.setdefault(token, [])
                if raw not in examples[token] and len(examples[token]) < 5:
                    examples[token].append(raw)
    rows = []
    for token, frequency in sorted(frequencies.items(), key=lambda item: (-item[1], item[0])):
        rows.append({
            "token_original": token,
            "frequencia": int(frequency),
            "possivel_expansao": suggestions.get(token, ""),
            "exemplos": json.dumps(examples[token], ensure_ascii=False),
            "status": "CANDIDATA_ABREVIACAO",
        })
    result = pd.DataFrame(rows, columns=["token_original", "frequencia", "possivel_expansao", "exemplos", "status"])
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_dataframe(result, Path(output_path))
    return result


def _audit_dataframe_legacy(
    df_recape: pd.DataFrame,
    graph: RoadGraph,
    *,
    resolver: StreetResolver | None = None,
    aliases_path: str | os.PathLike[str] = DEFAULT_ALIAS_PATH,
    cache_path: str | os.PathLike[str] = DEFAULT_CACHE_PATH,
    source_path: str | os.PathLike[str] | None = DEFAULT_GEOSAMPA_PATH,
    progress: bool = True,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """Executa a comparação atual × recomendada para cada recape."""
    started = time.perf_counter()
    resolver = resolver or StreetResolver(
        graph,
        aliases_path=aliases_path,
        cache_path=cache_path,
        source_path=source_path,
    )
    rows = []
    method_counts: Counter[str] = Counter()
    failure_reasons: Counter[str] = Counter()
    street_failure_reasons: Counter[str] = Counter()
    route_failure_reasons: Counter[str] = Counter()
    current_exact = current_fuzzy = divergences = ambiguous = with_geo = with_both = 0
    total = len(df_recape)
    for position, (index, row) in enumerate(df_recape.iterrows(), 1):
        context = _context_from_row(row)
        current = _current_resolution(graph, context)
        resolution = resolver.resolve_context(context)
        audit_row = _audit_row(row, index, current, resolution)
        rows.append(audit_row)
        method_counts[resolution.method] += 1
        for reason in resolution.street_review_reasons:
            failure_reasons[reason] += 1
            street_failure_reasons[reason] += 1
        for reason in resolution.route_review_reasons:
            route_failure_reasons[reason] += 1
        current_exact += current[2] == "EXATO"
        current_fuzzy += current[2] == "FUZZY"
        divergences += bool(audit_row["diverge_resolucao_atual"])
        ambiguous += resolution.margin_top2 is not None and resolution.margin_top2 < resolver.config.ambiguous_margin
        with_geo += bool(resolution.candidates and resolution.candidates[0].distance_m is not None)
        with_both += bool(
            resolution.candidates
            and resolution.candidates[0].intersects_de is True
            and resolution.candidates[0].intersects_ate is True
        )
        if progress and (position == total or position % 250 == 0):
            elapsed = time.perf_counter() - started
            rate = position / elapsed if elapsed else 0.0
            eta = (total - position) / rate if rate else 0.0
            print(
                f"   Auditoria: {position:,}/{total:,} | candidatos={sum(len(item.candidates) for item in resolver._resolution_cache.values()):,} "
                f"| evidências_geo={with_geo:,} | interseções_ambas={with_both:,} | "
                f"tempo={elapsed:.1f}s | estimativa_restante={eta:.1f}s",
                end="\r",
            )
    audit = pd.DataFrame(rows)
    if progress:
        print()
    review = audit[audit["requer_revisao"]].copy() if not audit.empty else audit.copy()
    if not review.empty:
        review["decision"] = ""
        review["manual_resolved_street"] = ""
        review["manual_codlog"] = ""
        review["review_notes"] = ""
        review["approved_for_alias"] = ""
    report = {
        "total": int(total),
        "current_exact": int(current_exact),
        "current_fuzzy": int(current_fuzzy),
        "recommended_high": int((audit["confianca"] == "HIGH").sum()) if not audit.empty else 0,
        "recommended_medium": int((audit["confianca"] == "MEDIUM").sum()) if not audit.empty else 0,
        "recommended_low": int((audit["confianca"] == "LOW").sum()) if not audit.empty else 0,
        "unresolved": int((audit["confianca"] == "UNRESOLVED").sum()) if not audit.empty else 0,
        "divergences": int(divergences),
        "ambiguous_top2": int(ambiguous),
        "with_geographic_evidence": int(with_geo),
        "with_both_intersections": int(with_both),
        "street_reviews": int(audit["street_requires_review"].sum()) if not audit.empty else 0,
        "route_reviews": int(audit["route_requires_review"].sum()) if not audit.empty else 0,
        "high_street_with_route_warning": int(
            ((audit["street_confidence"] == "HIGH") & audit["route_requires_review"]).sum()
        ) if not audit.empty else 0,
        "unresolved_transversals_de": int(
            (audit["de_resolution_status"] == "NAO_RESOLVIDA").sum()
        ) if not audit.empty else 0,
        "unresolved_transversals_ate": int(
            (audit["ate_resolution_status"] == "NAO_RESOLVIDA").sum()
        ) if not audit.empty else 0,
        "confirmed_both_intersections": int(with_both),
        "confirmed_single_intersection": int(
            ((audit["intersects_de"] == True) ^ (audit["intersects_ate"] == True)).sum()
        ) if not audit.empty else 0,
        "contradictory_route_contexts": int(
            (audit["route_context_status"] == "CONTEXT_CONTRADICTORY").sum()
        ) if not audit.empty else 0,
        "methods": {str(key): int(value) for key, value in sorted(method_counts.items())},
        "failure_reasons": {str(key): int(value) for key, value in sorted(failure_reasons.items())},
        "street_failure_reasons": {str(key): int(value) for key, value in sorted(street_failure_reasons.items())},
        "route_failure_reasons": {str(key): int(value) for key, value in sorted(route_failure_reasons.items())},
        "invalid_alias_definitions": int(len(resolver.invalid_aliases)),
        "alias_file_errors": list(resolver.alias_file_errors),
        "timings": {
            "total_seconds": round(time.perf_counter() - started, 3),
            "lexical_seconds": round(resolver.stage_seconds["lexical"], 3),
            "geographic_seconds": round(resolver.stage_seconds["geographic"], 3),
            "intersections_seconds": round(resolver.stage_seconds["intersections"], 3),
            "street_resolution_seconds": round(resolver.stage_seconds["street_resolution"], 3),
            "transversal_resolution_seconds": round(resolver.stage_seconds["transversal_resolution"], 3),
            "route_context_seconds": round(resolver.stage_seconds["route_context"], 3),
            "cache_hits": int(resolver.cache_hits),
            "cache_misses": int(resolver.cache_misses),
        },
    }
    return audit, review, report


def audit_dataframe(
    df_recape: pd.DataFrame,
    graph: RoadGraph,
    *,
    resolver: StreetResolver | None = None,
    aliases_path: str | os.PathLike[str] = DEFAULT_ALIAS_PATH,
    cache_path: str | os.PathLike[str] = DEFAULT_CACHE_PATH,
    source_path: str | os.PathLike[str] | None = DEFAULT_GEOSAMPA_PATH,
    progress: bool = True,
    street_only: bool = False,
    skip_route_context: bool = False,
    checkpoint_path: str | os.PathLike[str] | None = None,
    resume: bool = False,
    reset_checkpoint: bool = False,
    checkpoint_every: int | None = None,
    interrupt_after: int | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """Audita em fases, com deduplicacao, caches separados e retomada segura."""
    started = time.perf_counter()
    resolver = resolver or StreetResolver(
        graph,
        aliases_path=aliases_path,
        cache_path=cache_path,
        source_path=source_path,
    )
    evaluate_route_context = not (street_only or skip_route_context)
    mode = "street-only" if street_only else "skip-route-context" if skip_route_context else "full"
    total = len(df_recape)
    checkpoint = Path(checkpoint_path) if checkpoint_path else None
    if checkpoint is not None and reset_checkpoint and checkpoint.exists():
        checkpoint.unlink()
    identity = {
        "version": StreetResolver.VERSION,
        "input": _dataframe_signature(df_recape),
        "resolver": resolver._cache_identity,
        "mode": mode,
    }
    rows: list[dict[str, Any]] = []
    start_position = 0
    if checkpoint is not None and resume and checkpoint.exists():
        payload = _checkpoint_read(checkpoint)
        if payload and payload.get("identity") == identity:
            saved_rows = payload.get("rows", [])
            next_position = int(payload.get("next_position", 0) or 0)
            if isinstance(saved_rows, list) and 0 <= next_position <= total and len(saved_rows) == next_position:
                rows = list(saved_rows)
                start_position = next_position
                resolver.cache_stats["checkpoint_reused_records"] = start_position

    interval = max(1, int(checkpoint_every or resolver.config.checkpoint_every))
    deduplicated: dict[tuple[Any, ...], StreetResolution] = {}
    current_cache: dict[tuple[str, str], tuple[str | None, float, str]] = {}

    def context_key(context: StreetResolutionContext) -> tuple[Any, ...]:
        lat = _safe_float(context.latitude)
        lon = _safe_float(context.longitude)
        digits = resolver.config.coordinate_round_digits
        return (
            resolver._normalize(context.name_used),
            _text(context.codlog),
            round(lat, digits) if lat is not None else None,
            round(lon, digits) if lon is not None else None,
            resolver._normalize(context.de_original),
            resolver._normalize(context.ate_original),
            mode,
        )

    def save_progress(next_position: int) -> None:
        if checkpoint is None:
            return
        checkpoint_started = time.perf_counter()
        resolver.save_cache()
        _checkpoint_write(checkpoint, {
            "identity": identity,
            "next_position": next_position,
            "rows": rows,
            "metrics": dict(resolver.cache_stats),
            "stage_seconds": dict(resolver.stage_seconds),
            "caches": {
                "path": str(resolver.cache_path),
                "identity": resolver._cache_identity,
                "lexical": len(resolver._lexical_cache),
                "primary": len(resolver._primary_cache),
                "geographic": len(resolver._geographic_cache),
                "transversal": len(resolver._transversal_cache),
                "intersections": len(resolver._intersection_cache),
                "context": len(resolver._context_cache),
            },
        })
        resolver.stage_seconds["checkpoint"] += time.perf_counter() - checkpoint_started

    for position, (index, row) in enumerate(df_recape.iloc[start_position:].iterrows(), start_position + 1):
        context = _context_from_row(row)
        current_key = (resolver._normalize(context.name_used), _text(context.codlog))
        if current_key not in current_cache:
            current_cache[current_key] = _current_resolution(graph, context)
        current = current_cache[current_key]
        dedup_key = context_key(context)
        resolution = deduplicated.get(dedup_key)
        if resolution is None:
            resolution = resolver.resolve_context(context, evaluate_route_context=evaluate_route_context)
            deduplicated[dedup_key] = resolution
        else:
            resolver.cache_stats["contexts_deduplicated"] += 1
        rows.append(_audit_row(row, index, current, resolution))
        if progress and (position == total or position % 250 == 0):
            elapsed = time.perf_counter() - started
            rate = position / elapsed if elapsed else 0.0
            eta = (total - position) / rate if rate else 0.0
            print(
                f"   Auditoria: {position:,}/{total:,} | candidatos={resolver.cache_stats['candidates_scored']:,} "
                f"| intersecoes={resolver.cache_stats['intersection_queries']:,} "
                f"| tempo={elapsed:.1f}s | estimativa_restante={eta:.1f}s",
                end="\r",
            )
        if checkpoint is not None and (position % interval == 0 or position == total):
            save_progress(position)
        if interrupt_after is not None and position >= int(interrupt_after):
            save_progress(position)
            raise AuditInterrupted(f"Auditoria interrompida apos {position} registros")
    if progress:
        print()
    audit = pd.DataFrame(rows)
    review = audit[audit["requer_revisao"]].copy() if not audit.empty else audit.copy()
    if not review.empty:
        for column in ("decision", "manual_resolved_street", "manual_codlog", "review_notes", "approved_for_alias"):
            review[column] = ""
    bool_series = lambda column: audit[column].fillna(False).astype(bool) if column in audit else pd.Series(dtype=bool)
    def reason_counts(column: str) -> dict[str, int]:
        counter: Counter[str] = Counter()
        if column in audit:
            for value in audit[column].fillna(""):
                counter.update(item for item in str(value).split(";") if item)
        return {str(key): int(value) for key, value in sorted(counter.items())}
    street_reasons = reason_counts("street_review_reasons")
    route_reasons = reason_counts("route_review_reasons")
    with_both = int(((audit.get("intersects_de", pd.Series(dtype=object)) == True) & (audit.get("intersects_ate", pd.Series(dtype=object)) == True)).sum()) if not audit.empty else 0
    report = {
        "total": int(total),
        "mode": mode,
        "resolver_version": StreetResolver.VERSION,
        "cache_version": CACHE_VERSION,
        "current_exact": int((audit.get("metodo_atual", pd.Series(dtype=object)) == "EXATO").sum()) if not audit.empty else 0,
        "current_fuzzy": int((audit.get("metodo_atual", pd.Series(dtype=object)) == "FUZZY").sum()) if not audit.empty else 0,
        "recommended_high": int((audit.get("confianca", pd.Series(dtype=object)) == "HIGH").sum()) if not audit.empty else 0,
        "recommended_medium": int((audit.get("confianca", pd.Series(dtype=object)) == "MEDIUM").sum()) if not audit.empty else 0,
        "recommended_low": int((audit.get("confianca", pd.Series(dtype=object)) == "LOW").sum()) if not audit.empty else 0,
        "unresolved": int((audit.get("confianca", pd.Series(dtype=object)) == "UNRESOLVED").sum()) if not audit.empty else 0,
        "divergences": int(bool_series("diverge_resolucao_atual").sum()),
        "ambiguous_top2": int(((audit.get("margem_top2", pd.Series(dtype=float)).notna()) & (audit.get("margem_top2", pd.Series(dtype=float)) < resolver.config.ambiguous_margin)).sum()) if not audit.empty else 0,
        "with_geographic_evidence": int(audit.get("distance_m", pd.Series(dtype=object)).notna().sum()) if not audit.empty else 0,
        "with_both_intersections": with_both,
        "street_reviews": int(bool_series("street_requires_review").sum()),
        "route_reviews": int(bool_series("route_requires_review").sum()),
        "high_street_with_route_warning": int(((audit.get("street_confidence", pd.Series(dtype=object)) == "HIGH") & bool_series("route_requires_review")).sum()) if not audit.empty else 0,
        "unresolved_transversals_de": int((audit.get("de_resolution_status", pd.Series(dtype=object)) == "NAO_RESOLVIDA").sum()) if not audit.empty else 0,
        "unresolved_transversals_ate": int((audit.get("ate_resolution_status", pd.Series(dtype=object)) == "NAO_RESOLVIDA").sum()) if not audit.empty else 0,
        "confirmed_both_intersections": with_both,
        "confirmed_single_intersection": int(((audit.get("intersects_de", pd.Series(dtype=object)) == True) ^ (audit.get("intersects_ate", pd.Series(dtype=object)) == True)).sum()) if not audit.empty else 0,
        "contradictory_route_contexts": int((audit.get("route_context_status", pd.Series(dtype=object)) == "CONTEXT_CONTRADICTORY").sum()) if not audit.empty else 0,
        "methods": {str(key): int(value) for key, value in sorted(Counter(audit.get("metodo_recomendado", pd.Series(dtype=object)).dropna()).items())},
        "failure_reasons": {**street_reasons, **route_reasons},
        "street_failure_reasons": street_reasons,
        "route_failure_reasons": route_reasons,
        "invalid_alias_definitions": int(len(resolver.invalid_aliases)),
        "alias_file_errors": list(resolver.alias_file_errors),
        "cache": dict(resolver.cache_stats),
        "metrics": dict(resolver.cache_stats),
        "timings": {
            "total_seconds": round(time.perf_counter() - started, 3),
            **{f"{key}_seconds": round(value, 3) for key, value in resolver.stage_seconds.items()},
            "cache_hits": int(resolver.cache_hits),
            "cache_misses": int(resolver.cache_misses),
        },
    }
    if checkpoint is not None and checkpoint.exists():
        checkpoint.unlink()
    return audit, review, report


def write_audit_reports(
    audit: pd.DataFrame,
    review: pd.DataFrame,
    report: Mapping[str, Any],
    *,
    output_dir: str | os.PathLike[str] = DEFAULT_PROCESSED_DIR,
    normalization_df: pd.DataFrame | None = None,
) -> dict[str, Path]:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    audit_path = output / "street_resolution_audit.csv"
    review_path = output / "street_resolution_review.csv"
    report_path = output / "street_resolution_report.json"
    normalization_path = output / "street_normalization_candidates.csv"
    _atomic_write_dataframe(audit, audit_path)
    review_columns = [
        "decision",
        "manual_resolved_street",
        "manual_codlog",
        "review_notes",
        "approved_for_alias",
    ]
    for column in review_columns:
        if column not in review.columns:
            review[column] = ""
    _atomic_write_dataframe(review, review_path)
    temporary_report = report_path.with_suffix(report_path.suffix + ".tmp")
    with temporary_report.open("w", encoding="utf-8") as stream:
        json.dump(dict(report), stream, ensure_ascii=False, indent=2)
    _atomic_replace(temporary_report, report_path)
    if normalization_df is not None:
        _atomic_write_dataframe(normalization_df, normalization_path)
    return {
        "audit": audit_path,
        "review": review_path,
        "report": report_path,
        "normalization": normalization_path,
    }


def load_existing_road_graph(
    *,
    graph_cache_path: str | os.PathLike[str] = DEFAULT_GRAPH_CACHE,
    source_path: str | os.PathLike[str] = DEFAULT_GEOSAMPA_PATH,
    normalizer: Callable[[str], str] | None = None,
) -> RoadGraph:
    """Carrega o grafo existente ou o constrói do GeoJSON já presente.

    Esta função nunca baixa dados e não chama ``route()``.
    """
    try:
        import transform as transform_module
    except ImportError:  # execução como pacote
        from . import transform as transform_module
        sys.modules.setdefault("transform", transform_module)
    # O ETL foi executado anteriormente como script e o pickle pode ter
    # serializado ``normalizar_rua`` como ``__main__.normalizar_rua``.
    main_module = sys.modules.get("__main__")
    if main_module is not None and not hasattr(main_module, "normalizar_rua"):
        main_module.normalizar_rua = transform_module.normalizar_rua
    if normalizer is None:
        try:
            from transform import normalizar_rua
        except ImportError:  # pragma: no cover
            from . import transform as transform_module
            sys.modules.setdefault("transform", transform_module)
            normalizer = transform_module.normalizar_rua
        else:
            normalizer = normalizar_rua
    graph = RoadGraph.load_cached(graph_cache_path, source_path, normalizer=normalizer)
    if graph is None:
        # Alguns caches antigos persistiram ``st_mtime`` com precisão de
        # segundos. Reaproveitamos esse cache somente quando o tamanho é igual
        # e a diferença temporal é subsegundo; mudanças reais continuam
        # invalidando o índice.
        try:
            with open(graph_cache_path, "rb") as stream:
                payload = pickle.load(stream)
            cached_source = payload.get("source")
            current_source = _source_signature(source_path)
            close_timestamp = (
                cached_source
                and current_source
                and cached_source[0] == current_source[0]
                and abs(int(cached_source[1]) - int(current_source[1])) <= 1_000_000_000
            )
            if payload.get("version") == RoadGraph.CACHE_VERSION and close_timestamp:
                graph = payload["graph"]
                graph.normalizer = normalizer
                graph._rebuild_spatial_index()
        except (OSError, EOFError, pickle.PickleError, AttributeError, KeyError, ValueError, TypeError, ImportError):
            graph = None
    if graph is not None:
        return graph
    if gpd is None:
        raise RuntimeError("geopandas não está instalado para construir o grafo diagnóstico")
    if not os.path.exists(source_path):
        raise FileNotFoundError(f"GeoJSON GeoSampa não encontrado: {source_path}")
    roads = gpd.read_file(source_path)
    if roads.crs is None:
        roads = roads.set_crs("EPSG:31983")
    roads = roads.to_crs("EPSG:31983")
    roads = roads[roads.geometry.notna() & ~roads.geometry.is_empty].copy()
    return RoadGraph.from_geodataframe(roads, normalizer)


def run_audit(
    df_recape: pd.DataFrame,
    graph: RoadGraph,
    *,
    output_dir: str | os.PathLike[str] = DEFAULT_PROCESSED_DIR,
    aliases_path: str | os.PathLike[str] = DEFAULT_ALIAS_PATH,
    cache_path: str | os.PathLike[str] = DEFAULT_CACHE_PATH,
    source_path: str | os.PathLike[str] | None = DEFAULT_GEOSAMPA_PATH,
    progress: bool = True,
    street_only: bool = False,
    skip_route_context: bool = False,
    checkpoint_path: str | os.PathLike[str] | None = None,
    resume: bool = True,
    reset_checkpoint: bool = False,
    reset_cache: bool = False,
    checkpoint_every: int | None = None,
    load_graph_seconds: float | None = None,
) -> dict[str, Any]:
    """Executa e persiste a auditoria completa, sem alterar ``df_recape``."""
    try:
        from transform import corrigir_texto, normalizar_rua
    except ImportError:  # pragma: no cover
        from .transform import corrigir_texto, normalizar_rua
    cache_file = Path(cache_path)
    checkpoint_file = Path(checkpoint_path) if checkpoint_path else Path(output_dir) / "street_resolution_audit.checkpoint.pkl"
    if reset_cache and cache_file.exists():
        cache_file.unlink()
    if reset_cache and checkpoint_file.exists():
        checkpoint_file.unlink()
    if reset_checkpoint and checkpoint_file.exists():
        checkpoint_file.unlink()
    resolver = StreetResolver(
        graph,
        normalizer=normalizar_rua,
        text_corrector=corrigir_texto,
        aliases_path=aliases_path,
        cache_path=cache_path,
        source_path=source_path,
    )
    audit, review, report = audit_dataframe(
        df_recape,
        graph,
        resolver=resolver,
        progress=progress,
        street_only=street_only,
        skip_route_context=skip_route_context,
        checkpoint_path=checkpoint_file,
        resume=resume,
        reset_checkpoint=reset_checkpoint,
        checkpoint_every=checkpoint_every,
    )
    if load_graph_seconds is not None:
        report.setdefault("timings", {})["load_graph_seconds"] = round(float(load_graph_seconds), 3)
    normalization = _write_normalization_candidates(
        df_recape,
        Path(output_dir) / "street_normalization_candidates.csv",
        normalizer=normalizar_rua,
        text_corrector=corrigir_texto,
    )
    report_started = time.perf_counter()
    paths = write_audit_reports(audit, review, report, output_dir=output_dir, normalization_df=normalization)
    resolver.save_cache()
    report = dict(report)
    report["timings"] = dict(report.get("timings", {}))
    report["timings"]["report_write_seconds"] = round(time.perf_counter() - report_started, 3)
    report["output_files"] = {key: str(value) for key, value in paths.items()}
    report_path = Path(output_dir) / "street_resolution_report.json"
    temporary_report = report_path.with_suffix(report_path.suffix + ".tmp")
    with temporary_report.open("w", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    _atomic_replace(temporary_report, report_path)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Auditoria diagnóstica de logradouros")
    parser.add_argument("--limit", type=int, default=None, help="limita a quantidade de recapes")
    parser.add_argument("--sample", type=int, default=None, help="alias de --limit para amostras")
    parser.add_argument("--resume", action="store_true", help="retoma o checkpoint valido")
    parser.add_argument("--reset-cache", action="store_true", help="limpa apenas os caches diagnosticos")
    parser.add_argument("--audit-streets-reset", action="store_true", help="reinicia checkpoint e cache diagnosticos")
    parser.add_argument("--skip-route-context", action="store_true", help="nao resolve De/AtÃ©")
    parser.add_argument("--street-only", action="store_true", help="audita somente a via principal")
    parser.add_argument("--checkpoint-every", type=int, default=None)
    parser.add_argument("--output-dir", default=str(DEFAULT_PROCESSED_DIR))
    parser.add_argument("--aliases", default=str(DEFAULT_ALIAS_PATH))
    parser.add_argument("--cache", default=str(DEFAULT_CACHE_PATH))
    parser.add_argument("--graph-cache", default=str(DEFAULT_GRAPH_CACHE))
    parser.add_argument("--geojson", default=str(DEFAULT_GEOSAMPA_PATH))
    args = parser.parse_args(argv)
    try:
        try:
            from transform import load_recape, normalizar_rua
        except ImportError:
            from .transform import load_recape, normalizar_rua
        recape = load_recape()
        limit = args.sample if args.sample is not None else args.limit
        if limit is not None:
            recape = recape.head(max(limit, 0)).copy()
        graph = load_existing_road_graph(
            graph_cache_path=args.graph_cache,
            source_path=args.geojson,
            normalizer=normalizar_rua,
        )
        report = run_audit(
            recape,
            graph,
            output_dir=args.output_dir,
            aliases_path=args.aliases,
            cache_path=args.cache,
            source_path=args.geojson,
            street_only=args.street_only,
            skip_route_context=args.skip_route_context,
            resume=True,
            reset_checkpoint=args.audit_streets_reset,
            reset_cache=args.reset_cache or args.audit_streets_reset,
            checkpoint_every=args.checkpoint_every,
        )
        print(json.dumps({key: report[key] for key in ("total", "recommended_high", "recommended_medium", "recommended_low", "unresolved", "divergences")}, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(f"Auditoria diagnóstica não executada: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
