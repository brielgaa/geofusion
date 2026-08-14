"""Small, serializable contracts for the operational layer."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import date, datetime
from typing import Any


def serialize(value: Any) -> Any:
    if is_dataclass(value):
        return {key: serialize(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): serialize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [serialize(item) for item in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


@dataclass(frozen=True)
class ProvenanceValue:
    value: Any
    source: str
    source_record_id: str | None
    method: str
    confidence: str
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return serialize(self)


@dataclass(frozen=True)
class StreetLookupQuery:
    street: str | None = None
    number: int | str | None = None
    latitude: float | None = None
    longitude: float | None = None
    record_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return serialize(self)


@dataclass(frozen=True)
class StreetLookupCandidate:
    segment_id: str
    street: str
    normalized_street: str
    codlog: str | None
    geometry_wkt: str | None
    number_match: str
    number_range: dict[str, int | None] = field(default_factory=dict)
    distance_to_segment_m: float | None = None
    source: str = "geosampa_segmento_logradouro.geojson"

    def to_dict(self) -> dict[str, Any]:
        return serialize(self)


@dataclass
class StreetLookupResult:
    query: StreetLookupQuery
    normalized_street: str | None = None
    matched_street: str | None = None
    number: int | str | None = None
    latitude: float | None = None
    longitude: float | None = None
    segment_id: str | None = None
    codlog: str | None = None
    geometry_wkt: str | None = None
    regional: str | None = None
    subprefecture: str | None = None
    match_method: str = "NOT_FOUND"
    confidence: str = "NOT_FOUND"
    number_capability: str = "UNSUPPORTED"
    candidate_count: int = 0
    alternatives: list[StreetLookupCandidate] = field(default_factory=list)
    distance_to_segment_m: float | None = None
    warnings: list[str] = field(default_factory=list)
    provenance: list[ProvenanceValue] = field(default_factory=list)

    @property
    def found(self) -> bool:
        return self.segment_id is not None

    def to_dict(self) -> dict[str, Any]:
        return serialize(self)


@dataclass
class SurfaceLookupResult:
    surface_type: str | None = None
    raw_surface_type: str | None = None
    status: str = "DATA_UNAVAILABLE"
    segment_id: str | None = None
    source: str | None = None
    source_record_id: str | None = None
    confidence: str = "NOT_FOUND"
    method: str = "NO_SURFACE_DATA"
    warnings: list[str] = field(default_factory=list)
    provenance: list[ProvenanceValue] = field(default_factory=list)
    candidates: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return serialize(self)


@dataclass(frozen=True)
class ResurfacingRecord:
    resurfacing_id: str
    street: str | None
    segment: str | None
    geometry_wkt: str | None
    status: str | None
    resurfacing_date: date | None
    resurfacing_date_type: str | None
    resurfacing_date_source: str | None
    resurfacing_date_confidence: str
    regional: str | None
    subprefecture: str | None
    match_method: str
    match_confidence: str
    source: str
    raw_surface_type: str | None = None
    provenance: tuple[ProvenanceValue, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return serialize(self)


@dataclass
class ResurfacingLookupResult:
    status: str = "NOT_FOUND"
    history: list[ResurfacingRecord] = field(default_factory=list)
    latest_resurfacing: ResurfacingRecord | None = None
    match_method: str = "NO_RESURFACING"
    match_confidence: str = "NOT_FOUND"
    warnings: list[str] = field(default_factory=list)
    provenance: list[ProvenanceValue] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return serialize(self)


@dataclass(frozen=True)
class ProtectionResult:
    status: str
    start_date: date | None
    end_date: date | None
    days_remaining: int | None
    date_type: str | None = None
    date_source: str | None = None
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return serialize(self)


@dataclass(frozen=True)
class InterventionEvent:
    record_id: str
    geometry_wkt: str | None = None
    execution_date: date | None = None
    execution_date_source: str | None = None
    execution_date_confidence: str = "UNKNOWN"
    intervention_type: str | None = None
    emergency: bool | None = None


@dataclass(frozen=True)
class TemporalRelationshipResult:
    relationship: str
    execution_date: date | None
    emergency: bool | None
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return serialize(self)


@dataclass(frozen=True)
class GeometryInfo:
    status: str
    official_wkt: str | None = None
    official_source: str | None = None
    shadow_wkt: str | None = None
    shadow_source: str | None = None
    consensus_class: str | None = None
    validation_class: str | None = None
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return serialize(self)


@dataclass(frozen=True)
class DataQuality:
    status: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return serialize(self)


@dataclass
class OperationalLocationResult:
    location: StreetLookupResult
    surface: SurfaceLookupResult
    resurfacing: ResurfacingLookupResult
    protection: ProtectionResult
    geometry: GeometryInfo
    administrative_area: dict[str, Any]
    data_quality: DataQuality
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return serialize(self)
