"""Pure-ish operational services over :mod:`repository` indexes."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Iterable

from .models import (
    DataQuality,
    GeometryInfo,
    InterventionEvent,
    OperationalLocationResult,
    ProtectionResult,
    ProvenanceValue,
    ResurfacingLookupResult,
    ResurfacingRecord,
    StreetLookupCandidate,
    StreetLookupQuery,
    StreetLookupResult,
    SurfaceLookupResult,
    TemporalRelationshipResult,
)
from .repository import OperationalRepository, RecapeRecord, SegmentRecord


def _parse_date(value: date | datetime | str | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _add_year(value: date) -> date:
    try:
        return value.replace(year=value.year + 1)
    except ValueError:
        # Explicit policy for 29 February: the anniversary is 28 February.
        return value.replace(year=value.year + 1, month=2, day=28)


def calculate_protection_status(
    resurfacing_date: date | datetime | str | None,
    reference_date: date | datetime | str,
    *,
    expiring_soon_days: int = 30,
    date_type: str | None = None,
    date_source: str | None = None,
) -> ProtectionResult:
    """Calculate a one-calendar-year protection window.

    The start is inclusive and the end anniversary is exclusive: on the exact
    end date the window is ``EXPIRED`` with zero days remaining.  No current
    clock is consulted; callers must provide ``reference_date``.
    """
    start = _parse_date(resurfacing_date)
    reference = _parse_date(reference_date)
    if reference is None:
        raise ValueError("reference_date must be explicit and parseable")
    if start is None:
        return ProtectionResult(
            status="UNKNOWN_DATE",
            start_date=None,
            end_date=None,
            days_remaining=None,
            date_type=date_type,
            date_source=date_source,
            warnings=("RESURFACING_DATE_UNAVAILABLE",),
        )
    end = _add_year(start)
    remaining = (end - reference).days
    if reference >= end:
        status = "EXPIRED"
        days_remaining = 0
    elif remaining <= expiring_soon_days:
        status = "EXPIRING_SOON"
        days_remaining = remaining
    else:
        status = "ACTIVE"
        days_remaining = remaining
    return ProtectionResult(status, start, end, days_remaining, date_type, date_source)


def classify_temporal_relationship(
    event: InterventionEvent,
    resurfacing_date: date | datetime | str | None,
) -> TemporalRelationshipResult:
    """Classify only when an explicit intervention execution date exists."""
    start = _parse_date(resurfacing_date)
    execution = _parse_date(event.execution_date)
    warnings: list[str] = []
    if execution is None:
        warnings.append("EXECUTION_DATE_UNAVAILABLE")
        warnings.append("NOTIFICATION_DATE_NOT_USED_AS_EXECUTION_DATE")
        return TemporalRelationshipResult("TEMPORAL_RELATIONSHIP_UNKNOWN", None, event.emergency, tuple(warnings))
    if start is None:
        warnings.append("RESURFACING_DATE_UNAVAILABLE")
        return TemporalRelationshipResult("TEMPORAL_RELATIONSHIP_UNKNOWN", execution, event.emergency, tuple(warnings))
    end = _add_year(start)
    if execution < start:
        relationship = "BEFORE_RESURFACING"
    elif execution == start:
        relationship = "DURING_RESURFACING"
    elif execution < end:
        relationship = "DURING_PROTECTION_WINDOW"
    else:
        relationship = "AFTER_PROTECTION_WINDOW"
    if event.emergency is None:
        warnings.append("EMERGENCY_FLAG_UNKNOWN")
    elif event.emergency:
        warnings.append("EMERGENCY_EVENT_REQUIRES_SEPARATE_POLICY")
    return TemporalRelationshipResult(relationship, execution, event.emergency, tuple(warnings))


class StreetLookupService:
    def __init__(self, repository: OperationalRepository, *, coordinate_max_distance_m: float = 50.0, coordinate_high_distance_m: float = 10.0):
        self.repository = repository
        self.coordinate_max_distance_m = coordinate_max_distance_m
        self.coordinate_high_distance_m = coordinate_high_distance_m

    def lookup(self, query: StreetLookupQuery | None = None, **kwargs: Any) -> StreetLookupResult:
        query = query or StreetLookupQuery(**kwargs)
        if query.record_id:
            result = self._lookup_record(query)
            if result.confidence != "NOT_FOUND":
                return result
        if query.street:
            result = self._lookup_street(query)
            if result.confidence != "NOT_FOUND" or not (query.latitude is not None and query.longitude is not None):
                return result
        if query.latitude is not None and query.longitude is not None:
            return self._lookup_coordinate(query)
        return StreetLookupResult(query=query, confidence="UNSUPPORTED", match_method="NO_QUERY", warnings=["PROVIDE_STREET_NUMBER_COORDINATES_OR_RECORD_ID"])

    def lookup_many(self, queries: Iterable[StreetLookupQuery]) -> list[StreetLookupResult]:
        return [self.lookup(query) for query in queries]

    def _lookup_record(self, query: StreetLookupQuery) -> StreetLookupResult:
        record = self.repository.recape_by_id.get(str(query.record_id))
        if record is None:
            return StreetLookupResult(query=query, confidence="NOT_FOUND", match_method="RECORD_ID_NOT_FOUND", warnings=["RECORD_ID_NOT_FOUND"])
        provenance = [ProvenanceValue(record.record_id, record.source_file, record.record_id, "RECORD_ID", "EXACT")]
        warnings = ["REGIONAL_UNAVAILABLE"]
        if record.geometry is None:
            warnings.append("OFFICIAL_GEOMETRY_UNAVAILABLE")
        return StreetLookupResult(
            query=query,
            normalized_street=next(iter(record.normalized_streets), None),
            matched_street=record.street or None,
            number=None,
            segment_id=None,
            geometry_wkt=record.geometry_wkt,
            subprefecture=record.subprefecture,
            match_method="RECORD_ID",
            confidence="EXACT",
            number_capability="UNSUPPORTED",
            candidate_count=1,
            alternatives=[],
            warnings=warnings,
            provenance=provenance,
        )

    def _lookup_street(self, query: StreetLookupQuery) -> StreetLookupResult:
        normalized = self.repository._normalize(query.street)
        segments = self.repository.segments_by_street.get(normalized, [])
        number = self._parse_number(query.number)
        range_available = any(any(value is not None for value in (segment.number_initial_even, segment.number_final_even, segment.number_initial_odd, segment.number_final_odd)) for segment in segments)
        capability = "NUMBER_RANGE" if range_available else "STREET_ONLY"
        if not segments:
            return StreetLookupResult(query=query, normalized_street=normalized or None, number=query.number, confidence="NOT_FOUND", match_method="STREET_NOT_FOUND", number_capability="UNSUPPORTED", warnings=["STREET_NOT_FOUND"])
        if query.number is not None and number is None:
            return StreetLookupResult(query=query, normalized_street=normalized, number=query.number, matched_street=segments[0].street, confidence="UNSUPPORTED", match_method="INVALID_NUMBER", number_capability=capability, candidate_count=0, warnings=["NUMBER_NOT_NUMERIC"])
        candidates = [segment for segment in segments if number is None or segment.matches_number(number)]
        if number is not None and not candidates:
            return StreetLookupResult(query=query, normalized_street=normalized, matched_street=segments[0].street, number=number, confidence="NOT_FOUND", match_method="NUMBER_OUTSIDE_KNOWN_RANGE", number_capability=capability, candidate_count=0, alternatives=[segment.to_candidate(number=number) for segment in segments[:20]], warnings=["NUMBER_OUTSIDE_KNOWN_RANGE", "EXACT_ADDRESS_POINTS_UNAVAILABLE"])
        candidates = sorted(candidates, key=lambda segment: segment.segment_id)
        if len(candidates) == 1:
            segment = candidates[0]
            method = "STREET_AND_NUMBER_RANGE" if number is not None else "STREET_EXACT"
            confidence = "EXACT" if number is not None else "HIGH"
            return self._segment_result(query, segment, method, confidence, capability, number=number, candidate_count=1)
        return StreetLookupResult(
            query=query,
            normalized_street=normalized,
            matched_street=candidates[0].street,
            number=number,
            match_method="STREET_AND_NUMBER_AMBIGUOUS" if number is not None else "STREET_AMBIGUOUS",
            confidence="AMBIGUOUS",
            number_capability=capability,
            candidate_count=len(candidates),
            alternatives=[segment.to_candidate(number=number) for segment in candidates[:50]],
            warnings=["MULTIPLE_COMPATIBLE_SEGMENTS", "DO_NOT_SELECT_SILENTLY"],
            provenance=[ProvenanceValue(normalized, "data/cache/geosampa_segmento_logradouro.geojson", None, "STREET_EXACT", "AMBIGUOUS")],
        )

    def _lookup_coordinate(self, query: StreetLookupQuery) -> StreetLookupResult:
        pairs = self.repository.spatial_segments(float(query.latitude), float(query.longitude), max_distance_m=self.coordinate_max_distance_m)
        if not pairs:
            return StreetLookupResult(query=query, confidence="NOT_FOUND", match_method="COORDINATE_TOO_FAR", number=query.number, latitude=query.latitude, longitude=query.longitude, number_capability="UNSUPPORTED", warnings=["COORDINATE_TOO_FAR_OR_NO_SEGMENT"])
        if len(pairs) > 1:
            alternatives = [segment.to_candidate(distance_m=distance) for segment, distance in pairs]
            return StreetLookupResult(query=query, confidence="AMBIGUOUS", match_method="COORDINATE_MULTIPLE_NEAREST", number=query.number, latitude=query.latitude, longitude=query.longitude, number_capability="UNSUPPORTED", candidate_count=len(pairs), alternatives=alternatives, distance_to_segment_m=round(pairs[0][1], 6), warnings=["MULTIPLE_NEAREST_SEGMENTS", "DO_NOT_SELECT_SILENTLY"], provenance=[ProvenanceValue(query.latitude, "data/cache/geosampa_segmento_logradouro.geojson", None, "COORDINATE_NEAREST", "AMBIGUOUS")])
        segment, distance = pairs[0]
        confidence = "HIGH" if distance <= self.coordinate_high_distance_m else "MEDIUM"
        return self._segment_result(query, segment, "COORDINATE_NEAREST", confidence, "UNSUPPORTED", distance=distance)

    def _segment_result(self, query: StreetLookupQuery, segment: SegmentRecord, method: str, confidence: str, capability: str, *, number: int | None = None, distance: float | None = None, candidate_count: int = 1) -> StreetLookupResult:
        warnings = ["REGIONAL_UNAVAILABLE", "SUBPREFECTURE_UNAVAILABLE"]
        if method == "STREET_EXACT":
            warnings.append("NUMBER_NOT_PROVIDED")
        return StreetLookupResult(
            query=query,
            normalized_street=segment.normalized_street,
            matched_street=segment.street,
            number=number if number is not None else query.number,
            latitude=query.latitude,
            longitude=query.longitude,
            segment_id=segment.segment_id,
            codlog=segment.codlog,
            geometry_wkt=segment.geometry.wkt,
            match_method=method,
            confidence=confidence,
            number_capability=capability,
            candidate_count=candidate_count,
            alternatives=[],
            distance_to_segment_m=round(distance, 6) if distance is not None else None,
            warnings=warnings,
            provenance=[ProvenanceValue(segment.segment_id, segment.source, segment.segment_id, method, confidence)],
        )

    @staticmethod
    def _parse_number(value: Any) -> int | None:
        if value is None or str(value).strip() == "":
            return None
        try:
            number = float(str(value).replace(",", "."))
            return int(number) if number.is_integer() else None
        except (TypeError, ValueError):
            return None


class SurfaceLookupService:
    def __init__(self, repository: OperationalRepository):
        self.repository = repository

    def lookup(self, location: StreetLookupResult | None = None, *, record_id: str | None = None, latitude: float | None = None, longitude: float | None = None, segment_id: str | None = None) -> SurfaceLookupResult:
        record_id = record_id or (location.query.record_id if location else None)
        if not record_id and location and location.segment_id and location.segment_id.startswith("recape:"):
            record_id = location.segment_id.split(":", 1)[1]
        if record_id:
            record = self.repository.recape_by_id.get(str(record_id))
            if record is not None:
                return self._record_result(record, "RECORD_ID", "EXACT")
        if latitude is None and longitude is None and location:
            latitude, longitude = location.latitude, location.longitude
        if latitude is not None and longitude is not None:
            pairs = self.repository.spatial_recapes(latitude, longitude)
            if not pairs:
                return SurfaceLookupResult(warnings=["NO_OFFICIAL_RECAPE_PATH_NEAR_COORDINATE"], method="COORDINATE_NO_RECAPE_PATH")
            closest = pairs[0][1]
            nearest = [(record, distance) for record, distance in pairs if distance <= closest + 1.0]
            if len(nearest) > 1:
                return SurfaceLookupResult(status="AMBIGUOUS", confidence="AMBIGUOUS", method="COORDINATE_MULTIPLE_RECAPES", warnings=["MULTIPLE_RECAPES_NEAR_COORDINATE"], candidates=[{"record_id": record.record_id, "distance_to_segment_m": round(distance, 6), "raw_surface_type": record.raw_surface_type} for record, distance in nearest])
            record, distance = nearest[0]
            result = self._record_result(record, "COORDINATE_OFFICIAL_PATH", "HIGH" if distance <= 10.0 else "MEDIUM")
            result.warnings.append(f"DISTANCE_TO_RECAP_PATH_M={distance:.3f}")
            return result
        return SurfaceLookupResult(warnings=["SURFACE_IS_NOT_SEGMENT_KEYED", "DATA_UNAVAILABLE_WITHOUT_RECORD_OR_COORDINATE"], method="NO_SEGMENT_LEVEL_SURFACE_JOIN")

    def _record_result(self, record: RecapeRecord, method: str, confidence: str) -> SurfaceLookupResult:
        provenance = [ProvenanceValue(record.raw_surface_type, record.source_file, record.record_id, method, confidence)]
        if not record.raw_surface_type:
            return SurfaceLookupResult(status="DATA_UNAVAILABLE", segment_id=None, source=record.source_file, source_record_id=record.record_id, confidence="NOT_FOUND", method=method, warnings=["SURFACE_FIELD_EMPTY"], provenance=provenance)
        return SurfaceLookupResult(surface_type=record.raw_surface_type, raw_surface_type=record.raw_surface_type, status="FOUND", segment_id=None, source=record.source_file, source_record_id=record.record_id, confidence=confidence, method=method, provenance=provenance)


class ResurfacingLookupService:
    def __init__(self, repository: OperationalRepository):
        self.repository = repository

    def lookup(self, location: StreetLookupResult | None = None, *, record_id: str | None = None, street: str | None = None, latitude: float | None = None, longitude: float | None = None) -> ResurfacingLookupResult:
        record_id = record_id or (location.query.record_id if location else None)
        if not record_id and location and location.segment_id and location.segment_id.startswith("recape:"):
            record_id = location.segment_id.split(":", 1)[1]
        if record_id:
            record = self.repository.recape_by_id.get(str(record_id))
            if record is None:
                return ResurfacingLookupResult(warnings=["RECORD_ID_NOT_FOUND"])
            return self._result([record], "RECORD_ID", "EXACT")
        if latitude is None and longitude is None and location:
            latitude, longitude = location.latitude, location.longitude
        if latitude is not None and longitude is not None:
            pairs = self.repository.spatial_recapes(latitude, longitude)
            if not pairs:
                return ResurfacingLookupResult(warnings=["NO_OFFICIAL_RECAPE_PATH_NEAR_COORDINATE"], match_method="COORDINATE_NO_RECAPE_PATH")
            closest = pairs[0][1]
            nearest = [record for record, distance in pairs if distance <= closest + 1.0]
            return self._result(nearest, "COORDINATE_OFFICIAL_PATH", "HIGH" if len(nearest) == 1 else "AMBIGUOUS")
        street_value = street or (location.normalized_street if location else None)
        normalized = self.repository._normalize(street_value)
        records = self.repository.recapes_by_street.get(normalized, []) if normalized else []
        if not records:
            return ResurfacingLookupResult(warnings=["NO_RESURFACING_FOR_STREET"], match_method="STREET_NOT_FOUND")
        return self._result(records, "STREET_EXACT", "MEDIUM" if len(records) > 1 else "HIGH")

    def _result(self, records: list[RecapeRecord], method: str, confidence: str) -> ResurfacingLookupResult:
        history = sorted((self._record(record, method, confidence) for record in records), key=lambda item: (item.resurfacing_date is not None, item.resurfacing_date or date.min, item.resurfacing_id), reverse=True)
        dated = [item for item in history if item.resurfacing_date is not None]
        latest = dated[0] if dated else None
        warnings = []
        if not dated:
            warnings.append("NO_USABLE_COMPLETION_DATE")
        if len(records) > 1:
            warnings.append("MULTIPLE_RESURFACING_HISTORY_RECORDS")
        return ResurfacingLookupResult(status="FOUND", history=history, latest_resurfacing=latest, match_method=method, match_confidence=confidence, warnings=warnings, provenance=[item.provenance[0] for item in history if item.provenance])

    @staticmethod
    def _record(record: RecapeRecord, method: str, confidence: str) -> ResurfacingRecord:
        warnings = ("DATE_FIELD_IS_COMPLETION_DATE_NOT_EXECUTION_EVENT",) if record.resurfacing_date else ("RESURFACING_DATE_UNAVAILABLE",)
        provenance = (ProvenanceValue(record.resurfacing_date, record.source_file, record.record_id, method, record.date_type and "MEDIUM" or "UNKNOWN", warnings),)
        return ResurfacingRecord(record.record_id, record.street or None, None, record.geometry_wkt, record.status or None, record.resurfacing_date, record.date_type, "data/processed/recape_clean.csv:data_termino" if record.resurfacing_date else None, "MEDIUM" if record.resurfacing_date else "UNKNOWN", None, record.subprefecture, method, confidence, record.source_file, record.raw_surface_type, provenance)


class ResurfacingProtectionService:
    def __init__(self, *, expiring_soon_days: int = 30):
        self.expiring_soon_days = expiring_soon_days

    def calculate(self, resurfacing: ResurfacingRecord | None, reference_date: date | datetime | str) -> ProtectionResult:
        if resurfacing is None:
            return calculate_protection_status(None, reference_date, expiring_soon_days=self.expiring_soon_days)
        return calculate_protection_status(resurfacing.resurfacing_date, reference_date, expiring_soon_days=self.expiring_soon_days, date_type=resurfacing.resurfacing_date_type, date_source=resurfacing.resurfacing_date_source)


class OperationalQueryService:
    def __init__(self, repository: OperationalRepository | None = None, *, expiring_soon_days: int = 30):
        self.repository = repository or OperationalRepository()
        self.street = StreetLookupService(self.repository)
        self.surface = SurfaceLookupService(self.repository)
        self.resurfacing = ResurfacingLookupService(self.repository)
        self.protection = ResurfacingProtectionService(expiring_soon_days=expiring_soon_days)

    def lookup(self, query: StreetLookupQuery, *, reference_date: date | datetime | str) -> OperationalLocationResult:
        location = self.street.lookup(query)
        surface = self.surface.lookup(location)
        resurfacing = self.resurfacing.lookup(location)
        protection = self.protection.calculate(resurfacing.latest_resurfacing, reference_date)
        geometry = self._geometry(location)
        administrative = {
            "regional": location.regional,
            "subprefecture": location.subprefecture or (resurfacing.latest_resurfacing.subprefecture if resurfacing.latest_resurfacing else None),
        }
        warnings = list(dict.fromkeys(location.warnings + surface.warnings + resurfacing.warnings + list(protection.warnings) + list(geometry.warnings)))
        if location.confidence == "AMBIGUOUS":
            quality = DataQuality("LIMITED", "street lookup returned multiple compatible candidates")
        elif location.confidence in {"NOT_FOUND", "UNSUPPORTED"}:
            quality = DataQuality("UNAVAILABLE", "location could not be resolved")
        elif surface.status == "DATA_UNAVAILABLE" or resurfacing.status == "NOT_FOUND" or protection.status == "UNKNOWN_DATE":
            quality = DataQuality("PARTIAL", "location resolved but one or more operational domains lack data")
        else:
            quality = DataQuality("COMPLETE", "location, surface, resurfacing and protection data available")
        return OperationalLocationResult(location, surface, resurfacing, protection, geometry, administrative, quality, warnings)

    def lookup_many(self, queries: Iterable[StreetLookupQuery], *, reference_date: date | datetime | str) -> list[OperationalLocationResult]:
        return [self.lookup(query, reference_date=reference_date) for query in queries]

    def _geometry(self, location: StreetLookupResult) -> GeometryInfo:
        official_wkt = location.geometry_wkt
        official_source = "data/cache/geosampa_segmento_logradouro.geojson" if location.segment_id and not location.segment_id.startswith("recape:") else ("data/processed/recape_clean.csv:path" if official_wkt else None)
        status = "OFFICIAL" if official_wkt else "UNRESOLVED"
        shadow_wkt = None
        shadow_source = None
        consensus_class = None
        validation_class = None
        record_id = location.query.record_id
        if record_id:
            quality = self.repository.shadow_quality_by_id.get(str(record_id)) or {}
            validator = self.repository.validator_by_id.get(str(record_id)) or {}
            consensus = self.repository.consensus_by_id.get(str(record_id)) or {}
            shadow_wkt = str(quality.get("geometry_wkt") or validator.get("geometry_wkt") or "") or None
            shadow_source = "data/processed/route_geometry_quality_shadow.csv" if quality.get("geometry_wkt") else ("data/processed/geometry_validation_shadow.csv" if validator.get("geometry_wkt") else None)
            consensus_class = str(consensus.get("consensus_class") or "") or None
            validation_class = str(validator.get("validation_class") or "") or None
        warnings = []
        if shadow_wkt and not official_wkt:
            warnings.append("SHADOW_GEOMETRY_NOT_OFFICIAL")
        return GeometryInfo(status, official_wkt, official_source, shadow_wkt, shadow_source, consensus_class, validation_class, tuple(warnings))
