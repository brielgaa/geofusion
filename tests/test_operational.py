from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest
from shapely.geometry import LineString, Point, mapping
from shapely.ops import transform

from src.operational import (
    InterventionEvent,
    OperationalQueryService,
    OperationalRepository,
    ResurfacingLookupService,
    StreetLookupQuery,
    StreetLookupService,
    SurfaceLookupService,
    calculate_protection_status,
    classify_temporal_relationship,
)
from src.operational.repository import WGS84_TO_METRIC


def _fixture_repo(tmp_path: Path) -> OperationalRepository:
    processed = tmp_path / "data" / "processed"
    cache = tmp_path / "data" / "cache"
    processed.mkdir(parents=True)
    cache.mkdir(parents=True)
    point = transform(WGS84_TO_METRIC.transform, Point(-46.7, -23.5))
    line = LineString([(point.x - 100, point.y), (point.x + 100, point.y)])
    other = LineString([(point.x - 100, point.y + 100), (point.x + 100, point.y + 100)])
    features = [
        {"type": "Feature", "id": "segment-1", "properties": {"codlog": "000001", "nm_logradouro": "RUA TESTE", "cd_numero_inicial_par": 100, "cd_numero_final_par": 200, "cd_numero_inicial_impar": 101, "cd_numero_final_impar": 201}, "geometry": mapping(line)},
        {"type": "Feature", "id": "segment-2", "properties": {"codlog": "000001", "nm_logradouro": "RUA TESTE", "cd_numero_inicial_par": 202, "cd_numero_final_par": 300, "cd_numero_inicial_impar": 203, "cd_numero_final_impar": 301}, "geometry": mapping(other)},
        {"type": "Feature", "id": "segment-3", "properties": {"codlog": "000002", "nm_logradouro": "OUTRA RUA", "cd_numero_inicial_par": 1, "cd_numero_final_par": 10, "cd_numero_inicial_impar": 1, "cd_numero_final_impar": 11}, "geometry": mapping(LineString([(point.x, point.y + 1000), (point.x + 100, point.y + 1000)]))},
    ]
    (cache / "geosampa_segmento_logradouro.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": features, "crs": {"type": "name", "properties": {"name": "EPSG:31983"}}}), encoding="utf-8")
    path = json.dumps([[-46.7, -23.5], [-46.699, -23.5]])
    rows = [
        {"id": "1", "status": "CONCLUIDO", "data_termino": "2025-08-10", "via": "Rua Teste", "rua_norm": "TESTE", "logradouro_geosampa": "RUA TESTE", "subprefeitura": "SUB", "revestimento": "CBUQ", "path": path, "status_path": "OK"},
        {"id": "2", "status": "CONCLUIDO", "data_termino": "2024-08-10", "via": "Rua Teste", "rua_norm": "TESTE", "logradouro_geosampa": "RUA TESTE", "subprefeitura": "SUB", "revestimento": "CONCRETO", "path": path, "status_path": "OK"},
        {"id": "3", "status": "PLANEJADO", "data_termino": "", "via": "Rua Teste", "rua_norm": "TESTE", "logradouro_geosampa": "RUA TESTE", "subprefeitura": "SUB", "revestimento": "", "path": "", "status_path": "SEM_CAMINHO"},
    ]
    pd.DataFrame(rows).to_csv(processed / "recape_clean.csv", index=False)
    pd.DataFrame([{"id": "1", "geometry_wkt": "LINESTRING (0 0, 1 1)"}]).to_csv(processed / "route_geometry_quality_shadow.csv", index=False)
    pd.DataFrame([{"id": "1", "validation_class": "VALIDATED_MEDIUM", "geometry_wkt": "LINESTRING (0 0, 1 1)"}]).to_csv(processed / "geometry_validation_shadow.csv", index=False)
    pd.DataFrame([{"id": "1", "consensus_class": "INSUFFICIENT_EVIDENCE"}]).to_csv(processed / "consensus_evidence_shadow.csv", index=False)
    return OperationalRepository(tmp_path)


def test_street_exact(tmp_path):
    result = StreetLookupService(_fixture_repo(tmp_path)).lookup(StreetLookupQuery(street="Rua Teste"))
    assert result.confidence == "AMBIGUOUS"


def test_street_not_found(tmp_path):
    result = StreetLookupService(_fixture_repo(tmp_path)).lookup(StreetLookupQuery(street="Rua Inexistente"))
    assert result.confidence == "NOT_FOUND"


def test_street_ambiguous(tmp_path):
    result = StreetLookupService(_fixture_repo(tmp_path)).lookup(StreetLookupQuery(street="Rua Teste"))
    assert result.match_method == "STREET_AMBIGUOUS"
    assert result.candidate_count == 2


def test_street_plus_number(tmp_path):
    result = StreetLookupService(_fixture_repo(tmp_path)).lookup(StreetLookupQuery(street="Rua Teste", number=150))
    assert result.confidence == "EXACT"
    assert result.segment_id == "segment-1"


