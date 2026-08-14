"""Read-only data access and reusable indexes for the operational layer."""
from __future__ import annotations

import csv
import json
import math
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
from pyproj import Transformer
from shapely.geometry import LineString
from shapely.ops import transform
from shapely.strtree import STRtree

try:
    from transform import normalizar_rua
except ImportError:  # pragma: no cover - package import
    from ..transform import normalizar_rua

from .models import StreetLookupCandidate


ROOT = Path(__file__).resolve().parents[2]
WGS84_TO_METRIC = Transformer.from_crs("EPSG:4326", "EPSG:31983", always_xy=True)
CSV_FIELD_LIMIT = 2**31 - 1
try:
    csv.field_size_limit(CSV_FIELD_LIMIT)
except OverflowError:  # pragma: no cover - platform dependent
    csv.field_size_limit(2**30)


@dataclass(frozen=True)
class SegmentRecord:
    segment_id: str
    street: str
    normalized_street: str
    codlog: str | None
    geometry: Any
    number_initial_even: int | None
    number_final_even: int | None
    number_initial_odd: int | None
    number_final_odd: int | None
    source: str

    def number_range(self, number: int | None) -> tuple[int | None, int | None]:
        if number is None:
            return None, None
        if number % 2:
            return self.number_initial_odd, self.number_final_odd
        return self.number_initial_even, self.number_final_even

    def matches_number(self, number: int) -> bool:
        start, end = self.number_range(number)
        return start is not None and end is not None and min(start, end) <= number <= max(start, end)

    def to_candidate(self, *, number: int | None = None, distance_m: float | None = None) -> StreetLookupCandidate:
        start, end = self.number_range(number)
        return StreetLookupCandidate(
            segment_id=self.segment_id,
            street=self.street,
            normalized_street=self.normalized_street,
            codlog=self.codlog,
            geometry_wkt=self.geometry.wkt if self.geometry is not None else None,
            number_match="MATCHED" if number is not None and self.matches_number(number) else ("RANGE_AVAILABLE" if number is not None else "NOT_REQUESTED"),
            number_range={"start": start, "end": end},
            distance_to_segment_m=round(float(distance_m), 6) if distance_m is not None else None,
            source=self.source,
        )


@dataclass(frozen=True)
class RecapeRecord:
    record_id: str
    street: str
    normalized_streets: tuple[str, ...]
    status: str
    subprefecture: str | None
    raw_surface_type: str | None
    resurfacing_date: date | None
    date_type: str | None
    geometry: Any
    geometry_wkt: str | None
    source_file: str
    status_path: str
    source_row: dict[str, Any]


