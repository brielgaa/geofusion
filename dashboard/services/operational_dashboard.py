"""Application adapter for the frozen GeoFusion operational layer.

The dashboard consumes the read-only repository and services through this module.
It owns presentation-oriented denormalization, while lookup and protection
semantics remain in ``src.operational``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st
from pyproj import Transformer
from shapely import wkt
from shapely.ops import transform

from src.operational.models import OperationalLocationResult, StreetLookupQuery
from src.operational.repository import OperationalRepository, RecapeRecord
from src.operational.services import OperationalQueryService, calculate_protection_status


WGS84_FROM_METRIC = Transformer.from_crs("EPSG:31983", "EPSG:4326", always_xy=True)
REFERENCE_DATE = date.today()


@dataclass
class OperationalContext:
    project_dir: Path
    repository: OperationalRepository
    service: OperationalQueryService
    recapes: pd.DataFrame
    notifications: pd.DataFrame
    crossmatch: pd.DataFrame
    pipeline_run: dict[str, Any]
    coverage_report: dict[str, Any]
    errors: list[str]
    signature: tuple[tuple[str, int, int], ...]


def dataset_signature(project_dir: Path) -> tuple[tuple[str, int, int], ...]:
    """Return a cheap invalidation key for the operational artifacts."""
    relative_paths = (
        "data/processed/recape_clean.csv",
        "data/processed/notificacoes.csv",
        "data/processed/cruzamento.csv",
        "data/processed/route_geometry_quality_shadow.csv",
        "data/processed/geometry_validation_shadow.csv",
        "data/processed/consensus_evidence_shadow.csv",
        "data/processed/pipeline_run.json",
        "data/processed/geosampa_coverage_report.json",
        "data/cache/geosampa_segmento_logradouro.geojson",
        "data/processed/operational_lookup_index.sqlite",
        "data/processed/operational_lookup_index_metadata.json",
    )
    result: list[tuple[str, int, int]] = []
    for relative in relative_paths:
        path = project_dir / relative
        try:
            stat = path.stat()
            result.append((relative, int(stat.st_mtime_ns), int(stat.st_size)))
        except OSError:
            result.append((relative, 0, 0))
    return tuple(result)


def _read_csv(path: Path) -> tuple[pd.DataFrame, str | None]:
    if not path.exists():
        return pd.DataFrame(), f"Arquivo ausente: {path.name}"
    try:
        frame = pd.read_csv(path, low_memory=False, encoding="utf-8-sig")
    except UnicodeDecodeError:
        frame = pd.read_csv(path, low_memory=False, encoding="latin-1")
    except (OSError, pd.errors.ParserError) as exc:
        return pd.DataFrame(), f"Não foi possível ler {path.name}: {exc}"
    return frame, None


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError):
        return {}


def _path_coordinates(value: Any) -> list[list[float]] | None:
    if isinstance(value, list) and len(value) >= 2:
        return [[float(pair[0]), float(pair[1])] for pair in value if len(pair) >= 2]
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        payload = json.loads(value)
        if isinstance(payload, dict):
            payload = payload.get("coordinates")
        if isinstance(payload, list) and len(payload) >= 2:
            return [[float(pair[0]), float(pair[1])] for pair in payload if len(pair) >= 2]
    except (TypeError, ValueError):
        return None
    return None


def _wkt_coordinates(value: Any) -> list[list[float]] | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        geometry = wkt.loads(value)
        geometry = transform(WGS84_FROM_METRIC.transform, geometry)
        if geometry.geom_type == "LineString":
            return [[float(x), float(y)] for x, y in geometry.coords]
    except Exception:  # malformed shadow geometry is a visible quality issue, not a page failure
        return None
    return None


def _shadow_quality(shadow: dict[str, Any], validator: dict[str, Any]) -> tuple[str, str, str]:
    value = str(shadow.get("geometry_confidence") or validator.get("geometry_confidence") or "").upper()
    if "HIGH" in value:
        return "SHADOW_HIGH", "Alta", "route_geometry_quality_shadow.csv"
    if "MEDIUM" in value:
        return "SHADOW_MEDIUM", "Média", "route_geometry_quality_shadow.csv"
    if "ESTIM" in value or value:
        return "ESTIMATED", "Estimada", "route_geometry_quality_shadow.csv"
    return "UNRESOLVED", "Não resolvida", ""


def _record_row(record: RecapeRecord, repository: OperationalRepository) -> dict[str, Any]:
    shadow = repository.shadow_quality_by_id.get(record.record_id, {})
    validator = repository.validator_by_id.get(record.record_id, {})
    consensus = repository.consensus_by_id.get(record.record_id, {})
    if record.geometry is not None:
        quality_code, quality_label, quality_source = "OFFICIAL", "Oficial", "recape_clean.csv:path"
        map_path = _path_coordinates(record.source_row.get("path"))
    else:
        quality_code, quality_label, quality_source = _shadow_quality(shadow, validator)
        map_path = _wkt_coordinates(shadow.get("geometry_wkt") or validator.get("geometry_wkt"))
    row = dict(record.source_row)
    row.update(
        {
            "record_id": record.record_id,
            "street_display": record.street or record.source_row.get("via") or "—",
            "surface_display": record.raw_surface_type or "Não informado",
            "quality_code": quality_code,
            "quality_label": quality_label,
            "quality_source": quality_source,
            "map_path": map_path,
            "shadow_confidence": shadow.get("geometry_confidence") or validator.get("geometry_confidence") or "",
            "validation_class": validator.get("validation_class") or "",
            "consensus_class": consensus.get("consensus_class") or "",
            "root_causes": shadow.get("root_causes") or validator.get("root_causes") or "",
            "has_official_geometry": record.geometry is not None,
            "completion_date": record.resurfacing_date,
            "date_type": record.date_type or "",
            "date_source": "data/processed/recape_clean.csv:data_termino" if record.resurfacing_date else "",
        }
    )
    protection = calculate_protection_status(
        record.resurfacing_date,
        REFERENCE_DATE,
        date_type=record.date_type,
        date_source=row["date_source"] or None,
    )
    row.update(
        {
            "protection_status": protection.status,
            "protection_start": protection.start_date,
            "protection_end": protection.end_date,
            "days_remaining": protection.days_remaining,
        }
    )
    return row


@st.cache_resource(show_spinner="Preparando índices operacionais…")
def load_operational_context(
    project_dir: str,
    signature: tuple[tuple[str, int, int], ...],
) -> OperationalContext:
    root = Path(project_dir)
    repository = OperationalRepository(root)
    service = OperationalQueryService(repository)
    errors: list[str] = []
    repository._load_lookup_index()
    lookup_status = repository.lookup_index_status
    if not lookup_status.get("valid"):
        errors.append(
            "Índice textual persistido indisponível ou obsoleto "
            f"({lookup_status.get('reason', 'UNKNOWN')}); o lookup usará o fallback GeoJSON. "
            "Execute `python -m src.operational.build_lookup_index --root . --benchmark`."
        )
    recape_records = repository.recapes
    rows = [_record_row(record, repository) for record in recape_records]
    recapes = pd.DataFrame(rows)
    notifications, error = _read_csv(root / "data" / "processed" / "notificacoes.csv")
    if error:
        errors.append(error)
    crossmatch, error = _read_csv(root / "data" / "processed" / "cruzamento.csv")
    if error:
        errors.append(error)
    return OperationalContext(
        project_dir=root,
        repository=repository,
        service=service,
        recapes=recapes,
        notifications=notifications,
        crossmatch=crossmatch,
        pipeline_run=_read_json(root / "data" / "processed" / "pipeline_run.json"),
        coverage_report=_read_json(root / "data" / "processed" / "geosampa_coverage_report.json"),
        errors=errors,
        signature=signature,
    )


def lookup(context: OperationalContext, query: StreetLookupQuery) -> OperationalLocationResult:
    return context.service.lookup(query, reference_date=REFERENCE_DATE)


def format_record_id(value: Any) -> str:
    return str(value).strip() if value is not None else ""