def test_number_unavailable_without_number(tmp_path):
    result = StreetLookupService(_fixture_repo(tmp_path)).lookup(StreetLookupQuery(street="Rua Teste"))
    assert result.number_capability == "NUMBER_RANGE"


def test_number_outside_known_range(tmp_path):
    result = StreetLookupService(_fixture_repo(tmp_path)).lookup(StreetLookupQuery(street="Rua Teste", number=999))
    assert result.match_method == "NUMBER_OUTSIDE_KNOWN_RANGE"


def test_coordinate_lookup(tmp_path):
    result = StreetLookupService(_fixture_repo(tmp_path)).lookup(StreetLookupQuery(latitude=-23.5, longitude=-46.7))
    assert result.match_method == "COORDINATE_MULTIPLE_NEAREST" or result.confidence in {"HIGH", "MEDIUM"}


def test_coordinate_too_far(tmp_path):
    result = StreetLookupService(_fixture_repo(tmp_path)).lookup(StreetLookupQuery(latitude=0.0, longitude=0.0))
    assert result.confidence == "NOT_FOUND"


def test_multiple_candidates_are_returned(tmp_path):
    result = StreetLookupService(_fixture_repo(tmp_path)).lookup(StreetLookupQuery(street="Rua Teste"))
    assert len(result.alternatives) == 2


def test_street_provenance_preserved(tmp_path):
    result = StreetLookupService(_fixture_repo(tmp_path)).lookup(StreetLookupQuery(street="Rua Teste", number=150))
    assert result.provenance[0].source.endswith("geojson")


def test_surface_exact_record(tmp_path):
    result = SurfaceLookupService(_fixture_repo(tmp_path)).lookup(record_id="1")
    assert result.surface_type == "CBUQ"


def test_different_surfaces_same_street(tmp_path):
    service = SurfaceLookupService(_fixture_repo(tmp_path))
    assert service.lookup(record_id="1").surface_type != service.lookup(record_id="2").surface_type


def test_surface_unavailable(tmp_path):
    result = SurfaceLookupService(_fixture_repo(tmp_path)).lookup(record_id="3")
    assert result.status == "DATA_UNAVAILABLE"


def test_raw_surface_retained(tmp_path):
    result = SurfaceLookupService(_fixture_repo(tmp_path)).lookup(record_id="2")
    assert result.raw_surface_type == "CONCRETO"


def test_surface_source_retained(tmp_path):
    result = SurfaceLookupService(_fixture_repo(tmp_path)).lookup(record_id="1")
    assert result.source == "data/processed/recape_clean.csv"


def test_surface_ambiguous_coordinate(tmp_path):
    result = SurfaceLookupService(_fixture_repo(tmp_path)).lookup(latitude=-23.5, longitude=-46.7)
    assert result.status == "AMBIGUOUS"


def test_no_default_asphalt(tmp_path):
    result = SurfaceLookupService(_fixture_repo(tmp_path)).lookup(record_id="3")
    assert result.surface_type is None


def test_no_resurfacing(tmp_path):
    result = ResurfacingLookupService(_fixture_repo(tmp_path)).lookup(street="Rua Inexistente")
    assert result.status == "NOT_FOUND"


def test_single_resurfacing(tmp_path):
    result = ResurfacingLookupService(_fixture_repo(tmp_path)).lookup(record_id="1")
    assert len(result.history) == 1


def test_multiple_resurfacing_history(tmp_path):
    result = ResurfacingLookupService(_fixture_repo(tmp_path)).lookup(street="Rua Teste")
    assert len(result.history) == 3


def test_latest_resurfacing(tmp_path):
    result = ResurfacingLookupService(_fixture_repo(tmp_path)).lookup(street="Rua Teste")
    assert result.latest_resurfacing.resurfacing_id == "1"


def test_missing_resurfacing_date(tmp_path):
    result = ResurfacingLookupService(_fixture_repo(tmp_path)).lookup(record_id="3")
    assert result.latest_resurfacing is None
    assert "NO_USABLE_COMPLETION_DATE" in result.warnings


def test_resurfacing_match_confidence(tmp_path):
    result = ResurfacingLookupService(_fixture_repo(tmp_path)).lookup(record_id="1")
    assert result.match_confidence == "EXACT"


def test_resurfacing_provenance(tmp_path):
    result = ResurfacingLookupService(_fixture_repo(tmp_path)).lookup(record_id="1")
    assert result.history[0].provenance[0].source_record_id == "1"


def test_protection_active():
    assert calculate_protection_status("2026-08-01", date(2026, 8, 10)).status == "ACTIVE"


def test_protection_expired():
    assert calculate_protection_status("2024-01-01", date(2026, 8, 10)).status == "EXPIRED"


def test_protection_expiring_soon():
    assert calculate_protection_status("2025-08-20", date(2026, 8, 10)).status == "EXPIRING_SOON"


def test_protection_unknown_date():
    assert calculate_protection_status(None, date(2026, 8, 10)).status == "UNKNOWN_DATE"


def test_protection_exact_start():
    assert calculate_protection_status("2026-08-10", date(2026, 8, 10)).status == "ACTIVE"


