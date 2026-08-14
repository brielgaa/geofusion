"""Consensus Evidence Engine for GeoFusion.

This module is deliberately shadow-only.  It reads persisted diagnostics and
combines them without invoking the ETL, StreetResolver, RoadGraph routing, or
any geometry generator.  The engine is intentionally conservative: evidence
is counted by dependency-aware groups and only when it refers to the same
candidate geometry.

Usage::

    python src/consensus_evidence.py --shadow
    python src/consensus_evidence.py --shadow --sample 30

The two public artifacts are ``data/processed/consensus_evidence_shadow.csv``
and ``data/processed/consensus_evidence_report.json``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
import tracemalloc
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import pandas as pd
from pyproj import Transformer
from shapely.geometry import LineString
from shapely.ops import transform
from shapely.wkt import loads as load_wkt


ROOT = Path(__file__).resolve().parents[1]
PROCESSED = ROOT / "data" / "processed"
OUTPUT_CSV = PROCESSED / "consensus_evidence_shadow.csv"
OUTPUT_REPORT = PROCESSED / "consensus_evidence_report.json"

VERSION = "consensus-evidence-shadow-v1"
OUTPUT_COLUMNS = [
    "id", "candidate_wkt", "candidate_hash", "snapshot_status",
    "geometry_validator_class", "boundary_class", "name_recovery_class",
    "route_quality_class", "human_review_class", "independent_evidence_count",
    "supporting_evidence_count", "conflicting_evidence_count",
    "independent_families_json", "supporting_sources_json",
    "conflicting_sources_json", "topology_ok", "boundary_ok", "name_ok",
    "gps_ok", "extension_ok", "component_ok", "codlog_ok",
    "candidate_competition", "candidate_count", "candidate_margin",
    "hard_failure_count", "hard_failures", "consensus_score",
    "consensus_class", "reason", "warnings", "official_geometry_present",
]

INDEPENDENT_SUPPORT_GROUPS = {"GEOMETRY_VALIDATION", "BOUNDARY_CHAIN", "HUMAN_REVIEW"}

WGS84_TO_METRIC = Transformer.from_crs("EPSG:4326", "EPSG:31983", always_xy=True)


def _text(value: Any) -> str:
    if value is None:
        return ""
    try:
        if bool(pd.isna(value)):
            return ""
    except (TypeError, ValueError):
        pass
    rendered = str(value).strip()
    return "" if rendered.casefold() in {"", "nan", "none", "null", "<na>", "[]"} else rendered


def _number(value: Any) -> float | None:
    try:
        converted = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None
    return converted if math.isfinite(converted) else None


def _integer(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def _boolean(value: Any) -> bool | None:
    text = _text(value).casefold()
    if not text:
        return None
    if text in {"true", "1", "yes", "sim", "y", "t"}:
        return True
    if text in {"false", "0", "no", "nao", "não", "n", "f"}:
        return False
    return None


def _tokens(value: Any) -> tuple[str, ...]:
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(sorted({str(item).strip() for item in value if _text(item)}))
    text = _text(value)
    if not text:
        return ()
    payload: Any = None
    if text.startswith("[") or text.startswith("{"):
        try:
            payload = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = None
    if isinstance(payload, list):
        values = payload
    elif isinstance(payload, dict):
        values = list(payload.values())
    else:
        values = re.split(r"[|;,]", text)
    return tuple(sorted({str(item).strip() for item in values if _text(item)}))


def _json(value: Any, default: Any = None) -> Any:
    if isinstance(value, (dict, list, tuple)):
        return value
    text = _text(value)
    if not text:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def parse_wkt(value: Any):
    text = _text(value)
    if not text:
        return None
    try:
        geometry = load_wkt(text)
    except Exception:
        return None
    if geometry is None or geometry.is_empty:
        return None
    return geometry


def geometry_hash(value: Any) -> str:
    """Return the hash convention used by ``geometry_validator.py``."""
    if hasattr(value, "candidate_wkt"):
        value = getattr(value, "candidate_wkt")
    if isinstance(value, Mapping):
        value = value.get("candidate_wkt") or value.get("geometry_wkt") or value.get("wkt")
    text = _text(value)
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text else ""


@dataclass(frozen=True)
class GeometryEquivalenceConfig:
    """Tolerances calibrated to the project's persisted metric geometries.

    Exact equality is preferred.  The near envelope uses metric coordinates,
    the same EPSG:31983 convention used by the validator and boundary audit.
    The values are intentionally exposed and reported so a future calibration
    can change them without hiding a threshold decision in the classifier.
    """

    near_hausdorff_m: float = 2.0
    near_endpoint_m: float = 3.0
    near_length_difference_pct: float = 5.0
    partial_overlap_ratio: float = 0.25


def _candidate_value(value: Any) -> tuple[Any, str]:
    if hasattr(value, "candidate_wkt"):
        wkt = _text(getattr(value, "candidate_wkt"))
        supplied_hash = _text(getattr(value, "geometry_hash", ""))
    elif isinstance(value, Mapping):
        wkt = _text(value.get("candidate_wkt") or value.get("geometry_wkt") or value.get("wkt"))
        supplied_hash = _text(value.get("geometry_hash") or value.get("geometry_hash_sha256"))
    else:
        wkt = _text(value)
        supplied_hash = ""
    geometry = parse_wkt(wkt)
    if geometry is not None and geometry.geom_type not in {"LineString", "MultiLineString"}:
        geometry = None
    return geometry, supplied_hash or geometry_hash(wkt)


def _endpoint_distance(left: Any, right: Any) -> float:
    try:
        left_start, left_end = left.boundary.geoms
        right_start, right_end = right.boundary.geoms
        return min(
            max(left_start.distance(right_start), left_end.distance(right_end)),
            max(left_start.distance(right_end), left_end.distance(right_start)),
        )
    except (AttributeError, ValueError, TypeError):
        return math.inf


def compare_geometry_candidates(
    left: Any,
    right: Any,
    config: GeometryEquivalenceConfig | None = None,
) -> str:
    """Compare two candidate geometries.

    Returns one of ``EXACT``, ``NEAR_EQUIVALENT``, ``PARTIAL_OVERLAP``,
    ``DIFFERENT`` or ``UNKNOWN``.  No geometry is generated or repaired.
    """
    config = config or GeometryEquivalenceConfig()
    left_geometry, left_hash = _candidate_value(left)
    right_geometry, right_hash = _candidate_value(right)
    if left_geometry is None or right_geometry is None:
        return "UNKNOWN"
    if left_hash and right_hash and left_hash == right_hash:
        return "EXACT"
    try:
        if left_geometry.equals(right_geometry):
            return "EXACT"
        left_length = float(left_geometry.length)
        right_length = float(right_geometry.length)
        if max(left_length, right_length) <= 0:
            return "UNKNOWN"
        length_difference = abs(left_length - right_length) / max(left_length, right_length) * 100.0
        hausdorff = max(left_geometry.hausdorff_distance(right_geometry), right_geometry.hausdorff_distance(left_geometry))
        endpoints = _endpoint_distance(left_geometry, right_geometry)
        if (
            hausdorff <= config.near_hausdorff_m
            and endpoints <= config.near_endpoint_m
            and length_difference <= config.near_length_difference_pct
        ):
            return "NEAR_EQUIVALENT"
        overlap = left_geometry.intersection(right_geometry).length / min(left_length, right_length)
        if overlap >= config.partial_overlap_ratio:
            return "PARTIAL_OVERLAP"
    except Exception:
        return "UNKNOWN"
    return "DIFFERENT"


@dataclass(frozen=True)
class EvidenceRecord:
    """Normalized evidence row independent of any one persisted CSV schema."""

    record_id: str
    source: str
    family: str
    classification: str = ""
    score: float | None = None
    candidate_wkt: str = ""
    geometry_hash: str = ""
    codlog: str = ""
    component: str = ""
    confidence: str = ""
    hard_failures: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    provenance: Mapping[str, Any] = field(default_factory=dict)
    depends_on: tuple[str, ...] = ()
    independent_group: str = ""
    candidate_count: int | None = None
    candidate_margin: float | None = None
    topology_ok: bool | None = None
    boundary_ok: bool | None = None
    name_ok: bool | None = None
    gps_ok: bool | None = None
    extension_ok: bool | None = None
    component_ok: bool | None = None
    codlog_ok: bool | None = None
    control_label: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "record_id", _text(self.record_id))
        object.__setattr__(self, "source", _text(self.source))
        object.__setattr__(self, "family", _text(self.family))
        object.__setattr__(self, "classification", _text(self.classification))
        object.__setattr__(self, "candidate_wkt", _text(self.candidate_wkt))
        object.__setattr__(self, "geometry_hash", _text(self.geometry_hash) or geometry_hash(self.candidate_wkt))
        object.__setattr__(self, "hard_failures", _tokens(self.hard_failures))
        object.__setattr__(self, "warnings", _tokens(self.warnings))
        object.__setattr__(self, "depends_on", _tokens(self.depends_on))
        object.__setattr__(self, "provenance", dict(self.provenance or {}))
        object.__setattr__(self, "independent_group", _text(self.independent_group) or self.family)


@dataclass(frozen=True)
class ConsensusEvidenceResult:
    id: str
    candidate_wkt: str = ""
    candidate_hash: str = ""
    snapshot_status: str = "SNAPSHOT_UNKNOWN"
    geometry_validator_class: str = ""
    boundary_class: str = ""
    name_recovery_class: str = ""
    route_quality_class: str = ""
    human_review_class: str = "UNREVIEWED"
    independent_evidence_count: int = 0
    supporting_evidence_count: int = 0
    conflicting_evidence_count: int = 0
    independent_families_json: str = "[]"
    supporting_sources_json: str = "[]"
    conflicting_sources_json: str = "[]"
    topology_ok: bool | None = None
    boundary_ok: bool | None = None
    name_ok: bool | None = None
    gps_ok: bool | None = None
    extension_ok: bool | None = None
    component_ok: bool | None = None
    codlog_ok: bool | None = None
    candidate_competition: bool = False
    candidate_count: int | None = None
    candidate_margin: float | None = None
    hard_failure_count: int = 0
    hard_failures: tuple[str, ...] = ()
    consensus_score: float = 0.0
    consensus_class: str = "INSUFFICIENT_EVIDENCE"
    reason: str = "INSUFFICIENT_INDEPENDENT_EVIDENCE"
    warnings: tuple[str, ...] = ()
    official_geometry_present: bool = False

    def to_row(self) -> dict[str, Any]:
        row = asdict(self)
        row["hard_failures"] = "|".join(self.hard_failures)
        row["warnings"] = "|".join(self.warnings)
        for field_name in ("topology_ok", "boundary_ok", "name_ok", "gps_ok", "extension_ok", "component_ok", "codlog_ok"):
            value = row[field_name]
            row[field_name] = "" if value is None else str(bool(value))
        row["candidate_competition"] = str(bool(self.candidate_competition))
        return row


@dataclass
class SourceArtifact:
    name: str
    path: Path
    kind: str
    id_column: str | None = None
    row_count: int = 0
    unique_id_count: int = 0
    duplicate_id_count: int = 0
    schema: list[str] = field(default_factory=list)
    geometry_columns: list[str] = field(default_factory=list)
    classification_columns: list[str] = field(default_factory=list)
    version_values: list[str] = field(default_factory=list)
    sha256: str | None = None
    modified_at: str | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["path"] = str(self.path)
        return payload


SOURCE_SPECS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("route_quality", "route_geometry_quality_shadow.csv", "shadow", ("id",)),
    ("geometry_validator", "geometry_validation_shadow.csv", "shadow", ("id",)),
    ("boundary_audit", "boundary_contradiction_audit.csv", "shadow", ("id", "record_id")),
    ("name_recovery", "boundary_name_recovery.csv", "shadow", ("id",)),
    ("route_audit", "route_geometry_audit.csv", "audit", ("id",)),
    ("route_review", "route_geometry_review.csv", "review", ("id",)),
    ("human_review", "route_geometry_human_review.csv", "human_review", ("id",)),
    ("street_resolution", "street_resolution_audit.csv", "diagnostic", ("id",)),
    ("street_human_review", "street_resolution_human_review.csv", "human_review", ("id",)),
    ("official_geometry", "recape_clean.csv", "official", ("id",)),
)


DEPENDENCY_GRAPH: tuple[dict[str, Any], ...] = (
    {"source": "route_quality", "evidence_family": "TOPOLOGY", "source_module": "route_geometry_quality_shadow", "depends_on": ["route_audit"], "independent_group": "ROUTE_QUALITY_CHAIN"},
    {"source": "route_audit", "evidence_family": "TOPOLOGY", "source_module": "route_geometry_audit", "depends_on": [], "independent_group": "ROUTE_QUALITY_CHAIN"},
    {"source": "geometry_validator", "evidence_family": "GEOMETRY_VALIDATION", "source_module": "geometry_validator", "depends_on": ["route_quality"], "independent_group": "GEOMETRY_VALIDATION"},
    {"source": "boundary_audit", "evidence_family": "BOUNDARY_GEOMETRY", "source_module": "boundary_contradiction_audit", "depends_on": ["geometry_validator", "route_quality"], "independent_group": "BOUNDARY_CHAIN"},
    {"source": "name_recovery", "evidence_family": "BOUNDARY_LEXICAL", "source_module": "boundary_name_recovery", "depends_on": ["boundary_audit"], "independent_group": "BOUNDARY_CHAIN"},
    {"source": "street_resolution", "evidence_family": "STREET_RESOLUTION", "source_module": "street_resolution_audit", "depends_on": [], "independent_group": "STREET_RESOLUTION"},
    {"source": "human_review", "evidence_family": "HUMAN_REVIEW", "source_module": "route_geometry_human_review", "depends_on": [], "independent_group": "HUMAN_REVIEW"},
)


def dependency_graph() -> list[dict[str, Any]]:
    """Return a JSON-safe copy of the explicit dependency map."""
    return json.loads(json.dumps(DEPENDENCY_GRAPH))


def _definition(source: str) -> dict[str, Any]:
    for definition in DEPENDENCY_GRAPH:
        if definition["source"] == source:
            return definition
    return {"evidence_family": "UNKNOWN", "depends_on": [], "independent_group": source}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def discover_artifacts(root: Path | str = ROOT) -> list[SourceArtifact]:
    """Inventory persisted artifacts without assuming a common population."""
    root = Path(root)
    processed = root / "data" / "processed"
    artifacts: list[SourceArtifact] = []
    known_paths: set[Path] = set()
    for name, filename, kind, id_candidates in SOURCE_SPECS:
        path = processed / filename
        if not path.exists():
            continue
        known_paths.add(path.resolve())
        frame = pd.read_csv(path, dtype=str, encoding="utf-8-sig", keep_default_na=False, nrows=0)
        schema = [str(column) for column in frame.columns]
        id_column = next((column for column in id_candidates if column in schema), None)
        rows = pd.read_csv(path, dtype=str, encoding="utf-8-sig", keep_default_na=False)
        identifiers = rows[id_column].astype(str) if id_column and id_column in rows else pd.Series(dtype=str)
        clean_ids = identifiers[identifiers.str.strip() != ""]
        version_columns = [column for column in schema if "version" in column.casefold() or "snapshot" in column.casefold()]
        versions = sorted({
            _text(value) for column in version_columns for value in rows[column].tolist() if _text(value)
        })
        geometry_columns = [column for column in schema if any(token in column.casefold() for token in ("wkt", "geometry", "path"))]
        classification_columns = [column for column in schema if any(token in column.casefold() for token in ("class", "recommend", "status", "decision", "confidence"))]
        notes: list[str] = []
        if len(versions) > 1:
            notes.append("MIXED_EXPLICIT_VERSIONS")
        artifacts.append(SourceArtifact(
            name=name, path=path, kind=kind, id_column=id_column,
            row_count=int(len(rows)), unique_id_count=int(clean_ids.nunique()),
            duplicate_id_count=int(max(0, len(clean_ids) - clean_ids.nunique())),
            schema=schema, geometry_columns=geometry_columns,
            classification_columns=classification_columns, version_values=versions,
            sha256=_sha256(path), modified_at=datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat(), notes=notes,
        ))
    # Detect additional shadow CSVs so an operator can see them in the report,
    # while leaving unknown schemas out of the scoring population.
    for path in sorted(processed.glob("*_shadow.csv")):
        if path.resolve() in known_paths:
            continue
        artifacts.append(SourceArtifact(
            name=f"unmapped:{path.stem}", path=path, kind="unmapped_shadow",
            sha256=_sha256(path), modified_at=datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat(),
            notes=["SCHEMA_NOT_MAPPED_TO_CONSENSUS_EVIDENCE"],
        ))
    return artifacts


def _row_provenance(source: str, artifact: SourceArtifact | None, row: Mapping[str, Any]) -> dict[str, Any]:
    definition = _definition(source)
    version = _text(row.get("shadow_version") or row.get("validator_version") or row.get("source_audit_version"))
    return {
        "source": source,
        "source_module": definition.get("source_module", source),
        "source_version": version,
        "artifact": str(artifact.path) if artifact else "",
        "artifact_sha256": artifact.sha256 if artifact else "",
        "snapshot_id": _text(row.get("snapshot_id") or row.get("generation_id") or row.get("run_id") or row.get("input_hash")),
    }


def _record(source: str, row: Mapping[str, Any], artifact: SourceArtifact | None, **values: Any) -> EvidenceRecord:
    definition = _definition(source)
    values.setdefault("record_id", _text(row.get("id") or row.get("record_id")))
    values.setdefault("source", source)
    values.setdefault("family", definition.get("evidence_family", "UNKNOWN"))
    values.setdefault("provenance", _row_provenance(source, artifact, row))
    values.setdefault("depends_on", tuple(definition.get("depends_on", ())))
    values.setdefault("independent_group", definition.get("independent_group", source))
    return EvidenceRecord(**values)


def _quality_records(frame: pd.DataFrame, artifact: SourceArtifact | None) -> list[EvidenceRecord]:
    records = []
    for _, row in frame.iterrows():
        values = row.to_dict()
        warnings = _tokens(row.get("warnings"))
        failures = set(_tokens(row.get("hard_failures")))
        warning_text = "|".join(warnings).upper()
        if "LOOP" in warning_text or "SELF_INTERSECTION" in warning_text:
            failures.add("SELF_INTERSECTION_OR_LOOP")
        if "DESVIO_EXTENSAO_ACIMA_50" in warning_text:
            failures.add("IMPOSSIBLE_LENGTH_DEVIATION")
        topology = _text(row.get("topology_status")).upper()
        component = _text(row.get("component_status")).upper()
        if "MULTIPLE" in topology or "DISCONNECTED" in topology:
            failures.add("DISCONNECTED_ROUTE")
        records.append(_record(
            "route_quality", values, artifact, classification=_text(row.get("geometry_confidence")),
            score=_number(row.get("geometry_score")), candidate_wkt=_text(row.get("geometry_wkt")),
            codlog=_text(row.get("codlog")), component=component, confidence=_text(row.get("geometry_confidence")),
            hard_failures=tuple(sorted(failures)), warnings=warnings,
            candidate_count=_integer(row.get("candidate_count")), candidate_margin=_number(row.get("top2_margin")),
            topology_ok=(topology not in {"UNRESOLVED", "MULTIPLE_COMPONENTS"}) if topology else None,
            component_ok=(component not in {"UNRESOLVED", "WRONG_COMPONENT"}) if component else None,
            extension_ok=(_number(row.get("extension_deviation_pct")) <= 50.0) if _number(row.get("extension_deviation_pct")) is not None else None,
        ))
    return records


def _validator_records(frame: pd.DataFrame, artifact: SourceArtifact | None) -> list[EvidenceRecord]:
    records = []
    for _, row in frame.iterrows():
        values = row.to_dict()
        failures = set(_tokens(row.get("hard_failures")))
        if _boolean(row.get("geometry_valid")) is False:
            failures.add("INVALID_WKT")
        topology = _text(row.get("topology_status") or row.get("topology_status_official")).upper()
        component = _text(row.get("component_status")).upper()
        if topology in {"MULTIPLE_COMPONENTS", "DISCONNECTED", "UNAVAILABLE"}:
            failures.add("DISCONNECTED_ROUTE" if topology == "MULTIPLE_COMPONENTS" else "TOPOLOGY_CONFLICT")
        if component in {"UNRESOLVED", "WRONG_COMPONENT"}:
            failures.add("WRONG_COMPONENT")
        if _text(row.get("de_validation")).upper() in {"CONTRADICTED", "CONTRADICTORY"} or _text(row.get("ate_validation")).upper() in {"CONTRADICTED", "CONTRADICTORY"}:
            failures.add("BOUNDARY_CONTRADICTION_CRITICAL")
        records.append(_record(
            "geometry_validator", values, artifact, classification=_text(row.get("validation_class")),
            score=_number(row.get("validation_score_independent")), candidate_wkt=_text(row.get("geometry_wkt")),
            geometry_hash=_text(row.get("geometry_hash_sha256")), codlog=_text(row.get("codlog")),
            component=component, confidence=_text(row.get("geometry_confidence")), hard_failures=tuple(sorted(failures)),
            warnings=_tokens(row.get("warnings")), candidate_count=_integer(row.get("candidate_count")),
            candidate_margin=_number(row.get("top2_margin") or row.get("validation_margin_top2")),
            topology_ok=_boolean(row.get("valid_geometry")) if _boolean(row.get("valid_geometry")) is not None else ((topology not in {"UNAVAILABLE", "MULTIPLE_COMPONENTS", "DISCONNECTED"}) if topology else None),
            component_ok=(component not in {"UNRESOLVED", "WRONG_COMPONENT", "MULTIPLE_COMPONENTS"}) if component else None,
            boundary_ok=(_text(row.get("de_validation")).upper() not in {"CONTRADICTED", "CONTRADICTORY"} and _text(row.get("ate_validation")).upper() not in {"CONTRADICTED", "CONTRADICTORY"}) if (_text(row.get("de_validation")) or _text(row.get("ate_validation"))) else None,
            gps_ok=_text(row.get("gps_status")).upper() in {"ON_PATH", "NEAR_PATH"} if _text(row.get("gps_status")) else None,
            extension_ok=(_number(row.get("extension_deviation_pct")) <= 50.0) if _number(row.get("extension_deviation_pct")) is not None else None,
        ))
    return records


def _boundary_records(frame: pd.DataFrame, artifact: SourceArtifact | None) -> list[EvidenceRecord]:
    records = []
    for _, row in frame.iterrows():
        values = row.to_dict()
        classification = _text(row.get("recommendation"))
        failures = set(_tokens(row.get("warnings")))
        if classification in {"KEEP_CONTRADICTION", "BOUNDARIES_REVERSED"}:
            failures.add("BOUNDARY_CONTRADICTION_CRITICAL")
        if classification == "DATA_INSUFFICIENT":
            # Absence of boundary evidence is not a rejection of the route;
            # it is a coverage limitation and must remain insufficient.
            warnings = tuple(sorted(set(_tokens(row.get("warnings"))) | {"BOUNDARY_EVIDENCE_INSUFFICIENT"}))
        else:
            warnings = _tokens(row.get("warnings"))
        codlogs = [value for value in (_text(row.get("de_codlog")), _text(row.get("ate_codlog"))) if value]
        records.append(_record(
            "boundary_audit", values, artifact, classification=classification,
            score=_number(row.get("boundary_validation_score")), candidate_wkt=_text(row.get("candidate_geometry_wkt")),
            codlog="|".join(sorted(set(codlogs))), component="", confidence=classification,
            hard_failures=tuple(sorted(failures)), warnings=warnings,
            boundary_ok=classification in {"BOUNDARIES_VALIDATED_HIGH", "BOUNDARIES_VALIDATED_MEDIUM", "ONE_BOUNDARY_VALIDATED"},
            candidate_count=None, candidate_margin=None,
        ))
    return records


def _name_records(frame: pd.DataFrame, artifact: SourceArtifact | None) -> list[EvidenceRecord]:
    records = []
    for _, row in frame.iterrows():
        values = row.to_dict()
        classification = _text(row.get("classification"))
        failures = {"LEXICAL_CONTRADICTION"} if classification == "NAME_DATA_CONTRADICTION" else set()
        records.append(_record(
            "name_recovery", values, artifact, classification=classification, score=_number(row.get("name_score")),
            codlog=_text(row.get("recovered_codlog")), confidence=classification,
            hard_failures=tuple(sorted(failures)), warnings=_tokens(row.get("warnings")),
            name_ok=classification in {"NAME_RECOVERED_HIGH", "NAME_RECOVERED_MEDIUM"},
        ))
    return records


def _route_audit_records(frame: pd.DataFrame, artifact: SourceArtifact | None) -> list[EvidenceRecord]:
    records = []
    for _, row in frame.iterrows():
        topology = _text(row.get("topology_status")).upper()
        component = _text(row.get("component_status")).upper()
        failures: set[str] = set()
        if "LOOP" in "|".join(_tokens(row.get("warnings"))).upper():
            failures.add("SELF_INTERSECTION_OR_LOOP")
        records.append(_record(
            "route_audit", row.to_dict(), artifact, classification=_text(row.get("geometry_confidence")),
            score=_number(row.get("geometry_score")), candidate_wkt=_text(row.get("geometry_wkt")),
            codlog=_text(row.get("codlog")), component=component, confidence=_text(row.get("geometry_confidence")),
            hard_failures=tuple(sorted(failures)), warnings=_tokens(row.get("warnings")),
            candidate_count=_integer(row.get("candidate_count")),
            topology_ok=topology not in {"UNRESOLVED", "MULTIPLE_COMPONENTS"} if topology else None,
            component_ok=component not in {"UNRESOLVED", "WRONG_COMPONENT"} if component else None,
        ))
    return records


def _street_records(frame: pd.DataFrame, artifact: SourceArtifact | None) -> list[EvidenceRecord]:
    records = []
    for _, row in frame.iterrows():
        classification = _text(row.get("confianca") or row.get("street_confidence") or row.get("metodo_recomendado"))
        warnings = _tokens(row.get("motivos_revisao"))
        records.append(_record(
            "street_resolution", row.to_dict(), artifact, classification=classification,
            score=_number(row.get("score_final") or row.get("score_atual")), codlog=_text(row.get("codlog_recomendado") or row.get("codlog_informado")),
            confidence=classification,
            warnings=tuple(sorted(set(warnings) | ({"STREET_RESOLUTION_REQUIRES_REVIEW"} if _boolean(row.get("requer_revisao")) else set()))),
            hard_failures=(),
            codlog_ok=not bool(_text(row.get("codlog_informado")) and _text(row.get("codlog_recomendado")) and _text(row.get("codlog_informado")) != _text(row.get("codlog_recomendado"))),
        ))
    return records


def _human_records(frame: pd.DataFrame, artifact: SourceArtifact | None) -> list[EvidenceRecord]:
    records = []
    for _, row in frame.iterrows():
        decision = _text(row.get("decision")).upper()
        if decision.startswith("APROVAR") or decision == "APPROVED":
            classification = "APPROVED"
            failures: tuple[str, ...] = ()
        elif decision.startswith("REJEITAR") or decision in {"REJECTED", "MANTER_SEM_GEOMETRIA"}:
            classification = "REJECTED"
            failures = ("HUMAN_REJECTION",)
        else:
            classification = "UNREVIEWED"
            failures = ()
        records.append(_record(
            "human_review", row.to_dict(), artifact, classification=classification,
            score=_number(row.get("geometry_score")), candidate_wkt=_text(row.get("manual_geometry_wkt")),
            confidence=_text(row.get("confidence_class") or row.get("geometry_confidence")),
            hard_failures=failures, warnings=_tokens(row.get("review_notes")),
            component=_text(row.get("selected_component_count")),
        ))
    return records


def load_evidence_records(root: Path | str = ROOT) -> tuple[list[EvidenceRecord], dict[str, SourceArtifact]]:
    """Load all mapped diagnostic artifacts into normalized records."""
    root = Path(root)
    artifacts = {item.name: item for item in discover_artifacts(root)}
    processed = root / "data" / "processed"
    loaders = {
        "route_quality": _quality_records, "geometry_validator": _validator_records,
        "boundary_audit": _boundary_records, "name_recovery": _name_records,
        "route_audit": _route_audit_records, "street_resolution": _street_records,
        "human_review": _human_records,
    }
    records: list[EvidenceRecord] = []
    for source, filename, _kind, _ids in SOURCE_SPECS:
        if source not in loaders:
            continue
        path = processed / filename
        if not path.exists():
            continue
        frame = pd.read_csv(path, dtype=str, encoding="utf-8-sig", keep_default_na=False)
        records.extend(loaders[source](frame, artifacts.get(source)))
    return records, artifacts


def _strength(record: EvidenceRecord) -> str:
    classification = record.classification.upper()
    if classification in {"VALIDATED_HIGH", "BOUNDARIES_VALIDATED_HIGH", "NAME_RECOVERED_HIGH", "RECONSTRUCTED_HIGH", "HIGH", "APPROVED"}:
        return "HIGH"
    if classification in {"VALIDATED_MEDIUM", "BOUNDARIES_VALIDATED_MEDIUM", "ONE_BOUNDARY_VALIDATED", "NAME_RECOVERED_MEDIUM", "RECONSTRUCTED_MEDIUM", "MEDIUM"}:
        return "MEDIUM"
    return ""


def _direction(record: EvidenceRecord) -> str:
    if record.classification.upper() in {"REJECTED", "KEEP_CONTRADICTION", "BOUNDARIES_REVERSED", "NAME_DATA_CONTRADICTION", "REJECTED_BY_CONSENSUS", "REJECT", "REJECTED_GEOMETRY"} or record.hard_failures:
        return "REJECT"
    if _strength(record):
        return "SUPPORT"
    return "NEUTRAL"


def _snapshot_status(records: Sequence[EvidenceRecord]) -> str:
    tokens = {
        _text(record.provenance.get(key))
        for record in records
        for key in ("snapshot_id", "generation_id", "run_id", "input_hash", "snapshot_hash")
        if _text(record.provenance.get(key))
    }
    if len(tokens) > 1 or any(_boolean(record.provenance.get("snapshot_conflict")) for record in records):
        return "SNAPSHOT_CONFLICT"
    if not records:
        return "SNAPSHOT_UNKNOWN"
    versions = [_text(record.provenance.get("source_version")) for record in records]
    if all(versions):
        return "SNAPSHOT_ALIGNED"
    if any(versions):
        return "SNAPSHOT_PARTIAL"
    return "SNAPSHOT_UNKNOWN"


def _canonical_candidate(records: Sequence[EvidenceRecord]) -> EvidenceRecord | None:
    priority = {"geometry_validator": 0, "route_quality": 1, "boundary_audit": 2, "human_review": 3, "route_audit": 4}
    candidates = [record for record in records if record.candidate_wkt]
    candidates.sort(key=lambda record: (priority.get(record.source, 99), -(_number(record.score) or 0.0), record.source))
    return candidates[0] if candidates else None


def _aggregate_class(records: Sequence[EvidenceRecord], sources: set[str]) -> str:
    values = [record for record in records if record.source in sources and record.classification]
    if not values:
        return ""
    order = {"HIGH": 0, "MEDIUM": 1, "REJECT": 2, "OTHER": 3}
    return min(values, key=lambda record: (order.get(_strength(record), 2 if _direction(record) == "REJECT" else 3), record.classification)).classification


def _bool_evidence(records: Sequence[EvidenceRecord], field_name: str) -> bool | None:
    values = [getattr(record, field_name) for record in records if getattr(record, field_name) is not None]
    if not values:
        return None
    if any(value is False for value in values):
        return False
    return True


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _classify(
    record_id: str,
    records: Sequence[EvidenceRecord],
    config: GeometryEquivalenceConfig | None = None,
    official_geometry_present: bool = False,
) -> ConsensusEvidenceResult:
    config = config or GeometryEquivalenceConfig()
    candidate = _canonical_candidate(records)
    candidate_wkt = candidate.candidate_wkt if candidate else ""
    candidate_hash = geometry_hash(candidate_wkt)
    snapshot = _snapshot_status(records)
    hard_failures: set[str] = set()
    warnings: set[str] = set()
    support_sources: set[str] = set()
    conflict_sources: set[str] = set()
    support_groups: dict[str, dict[str, Any]] = {}
    reject_groups: dict[str, dict[str, Any]] = {}
    comparisons: dict[str, str] = {}
    candidate_counts = [record.candidate_count for record in records if record.candidate_count is not None]
    candidate_margins = [record.candidate_margin for record in records if record.candidate_margin is not None]
    candidate_count = max(candidate_counts) if candidate_counts else None
    candidate_margin = min(candidate_margins) if candidate_margins else None
    candidate_competition = any(
        "COMPETING_CANDIDATE" in record.hard_failures
        or _text(record.provenance.get("competition_status")).upper() == "LOW_MARGIN"
        or (record.candidate_count is not None and record.candidate_count > 1 and record.candidate_margin is not None and record.candidate_margin < 0.08)
        for record in records
    )
    if candidate_competition:
        hard_failures.add("COMPETING_CANDIDATE")

    if candidate is None:
        if any(record.candidate_wkt for record in records):
            hard_failures.add("INVALID_WKT")
        else:
            warnings.add("NO_CANDIDATE_GEOMETRY")
    else:
        for record in records:
            if not record.candidate_wkt:
                continue
            comparison = compare_geometry_candidates(candidate, record, config)
            comparisons[record.source] = comparison
            if comparison in {"DIFFERENT", "UNKNOWN"}:
                conflict_sources.add(record.source)
                hard_failures.add("CANDIDATE_GEOMETRY_MISMATCH" if comparison == "DIFFERENT" else "INVALID_WKT")
            elif comparison == "PARTIAL_OVERLAP":
                warnings.add(f"PARTIAL_GEOMETRY_OVERLAP:{record.source}")

    if snapshot == "SNAPSHOT_CONFLICT":
        hard_failures.add("SNAPSHOT_CONFLICT")
    elif snapshot == "SNAPSHOT_PARTIAL":
        warnings.add("SNAPSHOT_PROVENANCE_PARTIAL")

    for record in records:
        direction = _direction(record)
        group = (record.independent_group or record.family or record.source).upper()
        source_group_fallback = {
            "GEOMETRY_VALIDATOR": "GEOMETRY_VALIDATION",
            "VALIDATOR": "GEOMETRY_VALIDATION",
            "BOUNDARY_AUDIT": "BOUNDARY_CHAIN",
            "BOUNDARY": "BOUNDARY_CHAIN",
            "NAME_RECOVERY": "BOUNDARY_CHAIN",
            "HUMAN_REVIEW": "HUMAN_REVIEW",
            "HUMAN": "HUMAN_REVIEW",
        }
        if group not in INDEPENDENT_SUPPORT_GROUPS and record.source.upper() in source_group_fallback:
            group = source_group_fallback[record.source.upper()]
        candidate_relevant = record.source != "street_resolution" and (
            bool(record.candidate_wkt) or record.source == "name_recovery"
        )
        if direction == "SUPPORT" and candidate_relevant and (not record.candidate_wkt or comparisons.get(record.source) in {"EXACT", "NEAR_EQUIVALENT", None}):
            support_sources.add(record.source)
            if group in INDEPENDENT_SUPPORT_GROUPS:
                bucket = support_groups.setdefault(group, {"family": record.family, "sources": set(), "strengths": []})
                bucket["sources"].add(record.source)
                bucket["strengths"].append(_strength(record))
        if direction == "REJECT":
            reject_sources = record.source
            conflict_sources.add(reject_sources)
            if group in INDEPENDENT_SUPPORT_GROUPS:
                bucket = reject_groups.setdefault(group, {"family": record.family, "sources": set(), "strengths": []})
                bucket["sources"].add(record.source)
                bucket["strengths"].append(_strength(record))
        for failure in record.hard_failures:
            hard_failures.add(failure)
        warnings.update(record.warnings)

    # A rejection with an agreeing candidate is still a contradiction if there
    # is positive evidence elsewhere; the final class decides whether it is a
    # conflict or a multi-family rejection.
    support_groups_count = len(support_groups)
    reject_groups_count = len(reject_groups)
    independent_families = []
    for group in sorted(set(support_groups) | set(reject_groups)):
        bucket = support_groups.get(group) or reject_groups.get(group)
        independent_families.append({
            "independent_group": group,
            "evidence_family": bucket["family"],
            "sources": sorted(bucket["sources"]),
            "direction": "SUPPORT" if group in support_groups else "REJECT",
            "strongest": "HIGH" if "HIGH" in bucket["strengths"] else ("MEDIUM" if "MEDIUM" in bucket["strengths"] else "REJECT"),
        })

    geometry_validator_class = _aggregate_class(records, {"geometry_validator"})
    boundary_class = _aggregate_class(records, {"boundary_audit"})
    name_class = _aggregate_class(records, {"name_recovery"})
    route_quality_class = _aggregate_class(records, {"route_quality"})
    human_class = _aggregate_class(records, {"human_review"}) or "UNREVIEWED"
    topology_ok = _bool_evidence([record for record in records if record.source in {"geometry_validator", "route_quality", "route_audit"}], "topology_ok")
    boundary_ok = _bool_evidence([record for record in records if record.source in {"geometry_validator", "boundary_audit"}], "boundary_ok")
    name_ok = _bool_evidence([record for record in records if record.source == "name_recovery"], "name_ok")
    gps_ok = _bool_evidence(records, "gps_ok")
    extension_ok = _bool_evidence(records, "extension_ok")
    component_ok = _bool_evidence([record for record in records if record.source in {"geometry_validator", "route_quality", "route_audit"}], "component_ok")
    codlog_ok = _bool_evidence(records, "codlog_ok")
    comparable_codlogs = {
        _text(record.codlog)
        for record in records
        if record.source != "name_recovery"
        and _text(record.codlog)
        and not (record.source == "boundary_audit" and "|" in _text(record.codlog))
    }
    if len(comparable_codlogs) > 1:
        codlog_ok = False
    elif comparable_codlogs and codlog_ok is not False:
        codlog_ok = True
    if any(record.classification == "NAME_DATA_CONTRADICTION" for record in records):
        hard_failures.add("LEXICAL_CONTRADICTION")
    if any(record.classification in {"KEEP_CONTRADICTION", "BOUNDARIES_REVERSED"} for record in records):
        hard_failures.add("BOUNDARY_CONTRADICTION_CRITICAL")
    if human_class == "REJECTED":
        hard_failures.add("HUMAN_REJECTION")
    if topology_ok is False:
        hard_failures.add("TOPOLOGY_CONFLICT")
    if component_ok is False:
        hard_failures.add("WRONG_COMPONENT")
    if extension_ok is False:
        hard_failures.add("IMPOSSIBLE_LENGTH_DEVIATION")
    if codlog_ok is False:
        hard_failures.add("CODLOG_DIVERGENCE")
    if any(comparison == "PARTIAL_OVERLAP" for comparison in comparisons.values()):
        hard_failures.add("CANDIDATE_GEOMETRY_MISMATCH")

    # The boundary lexical chain is one independent group even though it has
    # two files.  A high/medium pair must agree on the selected candidate.
    strong_groups = [bucket for bucket in support_groups.values() if "HIGH" in bucket["strengths"]]
    medium_groups = [bucket for bucket in support_groups.values() if any(strength in {"HIGH", "MEDIUM"} for strength in bucket["strengths"])]
    explicit_conflict = bool(conflict_sources) and bool(support_sources)
    if conflict_sources and not support_sources and reject_groups_count < 2:
        explicit_conflict = True
    no_critical_boundary = "BOUNDARY_CONTRADICTION_CRITICAL" not in hard_failures
    no_critical_lexical = "LEXICAL_CONTRADICTION" not in hard_failures
    no_competition = not candidate_competition
    # Missing topology/component evidence is not equivalent to validation.
    # HIGH and MEDIUM require explicit positive evidence for both fields.
    valid_topology = topology_ok is True and component_ok is True
    common_candidate = candidate is not None and "CANDIDATE_GEOMETRY_MISMATCH" not in hard_failures and "INVALID_WKT" not in hard_failures
    no_hard_failures = not hard_failures
    high_eligible = (
        len(strong_groups) >= 2
        and snapshot != "SNAPSHOT_CONFLICT"
        and common_candidate
        and no_hard_failures
        and valid_topology
        and no_critical_boundary
        and no_critical_lexical
        and no_competition
        and not explicit_conflict
    )
    medium_eligible = (
        bool(medium_groups)
        and snapshot != "SNAPSHOT_CONFLICT"
        and common_candidate
        and valid_topology
        and no_critical_boundary
        and no_critical_lexical
        and no_competition
        and not explicit_conflict
        and human_class != "REJECTED"
    )
    if high_eligible:
        consensus_class = "CONSENSUS_HIGH"
        reason = "TWO_INDEPENDENT_HIGH_SOURCES_SAME_GEOMETRY"
    elif medium_eligible:
        consensus_class = "CONSENSUS_MEDIUM"
        if "VALIDATED_HIGH" in geometry_validator_class and "BOUNDARIES_VALIDATED_MEDIUM" in boundary_class:
            reason = "VALIDATOR_HIGH_BOUNDARY_MEDIUM_NO_CONFLICT"
        elif len(medium_groups) >= 2:
            reason = "INDEPENDENT_SOURCES_SAME_GEOMETRY_NO_CRITICAL_CONFLICT"
        else:
            reason = "ONE_STRONG_SOURCE_WITH_COHERENT_AUXILIARY_EVIDENCE"
    elif reject_groups_count >= 2 and not support_sources and not explicit_conflict:
        consensus_class = "REJECTED_BY_CONSENSUS"
        reason = "MULTIPLE_INDEPENDENT_SOURCES_REJECT_CANDIDATE"
    elif explicit_conflict or "SNAPSHOT_CONFLICT" in hard_failures or (support_sources and hard_failures):
        consensus_class = "CONFLICTING_EVIDENCE"
        if "CANDIDATE_GEOMETRY_MISMATCH" in hard_failures:
            reason = "CANDIDATE_HASH_MISMATCH"
        elif "SNAPSHOT_CONFLICT" in hard_failures:
            reason = "SNAPSHOT_CONFLICT_REQUIRES_RECONCILIATION"
        elif "HUMAN_REJECTION" in hard_failures:
            reason = "HUMAN_REVIEW_REJECTED_CANDIDATE"
        elif "BOUNDARY_CONTRADICTION_CRITICAL" in hard_failures:
            reason = "BOUNDARY_HIGH_BUT_GEOMETRY_REJECTED"
        else:
            reason = "INDEPENDENT_EVIDENCE_CONFLICT"
    else:
        consensus_class = "INSUFFICIENT_EVIDENCE"
        reason = "INSUFFICIENT_INDEPENDENT_EVIDENCE"

    # Structured score is explanatory only; classification is gate-based.
    score = 0.0
    for bucket in support_groups.values():
        score += 30.0 if "HIGH" in bucket["strengths"] else 18.0
    if candidate is not None:
        score += 18.0 if all(value in {"EXACT", "NEAR_EQUIVALENT"} for value in comparisons.values()) else 0.0
    if topology_ok is True:
        score += 8.0
    if boundary_ok is True:
        score += 7.0
    if name_ok is True:
        score += 4.0
    if component_ok is True:
        score += 5.0
    if gps_ok is True:
        score += 3.0
    if extension_ok is True:
        score += 3.0
    score -= 22.0 * len(hard_failures)
    if candidate_competition:
        score -= 12.0
    score = round(max(0.0, min(100.0, score)), 6)
    if len(hard_failures) > 0 and consensus_class == "CONSENSUS_MEDIUM":
        # Medium may tolerate warnings, never a hard failure.
        consensus_class = "CONFLICTING_EVIDENCE" if support_sources else "INSUFFICIENT_EVIDENCE"
        reason = "HARD_FAILURE_BLOCKS_CONSENSUS"

    if snapshot == "SNAPSHOT_PARTIAL":
        warnings.add("SNAPSHOT_PARTIAL")
    if snapshot == "SNAPSHOT_UNKNOWN":
        warnings.add("SNAPSHOT_UNKNOWN")
    if candidate_competition:
        warnings.add("CANDIDATE_COMPETITION")
    if name_class in {"NAME_AMBIGUOUS", "NAME_NOT_FOUND"}:
        warnings.add("LEXICAL_EVIDENCE_NOT_CRITICAL")
    return ConsensusEvidenceResult(
        id=record_id, candidate_wkt=candidate_wkt, candidate_hash=candidate_hash, snapshot_status=snapshot,
        geometry_validator_class=geometry_validator_class, boundary_class=boundary_class,
        name_recovery_class=name_class, route_quality_class=route_quality_class, human_review_class=human_class,
        independent_evidence_count=support_groups_count, supporting_evidence_count=len(support_sources),
        conflicting_evidence_count=len(conflict_sources), independent_families_json=_json_text(independent_families),
        supporting_sources_json=_json_text(sorted(support_sources)), conflicting_sources_json=_json_text(sorted(conflict_sources)),
        topology_ok=topology_ok, boundary_ok=boundary_ok, name_ok=name_ok, gps_ok=gps_ok,
        extension_ok=extension_ok, component_ok=component_ok, codlog_ok=codlog_ok,
        candidate_competition=candidate_competition, candidate_count=candidate_count, candidate_margin=candidate_margin,
        hard_failure_count=len(hard_failures), hard_failures=tuple(sorted(hard_failures)), consensus_score=score,
        consensus_class=consensus_class, reason=reason, warnings=tuple(sorted(warnings)),
        official_geometry_present=official_geometry_present,
    )


class ConsensusEvidenceEngine:
    """Dependency-aware, deterministic classifier over normalized records."""

    def __init__(self, config: GeometryEquivalenceConfig | None = None):
        self.config = config or GeometryEquivalenceConfig()

    def evaluate_case(self, record_id: str, records: Sequence[EvidenceRecord], official_geometry_present: bool = False) -> ConsensusEvidenceResult:
        return _classify(_text(record_id), records, self.config, official_geometry_present)

    def evaluate(self, records: Iterable[EvidenceRecord], official_ids: set[str] | None = None) -> list[ConsensusEvidenceResult]:
        grouped: dict[str, list[EvidenceRecord]] = defaultdict(list)
        for record in records:
            if record.record_id:
                grouped[record.record_id].append(record)
        official_ids = official_ids or set()
        return [self.evaluate_case(identifier, grouped[identifier], identifier in official_ids) for identifier in _sort_ids(grouped)]


def _sort_ids(values: Iterable[str]) -> list[str]:
    def key(value: str) -> tuple[int, Any]:
        text = _text(value)
        return (0, int(text)) if text.isdigit() else (1, text)
    return sorted({_text(value) for value in values if _text(value)}, key=key)


def _read_official_geometries(root: Path | str = ROOT) -> dict[str, Any]:
    path = Path(root) / "data" / "processed" / "recape_clean.csv"
    if not path.exists():
        return {}
    frame = pd.read_csv(path, dtype=str, encoding="utf-8-sig", keep_default_na=False)
    result: dict[str, Any] = {}
    for _, row in frame.iterrows():
        payload = _json(row.get("path"))
        if not isinstance(payload, list) or len(payload) < 2:
            continue
        coordinates = []
        for item in payload:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                lon, lat = _number(item[0]), _number(item[1])
                if lon is not None and lat is not None:
                    coordinates.append((lon, lat))
        if len(coordinates) < 2:
            continue
        try:
            result[_text(row.get("id"))] = transform(WGS84_TO_METRIC.transform, LineString(coordinates))
        except Exception:
            continue
    return result


def _official_hashes(root: Path | str = ROOT) -> dict[str, str | None]:
    root = Path(root)
    names = (
        "data/processed/recape_clean.csv", "data/processed/notificacoes.csv",
        "data/processed/cruzamento.csv", "data/processed/recapes_sem_cobertura.csv",
        "data/processed/pipeline_run.json", "data/processed/geosampa_coverage_report.json",
        "data/config/street_aliases.csv",
    )
    return {name: _sha256(root / name) if (root / name).exists() else None for name in names}


def _source_maps(records: Sequence[EvidenceRecord]) -> dict[str, dict[str, list[EvidenceRecord]]]:
    result: dict[str, dict[str, list[EvidenceRecord]]] = defaultdict(lambda: defaultdict(list))
    for record in records:
        result[record.source][record.record_id].append(record)
    return result


def _coverage(records: Sequence[EvidenceRecord], total_ids: set[str]) -> dict[str, Any]:
    by_source = _source_maps(records)
    payload: dict[str, Any] = {}
    for source in sorted(by_source):
        ids = set(by_source[source])
        candidate_ids = {identifier for identifier, items in by_source[source].items() if any(item.candidate_wkt for item in items)}
        payload[source] = {
            "population": len(ids), "with_candidate_geometry": len(candidate_ids),
            "coverage_pct_of_population": round(len(ids) / len(total_ids) * 100.0, 6) if total_ids else None,
            "ids_not_in_base_population": len(ids - total_ids),
        }
    return payload


def _population_report(records: Sequence[EvidenceRecord], official_ids: set[str], results: Sequence[ConsensusEvidenceResult]) -> dict[str, Any]:
    maps = _source_maps(records)
    source_ids = {source: set(values) for source, values in ((source, set(mapping)) for source, mapping in ((key, value) for key, value in maps.items()))}
    candidate_ids = {record.record_id for record in records if record.candidate_wkt}
    validator_ids = set(maps.get("geometry_validator", {}))
    boundary_ids = set(maps.get("boundary_audit", {}))
    name_ids = set(maps.get("name_recovery", {}))
    human_ids = set(maps.get("human_review", {}))
    quality_ids = set(maps.get("route_quality", {}))
    total_ids = official_ids or set().union(*source_ids.values()) if source_ids else official_ids
    all_major = quality_ids & validator_ids & boundary_ids & name_ids
    return {
        "population_total": len(total_ids),
        "population_source": "recape_clean.csv" if official_ids else "union_of_mapped_artifacts",
        "population_with_geometry_candidate": len(candidate_ids & total_ids) if total_ids else len(candidate_ids),
        "population_with_validator": len(validator_ids & total_ids) if total_ids else len(validator_ids),
        "population_with_boundary": len(boundary_ids & total_ids) if total_ids else len(boundary_ids),
        "population_with_name_recovery": len(name_ids & total_ids) if total_ids else len(name_ids),
        "population_with_human_review": len(human_ids & total_ids) if total_ids else len(human_ids),
        "population_with_all_major_sources": len(all_major & total_ids) if total_ids else len(all_major),
        "population_common_all_mapped_sources": len(set.intersection(*source_ids.values())) if source_ids and len(source_ids) > 1 else 0,
        "mapped_union_population": len(set.union(*source_ids.values())) if source_ids else 0,
        "class_counts": dict(Counter(result.consensus_class for result in results)),
    }


def _agreement_matrix(records: Sequence[EvidenceRecord]) -> dict[str, Any]:
    source_map = _source_maps(records)
    names = sorted(source_map)
    matrix: dict[str, Any] = {}
    for left, right in combinations(names, 2):
        common = sorted(set(source_map[left]) & set(source_map[right]), key=lambda value: _sort_ids([value])[0])
        comparable = 0
        agreement = 0
        disagreement = 0
        joint_high = 0
        joint_rejection = 0
        comparisons = Counter()
        for identifier in common:
            left_records = source_map[left][identifier]
            right_records = source_map[right][identifier]
            left_record = next((item for item in left_records if item.candidate_wkt), left_records[0])
            right_record = next((item for item in right_records if item.candidate_wkt), right_records[0])
            geometry_comparison = compare_geometry_candidates(left_record, right_record)
            direction_equal = _direction(left_record) == _direction(right_record)
            if geometry_comparison != "UNKNOWN":
                comparable += 1
                comparisons[geometry_comparison] += 1
                if geometry_comparison in {"EXACT", "NEAR_EQUIVALENT"} and direction_equal:
                    agreement += 1
                else:
                    disagreement += 1
            if _strength(left_record) == "HIGH" and _strength(right_record) == "HIGH" and geometry_comparison in {"EXACT", "NEAR_EQUIVALENT"}:
                joint_high += 1
            if _direction(left_record) == "REJECT" and _direction(right_record) == "REJECT":
                joint_rejection += 1
        key = f"{left}__x__{right}"
        left_def, right_def = _definition(left), _definition(right)
        matrix[key] = {
            "source_a": left, "source_b": right, "common_ids": len(common), "comparable_ids": comparable,
            "agreement_rate": agreement / comparable if comparable else None,
            "disagreement_rate": disagreement / comparable if comparable else None,
            "joint_high": joint_high, "joint_rejection": joint_rejection,
            "geometry_comparisons": dict(comparisons),
            "dependent": right in left_def.get("depends_on", []) or left in right_def.get("depends_on", []) or left_def.get("independent_group") == right_def.get("independent_group"),
            "independent_group_a": left_def.get("independent_group", left),
            "independent_group_b": right_def.get("independent_group", right),
        }
    return matrix


def _ablation(records: Sequence[EvidenceRecord], official_ids: set[str], config: GeometryEquivalenceConfig) -> dict[str, Any]:
    engine = ConsensusEvidenceEngine(config)
    source_sets = {
        "all": records,
        "without_boundary": [record for record in records if record.source not in {"boundary_audit", "name_recovery"}],
        "without_name_recovery": [record for record in records if record.source != "name_recovery"],
        "without_validator": [record for record in records if record.source != "geometry_validator"],
        "without_topology": [record for record in records if record.source not in {"route_quality", "route_audit"}],
    }
    report: dict[str, Any] = {}
    baseline = None
    for label, subset in source_sets.items():
        subset_results = engine.evaluate(subset, official_ids)
        counts = Counter(result.consensus_class for result in subset_results)
        payload = {"total": len(subset_results), "high": counts.get("CONSENSUS_HIGH", 0), "medium": counts.get("CONSENSUS_MEDIUM", 0), "conflicting": counts.get("CONFLICTING_EVIDENCE", 0), "insufficient": counts.get("INSUFFICIENT_EVIDENCE", 0), "rejected": counts.get("REJECTED_BY_CONSENSUS", 0)}
        report[label] = payload
        if label == "all":
            baseline = payload
    for label, payload in report.items():
        if label == "all" or baseline is None:
            continue
        payload["delta_vs_all"] = {key: payload[key] - baseline[key] for key in ("high", "medium", "conflicting", "insufficient", "rejected")}
    return report


def _control_metrics(results: Sequence[ConsensusEvidenceResult], accepted: set[str] | None = None) -> dict[str, Any]:
    accepted = accepted or {"CONSENSUS_HIGH", "CONSENSUS_MEDIUM"}
    total = len(results)
    accepted_count = sum(result.consensus_class in accepted for result in results)
    rate = accepted_count / total if total else None
    if total:
        centre = (accepted_count + 1.96 * 1.96 / 2) / (total + 1.96 * 1.96)
        radius = 1.96 * math.sqrt((rate * (1 - rate) + 1.96 * 1.96 / (4 * total)) / total) / (1 + 1.96 * 1.96 / total)
        interval = [max(0.0, centre - radius), min(1.0, centre + radius)]
    else:
        interval = [None, None]
    return {"total": total, "accepted": accepted_count, "rejected_or_not_accepted": total - accepted_count, "acceptance_rate": rate, "acceptance_rate_ci_95": interval}


def _positive_controls(official_geometries: dict[str, Any], data: Sequence[Any], config: GeometryEquivalenceConfig | None = None) -> dict[str, Any]:
    """Run the same gates with a known official candidate geometry.

    ``data`` accepts normalized records (the production path) or result rows
    (kept for small unit-level callers).  The official geometry is a control
    candidate, never a supporting evidence record in the normal population.
    """
    config = config or GeometryEquivalenceConfig()
    if not data:
        return {**_control_metrics([]), "official_geometries": len(official_geometries), "eligible_with_same_candidate": 0, "comparison_rule": "EXACT_OR_NEAR_EQUIVALENT", "false_negative_investigation_required": False}
    if isinstance(data[0], ConsensusEvidenceResult):
        result_by_id = {result.id: result for result in data}
        control_results = []
        for identifier, official_geometry in official_geometries.items():
            result = result_by_id.get(identifier)
            if result and result.candidate_wkt and compare_geometry_candidates(official_geometry.wkt, result.candidate_wkt) in {"EXACT", "NEAR_EQUIVALENT"}:
                control_results.append(result)
        metrics = _control_metrics(control_results)
        metrics.update({"official_geometries": len(official_geometries), "eligible_with_same_candidate": len(control_results), "comparison_rule": "EXACT_OR_NEAR_EQUIVALENT", "false_negative_investigation_required": metrics["accepted"] < len(control_results)})
        return metrics
    by_id: dict[str, list[EvidenceRecord]] = defaultdict(list)
    for record in data:
        by_id[record.record_id].append(record)
    control_results: list[ConsensusEvidenceResult] = []
    for identifier, official_geometry in official_geometries.items():
        if identifier not in by_id:
            continue
        replaced = [replace(record, candidate_wkt=official_geometry.wkt) if record.candidate_wkt else record for record in by_id[identifier]]
        control_results.append(_classify(f"{identifier}::OFFICIAL_GEOMETRY", replaced, config, True))
    metrics = _control_metrics(control_results)
    false_negatives = [result for result in control_results if result.consensus_class not in {"CONSENSUS_HIGH", "CONSENSUS_MEDIUM"}]
    metrics.update({
        "official_geometries": len(official_geometries), "eligible_with_same_candidate": len(control_results),
        "comparison_rule": "EXACT_OR_NEAR_EQUIVALENT", "false_negative_investigation_required": metrics["accepted"] < len(control_results),
        "false_negative_by_class": dict(Counter(result.consensus_class for result in false_negatives)),
        "false_negative_reasons": Counter(result.reason for result in false_negatives).most_common(15),
        "false_negative_hard_failures": Counter(failure for result in false_negatives for failure in result.hard_failures).most_common(15),
    })
    return metrics


def _negative_controls(records: Sequence[EvidenceRecord], official_geometries: dict[str, Any], config: GeometryEquivalenceConfig) -> dict[str, Any]:
    """Deterministic wrong-candidate controls made by replacing persisted candidates.

    This never enters the normal population and never writes an official file.
    """
    by_id: dict[str, list[EvidenceRecord]] = defaultdict(list)
    for record in records:
        by_id[record.record_id].append(record)
    scenarios = ("WRONG_GEOMETRY", "WRONG_BOUNDARY", "PARALLEL_STREET", "CANDIDATE_COMPETITION")
    synthetic: list[ConsensusEvidenceResult] = []
    for identifier in _sort_ids(set(official_geometries) & set(by_id))[:50]:
        geometry = official_geometries[identifier]
        shifted = transform(lambda x, y, z=None: (x + 200.0, y + 200.0), geometry)
        for scenario in scenarios:
            altered: list[EvidenceRecord] = []
            for record in by_id[identifier]:
                candidate_wkt = record.candidate_wkt
                hard_failures = record.hard_failures
                candidate_count, candidate_margin = record.candidate_count, record.candidate_margin
                if scenario == "WRONG_GEOMETRY" and record.source == "geometry_validator":
                    candidate_wkt = shifted.wkt
                elif scenario == "WRONG_BOUNDARY" and record.source == "boundary_audit":
                    candidate_wkt = shifted.wkt
                elif scenario == "PARALLEL_STREET" and record.source in {"route_quality", "route_audit"}:
                    candidate_wkt = shifted.wkt
                elif scenario == "CANDIDATE_COMPETITION" and record.candidate_wkt:
                    candidate_count, candidate_margin = max(candidate_count or 1, 2), 0.01
                altered.append(replace(record, candidate_wkt=candidate_wkt, hard_failures=hard_failures, candidate_count=candidate_count, candidate_margin=candidate_margin))
            result = _classify(f"{identifier}::{scenario}", altered, config, False)
            synthetic.append(result)
    by_scenario = {}
    for scenario in scenarios:
        subset = [result for result in synthetic if result.id.endswith(f"::{scenario}")]
        metrics = _control_metrics(subset)
        metrics["false_acceptance_rate"] = metrics["acceptance_rate"]
        metrics["false_acceptance_rate_ci_95"] = metrics["acceptance_rate_ci_95"]
        by_scenario[scenario] = metrics
    overall = _control_metrics(synthetic)
    overall["false_acceptance_rate"] = overall["acceptance_rate"]
    overall["false_acceptance_rate_ci_95"] = overall["acceptance_rate_ci_95"]
    overall["scenarios"] = by_scenario
    overall["synthetic_cases"] = len(synthetic)
    return overall


def _projected_coverage(official_population_ids: set[str], official_geometry_ids: set[str], results: Sequence[ConsensusEvidenceResult]) -> dict[str, Any]:
    result_by_id = {result.id: result for result in results}
    official_result_ids = {identifier for identifier in official_geometry_ids if identifier in result_by_id}
    high = {result.id for result in results if result.consensus_class == "CONSENSUS_HIGH"}
    high_medium = {result.id for result in results if result.consensus_class in {"CONSENSUS_HIGH", "CONSENSUS_MEDIUM"}}
    official_count = len(official_population_ids)
    return {
        "official_geometry_count": len(official_geometry_ids),
        "official_population_count": official_count,
        "official_geometry_coverage_pct": len(official_geometry_ids) / official_count * 100.0 if official_count else None,
        "official_plus_consensus_high_shadow_count": len(official_geometry_ids | high),
        "official_plus_high_medium_shadow_count": len(official_geometry_ids | high_medium),
        "projected_shadow_high_coverage": len(official_geometry_ids | high) / official_count * 100.0 if official_count else None,
        "projected_shadow_high_medium_coverage": len(official_geometry_ids | high_medium) / official_count * 100.0 if official_count else None,
        "shadow_gain_high_not_official_count": len(high - official_geometry_ids),
        "shadow_gain_high_medium_not_official_count": len(high_medium - official_geometry_ids),
        "official_cases_with_consensus_evaluation": len(official_result_ids),
        "not_official_coverage": True,
    }


def _write_csv(results: Sequence[ConsensusEvidenceResult], path: Path) -> None:
    frame = pd.DataFrame([result.to_row() for result in results], columns=OUTPUT_COLUMNS)
    temporary = path.with_suffix(path.suffix + ".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(temporary, index=False, encoding="utf-8-sig")
    os.replace(temporary, path)


def _result_report(results: Sequence[ConsensusEvidenceResult]) -> dict[str, Any]:
    counts = Counter(result.consensus_class for result in results)
    return {
        "consensus_high": counts.get("CONSENSUS_HIGH", 0),
        "consensus_medium": counts.get("CONSENSUS_MEDIUM", 0),
        "conflicting": counts.get("CONFLICTING_EVIDENCE", 0),
        "insufficient": counts.get("INSUFFICIENT_EVIDENCE", 0),
        "rejected": counts.get("REJECTED_BY_CONSENSUS", 0),
        "class_counts": dict(counts),
        "top_reasons": Counter(result.reason for result in results).most_common(15),
        "top_hard_failures": Counter(failure for result in results for failure in result.hard_failures).most_common(20),
    }


def run_shadow(args: argparse.Namespace, root: Path | str = ROOT) -> dict[str, Any]:
    root = Path(root)
    started = time.perf_counter()
    tracemalloc.start()
    before_hashes = _official_hashes(root)
    records, artifacts = load_evidence_records(root)
    official_geometries = _read_official_geometries(root)
    official_ids = set()
    official_path = root / "data" / "processed" / "recape_clean.csv"
    if official_path.exists():
        official = pd.read_csv(official_path, dtype=str, encoding="utf-8-sig", keep_default_na=False)
        official_ids = {_text(value) for value in official["id"].tolist() if _text(value)}
    ids = _sort_ids({record.record_id for record in records} | official_ids)
    if getattr(args, "only_id", None):
        requested = {_text(value) for value in args.only_id}
        ids = [identifier for identifier in ids if identifier in requested]
    engine = ConsensusEvidenceEngine()
    grouped: dict[str, list[EvidenceRecord]] = defaultdict(list)
    for record in records:
        grouped[record.record_id].append(record)
    if getattr(args, "sample", None):
        ids = ids[: max(0, int(args.sample))]
    evaluation_ids = set(ids)
    results = [engine.evaluate_case(identifier, grouped.get(identifier, []), identifier in official_geometries) for identifier in ids]
    only_class = getattr(args, "only_class", None) or []
    if only_class:
        allowed = {value.upper() for value in only_class}
        results = [result for result in results if result.consensus_class.upper() in allowed]
    if getattr(args, "reset_cache", False):
        # There is intentionally no computational cache.  The flag means a
        # deterministic fresh replacement of the two shadow artifacts.
        pass
    _write_csv(results, root / "data" / "processed" / OUTPUT_CSV.name)
    shadow_csv_path = root / "data" / "processed" / OUTPUT_CSV.name
    after_hashes = _official_hashes(root)
    if before_hashes != after_hashes:
        raise RuntimeError("um output oficial ou protegido foi alterado durante o consensus shadow")
    total_ids = official_ids or set(ids)
    source_records = records
    analysis_records = records if not getattr(args, "sample", None) and not getattr(args, "only_id", None) else [record for record in records if record.record_id in evaluation_ids]
    controls_positive = _positive_controls(official_geometries, analysis_records, engine.config)
    controls_negative = _negative_controls(analysis_records, official_geometries, engine.config)
    human_records = [record for record in source_records if record.source == "human_review"]
    human_review_summary = {
        "available": bool(human_records), "review_rows": len(human_records),
        "approved": sum(record.classification == "APPROVED" for record in human_records),
        "rejected": sum(record.classification == "REJECTED" for record in human_records),
        "unreviewed_or_deferred": sum(record.classification == "UNREVIEWED" for record in human_records),
        "population_precision_recall_calculated": False,
        "use": "CALIBRATION_AND_EVALUATION_ONLY",
    }
    report = {
        "version": VERSION, "mode": "SHADOW_ONLY", "generated_at": datetime.now(timezone.utc).isoformat(),
        "population": _population_report(source_records, official_ids, results),
        "source_coverage": _coverage(source_records, total_ids),
        "source_inventory": [artifact.to_dict() for artifact in artifacts.values()],
        "shadow_artifact_hashes": {OUTPUT_CSV.name: _sha256(shadow_csv_path)},
        "dependency_graph": dependency_graph(),
        "snapshot_alignment": {
            "status_counts": dict(Counter(result.snapshot_status for result in results)),
            "incompatible_cases": sum(result.snapshot_status == "SNAPSHOT_CONFLICT" for result in results),
            "mixed_version_sources": {name: artifact.version_values for name, artifact in artifacts.items() if len(artifact.version_values) > 1},
            "partial_cases": sum(result.snapshot_status == "SNAPSHOT_PARTIAL" for result in results),
            "unknown_cases": sum(result.snapshot_status == "SNAPSHOT_UNKNOWN" for result in results),
            "alignment_rule": "snapshot_id/generation/input_hash agreement; module versions are provenance, not cross-module equality",
        },
        **_result_report(results),
        "independent_family_distribution": dict(Counter(
            family["independent_group"]
            for result in results
            for family in json.loads(result.independent_families_json)
        )),
        "controls_positive": controls_positive,
        "controls_negative": controls_negative,
        "human_review": human_review_summary,
        "false_acceptance_rate": controls_negative.get("false_acceptance_rate"),
        "false_acceptance_rate_ci_95": controls_negative.get("false_acceptance_rate_ci_95"),
        "positive_acceptance_rate": controls_positive.get("acceptance_rate"),
        "positive_acceptance_rate_ci_95": controls_positive.get("acceptance_rate_ci_95"),
        "agreement_matrix": _agreement_matrix(analysis_records),
        "ablation": _ablation(analysis_records, evaluation_ids & official_ids, engine.config),
        "projected_coverage": _projected_coverage(official_ids, set(official_geometries), results),
        "official_promotions_applied": 0,
        "official_geometry_mutation": False,
        "official_output_hashes_before": before_hashes,
        "official_output_hashes_after": after_hashes,
        "protected_hashes_unchanged": before_hashes == after_hashes,
        "geometry_equivalence_config": asdict(engine.config),
        "limitations": [
            "No persisted common run identifier exists across all artifacts; incomplete provenance is reported as PARTIAL/UNKNOWN.",
            "Boundary name recovery shares the BOUNDARY_CHAIN independent group with boundary audit.",
            "Human review is calibration evidence only; no decision is overwritten or promoted.",
            "Population-level precision/recall is not estimated from the small human sample.",
            "Projected coverage is a shadow simulation, never official coverage.",
        ],
        "runtime_seconds": round(time.perf_counter() - started, 6),
        "peak_memory_mb": round(tracemalloc.get_traced_memory()[1] / (1024 * 1024), 6),
    }
    tracemalloc.stop()
    report_path = root / "data" / "processed" / OUTPUT_REPORT.name
    temporary = report_path.with_suffix(report_path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False), encoding="utf-8")
    os.replace(temporary, report_path)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Consensus Evidence Engine em modo shadow")
    parser.add_argument("--shadow", action="store_true", help="habilita a execução somente shadow")
    parser.add_argument("--sample", type=int, default=None, help="processa os primeiros N IDs de forma determinística")
    parser.add_argument("--only-id", action="append", default=[], help="limita a IDs; pode ser repetido")
    parser.add_argument("--only-class", action="append", default=[], help="limita a classes finais; pode ser repetido")
    parser.add_argument("--resume", action="store_true", help="mantido por compatibilidade; joins são baratos e determinísticos")
    parser.add_argument("--reset-cache", action="store_true", help="força substituição limpa dos artefatos shadow; não há cache de geometria")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.shadow:
        print("Modo seguro: use --shadow; nenhum arquivo foi alterado.", file=sys.stderr)
        return 2
    report = run_shadow(args)
    print(json.dumps({
        "population_total": report["population"]["population_total"],
        "processed": sum(report[key] for key in ("consensus_high", "consensus_medium", "conflicting", "insufficient", "rejected")),
        "consensus_high": report["consensus_high"], "consensus_medium": report["consensus_medium"],
        "conflicting": report["conflicting"], "insufficient": report["insufficient"], "rejected": report["rejected"],
        "official_promotions_applied": report["official_promotions_applied"],
        "protected_hashes_unchanged": report["protected_hashes_unchanged"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