class OperationalRepository:
    """Repository with lazy CSV/GeoJSON loading and reusable spatial indexes."""

    def __init__(self, root: Path | str = ROOT, *, use_persisted_lookup_index: bool = True):
        self.root = Path(root)
        self._use_persisted_lookup_index = use_persisted_lookup_index
        self.processed = self.root / "data" / "processed"
        self.cache = self.root / "data" / "cache"
        self._recapes: list[RecapeRecord] | None = None
        self._recape_by_id: dict[str, RecapeRecord] | None = None
        self._recapes_by_street: dict[str, list[RecapeRecord]] | None = None
        self._recape_tree: STRtree | None = None
        self._recape_tree_records: list[RecapeRecord] = []
        self._segments: list[SegmentRecord] | None = None
        self._segments_by_street: dict[str, list[SegmentRecord]] | None = None
        self._lookup_index: Any | None = None
        self._lookup_street_mapping: Any | None = None
        self._lookup_index_checked = False
        self._lookup_index_status: dict[str, Any] | None = None
        self._segment_tree: STRtree | None = None
        self._segment_tree_records: list[SegmentRecord] = []
        self._consensus: dict[str, dict[str, Any]] | None = None
        self._validator: dict[str, dict[str, Any]] | None = None
        self._shadow_quality: dict[str, dict[str, Any]] | None = None

    @property
    def recapes(self) -> list[RecapeRecord]:
        if self._recapes is None:
            frame = self._read_frame("recape_clean.csv")
            records: list[RecapeRecord] = []
            for row in frame.to_dict("records"):
                identifier = self._text(row.get("id"))
                if not identifier:
                    continue
                street_values = {
                    self._normalize(row.get("rua_norm")),
                    self._normalize(row.get("logradouro_geosampa")),
                    self._normalize(row.get("via")),
                }
                street_values.discard("")
                geometry = self._path_geometry(row.get("path"))
                status = self._text(row.get("status"))
                completion_date = self._parse_date(row.get("data_termino"))
                usable_date = completion_date if status in {"CONCLUIDO", "CONCLUIDO_RATIFICAR"} else None
                date_type = "COMPLETION_DATE" if usable_date else None
                records.append(RecapeRecord(
                    record_id=identifier,
                    street=self._text(row.get("logradouro_geosampa") or row.get("via")),
                    normalized_streets=tuple(sorted(street_values)),
                    status=status,
                    subprefecture=self._text(row.get("subprefeitura")) or None,
                    raw_surface_type=self._text(row.get("revestimento")) or None,
                    resurfacing_date=usable_date,
                    date_type=date_type,
                    geometry=geometry,
                    geometry_wkt=geometry.wkt if geometry is not None else None,
                    source_file="data/processed/recape_clean.csv",
                    status_path=self._text(row.get("status_path")),
                    source_row=row,
                ))
            self._recapes = records
            self._recape_by_id = {record.record_id: record for record in records}
            by_street: dict[str, list[RecapeRecord]] = defaultdict(list)
            for record in records:
                for street in record.normalized_streets:
                    by_street[street].append(record)
            self._recapes_by_street = by_street
            self._recape_tree_records = [record for record in records if record.geometry is not None]
            self._recape_tree = STRtree([record.geometry for record in self._recape_tree_records]) if self._recape_tree_records else None
        return self._recapes

    @property
    def recape_by_id(self) -> dict[str, RecapeRecord]:
        self.recapes
        return self._recape_by_id or {}

    @property
    def recapes_by_street(self) -> dict[str, list[RecapeRecord]]:
        self.recapes
        return self._recapes_by_street or {}

    @property
    def segments(self) -> list[SegmentRecord]:
        if self._segments is None:
            path = self.cache / "geosampa_segmento_logradouro.geojson"
            payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"features": []}
            segments: list[SegmentRecord] = []
            for feature in payload.get("features", []):
                properties = feature.get("properties") or {}
                geometry_payload = feature.get("geometry") or {}
                coordinates = geometry_payload.get("coordinates")
                if geometry_payload.get("type") != "LineString" or not isinstance(coordinates, list) or len(coordinates) < 2:
                    continue
                try:
                    geometry = LineString([(float(x), float(y)) for x, y, *rest in coordinates])
                except (TypeError, ValueError):
                    continue
                street = self._text(properties.get("nm_logradouro"))
                normalized = self._normalize(street)
                if not normalized:
                    continue
                segments.append(SegmentRecord(
                    segment_id=self._text(feature.get("id") or properties.get("cd_identificador")),
                    street=street,
                    normalized_street=normalized,
                    codlog=self._text(properties.get("codlog")) or None,
                    geometry=geometry,
                    number_initial_even=self._integer(properties.get("cd_numero_inicial_par")),
                    number_final_even=self._integer(properties.get("cd_numero_final_par")),
                    number_initial_odd=self._integer(properties.get("cd_numero_inicial_impar")),
                    number_final_odd=self._integer(properties.get("cd_numero_final_impar")),
                    source="data/cache/geosampa_segmento_logradouro.geojson",
                ))
            self._segments = segments
            by_street: dict[str, list[SegmentRecord]] = defaultdict(list)
            for segment in segments:
                by_street[segment.normalized_street].append(segment)
            self._segments_by_street = by_street
            self._segment_tree_records = segments
            self._segment_tree = STRtree([segment.geometry for segment in segments]) if segments else None
        return self._segments

    @property
    def lookup_index_status(self) -> dict[str, Any]:
        if self._lookup_index_status is None:
            from .lookup_index import index_status

            self._lookup_index_status = index_status(self.root)
        return self._lookup_index_status

    def _load_lookup_index(self) -> None:
        if self._lookup_index_checked or not self._use_persisted_lookup_index:
            return
        self._lookup_index_checked = True
        from .lookup_index import LazyStreetMapping, open_valid_index

        self._lookup_index = open_valid_index(self.root)
        if self._lookup_index is not None:
            self._lookup_street_mapping = LazyStreetMapping(self._lookup_index)
        elif self.lookup_index_status.get("valid"):
            self._lookup_index_status = {
                **self.lookup_index_status,
                "valid": False,
                "reason": "INDEX_UNREADABLE",
            }

    @property
    def segments_by_street(self) -> dict[str, list[SegmentRecord]]:
        self._load_lookup_index()
        if self._lookup_street_mapping is not None:
            return self._lookup_street_mapping
        self.segments
        return self._segments_by_street or {}

    @property
    def consensus_by_id(self) -> dict[str, dict[str, Any]]:
        if self._consensus is None:
            frame = self._read_frame("consensus_evidence_shadow.csv")
            self._consensus = {self._text(row.get("id")): row for row in frame.to_dict("records") if self._text(row.get("id"))}
        return self._consensus

    @property
    def validator_by_id(self) -> dict[str, dict[str, Any]]:
        if self._validator is None:
            frame = self._read_frame("geometry_validation_shadow.csv")
            self._validator = {self._text(row.get("id")): row for row in frame.to_dict("records") if self._text(row.get("id"))}
        return self._validator

    @property
    def shadow_quality_by_id(self) -> dict[str, dict[str, Any]]:
        if self._shadow_quality is None:
            frame = self._read_frame("route_geometry_quality_shadow.csv")
            self._shadow_quality = {self._text(row.get("id")): row for row in frame.to_dict("records") if self._text(row.get("id"))}
        return self._shadow_quality

    def spatial_segments(self, latitude: float, longitude: float, *, max_distance_m: float = 50.0, tie_tolerance_m: float = 1.0) -> list[tuple[SegmentRecord, float]]:
        if self._segment_tree is None:
            self.segments
        if self._segment_tree is None:
            return []
        from shapely.geometry import Point
        point = transform(WGS84_TO_METRIC.transform, Point(float(longitude), float(latitude)))
        indices, distances = self._segment_tree.query_nearest(point, all_matches=True, return_distance=True, exclusive=False)
        pairs = sorted(((self._segment_tree_records[int(index)], float(distance)) for index, distance in zip(indices, distances)), key=lambda item: (item[1], item[0].segment_id))
        if not pairs or pairs[0][1] > max_distance_m:
            return []
        closest = pairs[0][1]
        return [(segment, distance) for segment, distance in pairs if distance <= closest + tie_tolerance_m]

    def spatial_recapes(self, latitude: float, longitude: float, *, max_distance_m: float = 50.0) -> list[tuple[RecapeRecord, float]]:
        if self._recape_tree is None:
            self.recapes
        if self._recape_tree is None:
            return []
        from shapely.geometry import Point
        point = transform(WGS84_TO_METRIC.transform, Point(float(longitude), float(latitude)))
        indices, distances = self._recape_tree.query_nearest(point, all_matches=True, return_distance=True, exclusive=False)
        pairs = sorted(((self._recape_tree_records[int(index)], float(distance)) for index, distance in zip(indices, distances)), key=lambda item: (item[1], item[0].record_id))
        return [(record, distance) for record, distance in pairs if distance <= max_distance_m]

    def inventory(self, *, reference_date: date | None = None) -> dict[str, Any]:
        """Build the factual data inventory and capability report."""
        reference_date = reference_date or date.today()
        entries = [
            self._inventory_csv("data/raw/sgz_156.csv", delimiter="|", source_type="raw_notification", notes=["pipe-delimited source"]),
            self._inventory_csv("data/raw/sgz_convias.csv", delimiter="|", source_type="raw_notification", notes=["pipe-delimited source"]),
            self._inventory_xlsx("data/raw/recape.xlsx", source_type="raw_resurfacing"),
            self._inventory_csv("data/processed/recape_clean.csv", source_type="official_resurfacing", notes=["official normalized ETL output; path is stored in WGS84 coordinate pairs"]),
            self._inventory_csv("data/processed/notificacoes.csv", source_type="official_notification", notes=["data_recebimento is notification receipt time"]),
            self._inventory_csv("data/processed/os_unificado.csv", source_type="official_notification_union"),
            self._inventory_csv("data/processed/cruzamento.csv", source_type="official_crossmatch", notes=["notification/resurfacing crossmatch; not an execution ledger"]),
            self._inventory_csv("data/processed/recapes_sem_cobertura.csv", source_type="official_route_failures"),
            self._inventory_csv("data/processed/street_resolution_audit.csv", source_type="diagnostic_street_resolution"),
            self._inventory_csv("data/processed/route_geometry_quality_shadow.csv", source_type="shadow_geometry_quality"),
            self._inventory_csv("data/processed/geometry_validation_shadow.csv", source_type="shadow_geometry_validation"),
            self._inventory_csv("data/processed/consensus_evidence_shadow.csv", source_type="shadow_consensus"),
            self._inventory_csv("data/config/street_aliases.csv", source_type="configuration", notes=["no active alias rows in current file"]),
            self._inventory_geojson("data/cache/geosampa_segmento_logradouro.geojson", source_type="geosampa_road_segments"),
        ]
        recapes = self.recapes
        surface_rows = [record for record in recapes if record.raw_surface_type]
        path_rows = [record for record in recapes if record.geometry is not None]
        completed_rows = [record for record in recapes if record.resurfacing_date is not None]
        date_status = self._protection_distribution(recapes, reference_date)
        range_segments = [segment for segment in self.segments if any(value is not None for value in (segment.number_initial_even, segment.number_final_even, segment.number_initial_odd, segment.number_final_odd))]
        street_range_streets = {segment.normalized_street for segment in range_segments}
        surface_values = Counter(record.raw_surface_type for record in surface_rows)
        capabilities = {
            "street_lookup": {
                "supported": bool(self.segments),
                "methods": ["STREET_EXACT", "STREET_AND_NUMBER_RANGE", "COORDINATE_NEAREST", "RECORD_ID"],
                "source": "data/cache/geosampa_segmento_logradouro.geojson",
                "segment_count": len(self.segments),
                "coordinate_crs": "EPSG:4326 input -> EPSG:31983 index",
                "coordinate_max_distance_m": 50.0,
            },
            "street_number_lookup": {
                "supported": bool(range_segments),
                "method": "NUMBER_RANGE",
                "streets_with_number_ranges": len(street_range_streets),
                "segments_with_number_ranges": len(range_segments),
                "coverage_pct_of_segments": round(len(range_segments) / len(self.segments) * 100.0, 6) if self.segments else 0.0,
                "exact_address_points": False,
            },
            "surface_lookup": {
                "supported": bool(surface_rows),
                "method": "RECAPE_RECORD_OR_OFFICIAL_PATH",
                "segment_level_surface_supported": False,
                "total_recape_records": len(recapes),
                "records_with_surface": len(surface_rows),
                "surface_coverage_pct": round(len(surface_rows) / len(recapes) * 100.0, 6) if recapes else 0.0,
                "official_path_records": len(path_rows),
                "official_path_records_with_surface": sum(record.raw_surface_type is not None for record in path_rows),
                "raw_surface_distribution": dict(surface_values),
                "normalization": "identity_only; raw values are preserved and not blindly consolidated",
            },
            "resurfacing_lookup": {
                "supported": True,
                "methods": ["RECORD_ID", "STREET_EXACT", "COORDINATE_OFFICIAL_PATH"],
                "total_resurfacing_records": len(recapes),
                "with_official_path": len(path_rows),
                "history_supported": True,
            },
            "resurfacing_date": {
                "field": "data_termino",
                "date_type": "COMPLETION_DATE",
                "source": "data/processed/recape_clean.csv",
                "usable_only_for_statuses": ["CONCLUIDO", "CONCLUIDO_RATIFICAR"],
                "total_resurfacing": len(recapes),
                "with_usable_date": len(completed_rows),
                "without_usable_date": len(recapes) - len(completed_rows),
                "notification_date_is_not_execution_date": True,
            },
            "protection": {
                "supported": "partial",
                "window_years": 1,
                "reference_date": reference_date.isoformat(),
                "counts": date_status,
                "unknown_reason": "no explicit completion date or non-completed status",
            },
            "intervention_execution_date": {"supported": False, "reason": "current notification sources expose data_recebimento, not execution_date"},
            "temporal_violation_detection": {"supported": False, "reason": "execution_date is absent; notification_date is never substituted"},
            "official_vs_shadow_geometry": {"official_path_records": len(path_rows), "shadow_geometry_is_separate": True, "shadow_never_replaces_official": True},
        }
        return {
            "version": "operational-data-layer-v1",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "reference_date": reference_date.isoformat(),
            "datasets": entries,
            "capabilities": capabilities,
            "limitations": [
                "GeoSampa supplies address ranges for road segments, but not exact address points.",
                "Recape surface is record-level; no authoritative surface-by-road-segment table was found.",
                "data_termino is treated as a completion-date field only for completed statuses; it is not notification_date.",
                "No execution_date/intervention event ledger is currently available.",
            ],
        }

    def write_inventory(self, path: Path | str | None = None, *, reference_date: date | None = None) -> dict[str, Any]:
        target = Path(path) if path else self.processed / "operational_data_capabilities.json"
        payload = self.inventory(reference_date=reference_date)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        return payload

    def _read_frame(self, filename: str) -> pd.DataFrame:
        path = self.processed / filename
        if not path.exists():
            return pd.DataFrame()
        return pd.read_csv(path, dtype=str, encoding="utf-8-sig", keep_default_na=False)

    def _inventory_csv(self, relative: str, *, delimiter: str = ",", source_type: str, notes: list[str] | None = None) -> dict[str, Any]:
        path = self.root / relative
        if not path.exists():
            return {"file": relative, "source": relative, "row_count": 0, "notes": ["missing"]}
        rows: list[dict[str, str]] = []
        encoding = "utf-8-sig"
        try:
            with path.open("r", encoding=encoding, newline="") as handle:
                reader = csv.DictReader(handle, delimiter=delimiter)
                columns = list(reader.fieldnames or [])
                for row in reader:
                    rows.append(row)
        except UnicodeDecodeError:
            encoding = "latin-1"
            with path.open("r", encoding=encoding, newline="") as handle:
                reader = csv.DictReader(handle, delimiter=delimiter)
                columns = list(reader.fieldnames or [])
                rows = list(reader)
        return self._inventory_payload(relative, source_type, columns, rows, encoding, notes or [])

    def _inventory_xlsx(self, relative: str, *, source_type: str) -> dict[str, Any]:
        path = self.root / relative
        if not path.exists():
            return {"file": relative, "source": relative, "row_count": 0, "notes": ["missing"]}
        sheets = pd.ExcelFile(path).sheet_names
        rows: list[dict[str, Any]] = []
        columns: list[str] = []
        sheet_name = sheets[0] if sheets else ""
        if sheet_name:
            frame = pd.read_excel(path, sheet_name=sheet_name, dtype=object)
            columns = [str(column) for column in frame.columns]
            rows = frame.where(pd.notna(frame), "").to_dict("records")
        payload = self._inventory_payload(relative, source_type, columns, rows, "xlsx", [f"sheet={sheet_name}"])
        payload["sheets"] = sheets
        return payload

    def _inventory_geojson(self, relative: str, *, source_type: str) -> dict[str, Any]:
        path = self.root / relative
        if not path.exists():
            return {"file": relative, "source": relative, "row_count": 0, "notes": ["missing"]}
        payload = json.loads(path.read_text(encoding="utf-8"))
        features = payload.get("features", [])
        properties = [feature.get("properties") or {} for feature in features]
        columns = sorted({key for row in properties for key in row})
        entry = self._inventory_payload(relative, source_type, columns, properties, "utf-8", [])
        entry.update({"geometry_field": "geometry", "crs": (payload.get("crs") or {}).get("properties", {}).get("name", "EPSG:31983"), "row_count": len(features)})
        return entry

    def _inventory_payload(self, relative: str, source_type: str, columns: list[str], rows: list[dict[str, Any]], encoding: str, notes: list[str]) -> dict[str, Any]:
        lower = {column.casefold(): column for column in columns}
        primary = next((lower[key] for key in ("id", "numero_os", "record_id", "cd_identificador") if key in lower), None)
        geometry = next((lower[key] for key in ("path", "geometry_wkt", "geometry", "ponto geometria", "candidate_geometry_wkt") if key in lower), None)
        street = next((lower[key] for key in ("logradouro_geosampa", "via", "rua_norm", "rua_raw", "rua", "nm_logradouro") if key in lower), None)
        number = next((lower[key] for key in ("numero", "cd_numero_inicial_par") if key in lower), None)
        from_field = next((lower[key] for key in ("de", "de_original", "from_field") if key in lower), None)
        to_field = next((lower[key] for key in ("ate", "até", "ate_original", "to_field") if key in lower), None)
        surface = next((lower[key] for key in ("revestimento", "surface", "surface_type") if key in lower), None)
        resurfacing_date = next((lower[key] for key in ("data_termino", "data término", "data_termino_recape", "execution_date") if key in lower), None)
        status = next((lower[key] for key in ("status", "status_recape", "status_path") if key in lower), None)
        regional = next((lower[key] for key in ("prefeitura_regional", "regional") if key in lower), None)
        subprefecture = next((lower[key] for key in ("subprefeitura", "subprefecture") if key in lower), None)
        coordinate_fields = [lower[key] for key in ("latitude", "longitude", "lat", "lon") if key in lower]
        date_ranges: dict[str, Any] = {}
        surface_values: dict[str, int] = {}
        null_rates: dict[str, float] = {}
        for column in [primary, geometry, street, number, from_field, to_field, surface, resurfacing_date, status, regional, subprefecture, *coordinate_fields]:
            if not column:
                continue
            values = [self._text(row.get(column)) for row in rows]
            null_rates[column] = round(sum(not value for value in values) / len(values), 6) if values else 0.0
            if column == surface:
                surface_values = dict(Counter(value for value in values if value))
            if column == resurfacing_date:
                parsed = [self._parse_date(value) for value in values if value]
                parsed = [value for value in parsed if value]
                if parsed:
                    date_ranges[column] = {"min": min(parsed).isoformat(), "max": max(parsed).isoformat(), "valid_count": len(parsed)}
        duplicates = None
        if primary:
            values = [self._text(row.get(primary)) for row in rows]
            nonempty = [value for value in values if value]
            duplicates = len(nonempty) - len(set(nonempty))
        return {
            "file": relative,
            "source": relative,
            "row_count": len(rows),
            "primary_key": primary,
            "geometry_field": geometry,
            "street_field": street,
            "number_field": number,
            "from_field": from_field,
            "to_field": to_field,
            "surface_field": surface,
            "resurfacing_date_field": resurfacing_date,
            "status_field": status,
            "regional_field": regional,
            "subprefecture_field": subprefecture,
            "coordinate_fields": coordinate_fields,
            "source_type": source_type,
            "encoding": encoding,
            "crs": "EPSG:4326 for latitude/longitude or path pairs" if coordinate_fields or relative.endswith("recape_clean.csv") else None,
            "columns": columns,
            "null_rate": null_rates,
            "duplicate_primary_key_rows": duplicates,
            "date_ranges": date_ranges,
            "surface_values": surface_values,
            "notes": notes,
        }

    def _protection_distribution(self, records: Iterable[RecapeRecord], reference_date: date) -> dict[str, int]:
        counts = Counter()
        for record in records:
            if record.resurfacing_date is None:
                counts["UNKNOWN_DATE"] += 1
                continue
            end = self._add_year(record.resurfacing_date)
            remaining = (end - reference_date).days
            if reference_date >= end:
                counts["EXPIRED"] += 1
            elif remaining <= 30:
                counts["EXPIRING_SOON"] += 1
            else:
                counts["ACTIVE"] += 1
        return dict(counts)

    @staticmethod
    def _add_year(value: date) -> date:
        try:
            return value.replace(year=value.year + 1)
        except ValueError:
            return value.replace(year=value.year + 1, month=2, day=28)

    @staticmethod
    def _path_geometry(value: Any):
        if not value:
            return None
        try:
            coordinates = json.loads(str(value))
            if not isinstance(coordinates, list) or len(coordinates) < 2:
                return None
            line = LineString([(float(item[0]), float(item[1])) for item in coordinates if isinstance(item, (list, tuple)) and len(item) >= 2])
            return transform(WGS84_TO_METRIC.transform, line) if len(line.coords) >= 2 else None
        except (TypeError, ValueError, json.JSONDecodeError):
            return None

    @staticmethod
    def _parse_date(value: Any) -> date | None:
        text = OperationalRepository._text(value)
        if not text:
            return None
        parsed = pd.to_datetime(text, errors="coerce", format="mixed", dayfirst=True)
        return None if pd.isna(parsed) else parsed.date()

    @staticmethod
    def _integer(value: Any) -> int | None:
        try:
            number = float(str(value).replace(",", "."))
            return int(number) if math.isfinite(number) else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _text(value: Any) -> str:
        if value is None:
            return ""
        try:
            if bool(pd.isna(value)):
                return ""
        except (TypeError, ValueError):
            pass
        rendered = str(value).strip()
        return "" if rendered.casefold() in {"", "nan", "none", "null", "<na>"} else rendered

    @staticmethod
    def _normalize(value: Any) -> str:
        try:
            return normalizar_rua(OperationalRepository._text(value))
        except Exception:
            return OperationalRepository._text(value).upper()