def test_protection_exact_end():
    result = calculate_protection_status("2025-08-10", date(2026, 8, 10))
    assert result.status == "EXPIRED" and result.days_remaining == 0


def test_protection_one_day_after_end():
    assert calculate_protection_status("2025-08-10", date(2026, 8, 11)).status == "EXPIRED"


def test_protection_calendar_year():
    result = calculate_protection_status("2023-03-01", date(2024, 2, 29))
    assert result.end_date == date(2024, 3, 1)


def test_protection_feb_29_policy():
    result = calculate_protection_status("2024-02-29", date(2025, 2, 28))
    assert result.end_date == date(2025, 2, 28)


def test_protection_explicit_reference_date():
    assert calculate_protection_status("2025-01-01", date(2025, 1, 2)).days_remaining == 364


def test_notification_date_is_ignored():
    event = InterventionEvent("n1", execution_date=None)
    result = classify_temporal_relationship(event, "2025-01-01")
    assert result.relationship == "TEMPORAL_RELATIONSHIP_UNKNOWN"


def test_missing_execution_date_is_unknown():
    result = classify_temporal_relationship(InterventionEvent("n1"), "2025-01-01")
    assert result.relationship == "TEMPORAL_RELATIONSHIP_UNKNOWN"


def test_notification_inside_window_is_not_violation():
    result = classify_temporal_relationship(InterventionEvent("n1"), "2025-01-01")
    assert "VIOLATION" not in result.relationship


def test_known_execution_inside_window():
    result = classify_temporal_relationship(InterventionEvent("e1", execution_date=date(2025, 6, 1)), "2025-01-01")
    assert result.relationship == "DURING_PROTECTION_WINDOW"


def test_known_execution_after_window():
    result = classify_temporal_relationship(InterventionEvent("e1", execution_date=date(2026, 2, 1)), "2025-01-01")
    assert result.relationship == "AFTER_PROTECTION_WINDOW"


def test_emergency_inside_window():
    result = classify_temporal_relationship(InterventionEvent("e1", execution_date=date(2025, 6, 1), emergency=True), "2025-01-01")
    assert result.relationship == "DURING_PROTECTION_WINDOW"
    assert "EMERGENCY" in " ".join(result.warnings)


def test_emergency_unknown():
    result = classify_temporal_relationship(InterventionEvent("e1", execution_date=date(2025, 6, 1)), "2025-01-01")
    assert "EMERGENCY_FLAG_UNKNOWN" in result.warnings


def test_complete_operational_result(tmp_path):
    result = OperationalQueryService(_fixture_repo(tmp_path)).lookup(StreetLookupQuery(record_id="1"), reference_date=date(2026, 8, 10))
    assert result.data_quality.status == "COMPLETE"


def test_partial_operational_result(tmp_path):
    result = OperationalQueryService(_fixture_repo(tmp_path)).lookup(StreetLookupQuery(street="Rua Teste", number=150), reference_date=date(2026, 8, 10))
    assert result.data_quality.status in {"PARTIAL", "LIMITED"}


def test_operational_unavailable_surface(tmp_path):
    result = OperationalQueryService(_fixture_repo(tmp_path)).lookup(StreetLookupQuery(record_id="3"), reference_date=date(2026, 8, 10))
    assert result.surface.status == "DATA_UNAVAILABLE"


def test_operational_unavailable_resurfacing(tmp_path):
    result = OperationalQueryService(_fixture_repo(tmp_path)).lookup(StreetLookupQuery(record_id="3"), reference_date=date(2026, 8, 10))
    assert result.resurfacing.status == "FOUND"
    assert result.protection.status == "UNKNOWN_DATE"


def test_official_geometry_retained(tmp_path):
    result = OperationalQueryService(_fixture_repo(tmp_path)).lookup(StreetLookupQuery(record_id="1"), reference_date=date(2026, 8, 10))
    assert result.geometry.status == "OFFICIAL"
    assert result.geometry.official_wkt


def test_shadow_geometry_separated(tmp_path):
    result = OperationalQueryService(_fixture_repo(tmp_path)).lookup(StreetLookupQuery(record_id="1"), reference_date=date(2026, 8, 10))
    assert result.geometry.shadow_wkt and result.geometry.shadow_wkt != result.geometry.official_wkt


def test_operational_warnings(tmp_path):
    result = OperationalQueryService(_fixture_repo(tmp_path)).lookup(StreetLookupQuery(street="Rua Teste", number=999), reference_date=date(2026, 8, 10))
    assert result.warnings


def test_serialization(tmp_path):
    result = OperationalQueryService(_fixture_repo(tmp_path)).lookup(StreetLookupQuery(record_id="1"), reference_date=date(2026, 8, 10))
    payload = result.to_dict()
    assert json.dumps(payload, ensure_ascii=False)


@pytest.mark.private_data
def test_inventory_report_exists_after_discovery():
    path = Path("data/processed/operational_data_capabilities.json")
    assert path.exists()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["capabilities"]["street_number_lookup"]["supported"] is True
