from __future__ import annotations

from pathlib import Path

import pytest

from dashboard.pages.query import _parse_coordinates, _query_from_input
from dashboard.services.integrity import protected_hashes
from dashboard.services.operational_dashboard import dataset_signature, load_operational_context
from src.operational.models import StreetLookupQuery


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def context():
    return load_operational_context.__wrapped__(str(ROOT), dataset_signature(ROOT))


@pytest.mark.private_data
def test_operational_context_uses_real_artifacts(context):
    assert len(context.recapes) == 5022
    assert int(context.recapes["quality_code"].eq("OFFICIAL").sum()) == 1577
    assert set(context.recapes["protection_status"]) <= {"ACTIVE", "EXPIRING_SOON", "EXPIRED", "UNKNOWN_DATE"}


@pytest.mark.private_data
def test_ambiguous_street_is_preserved_by_service(context):
    result = context.service.lookup(StreetLookupQuery(street="AVENIDA PAULISTA"), reference_date="2026-08-14")
    assert result.location.confidence == "AMBIGUOUS"
    assert result.location.candidate_count > 1
    assert "DO_NOT_SELECT_SILENTLY" in result.location.warnings


def test_query_input_supports_coordinates_and_record_ids():
    assert _parse_coordinates("-23,55; -46,73") == (-23.55, -46.73)
    assert _query_from_input("ID 6934", "") == StreetLookupQuery(record_id="6934")
    assert _query_from_input("-23.55,-46.73", "") == StreetLookupQuery(latitude=-23.55, longitude=-46.73)


@pytest.mark.private_data
def test_protected_hashes_are_present():
    hashes = protected_hashes(ROOT)
    assert hashes["src/road_graph.py"]
    assert hashes["data/processed/recape_clean.csv"]
