from __future__ import annotations

import json
from pathlib import Path

from shapely.geometry import LineString, mapping

from src.operational.lookup_index import build_lookup_index, index_status
from src.operational.models import StreetLookupQuery
from src.operational.repository import OperationalRepository, WGS84_TO_METRIC
from src.operational.services import StreetLookupService


def _write_source(root: Path) -> None:
    cache = root / "data" / "cache"
    cache.mkdir(parents=True)
    point = WGS84_TO_METRIC.transform(-46.7, -23.5)
    line = LineString([(point[0] - 10, point[1]), (point[0] + 10, point[1])])
    (cache / "geosampa_segmento_logradouro.geojson").write_text(
        json.dumps({
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature",
                "id": "segment-1",
                "properties": {
                    "nm_logradouro": "RUA ÍNDICE",
                    "codlog": "0001",
                    "cd_numero_inicial_par": 10,
                    "cd_numero_final_par": 20,
                    "cd_numero_inicial_impar": 11,
                    "cd_numero_final_impar": 21,
                },
                "geometry": mapping(line),
            }],
        }, ensure_ascii=False),
        encoding="utf-8",
    )


def test_persisted_index_serves_text_without_loading_full_segments(tmp_path):
    _write_source(tmp_path)
    result = build_lookup_index(tmp_path, source_sha256=False)
    assert result["record_count"] == 1
    assert index_status(tmp_path)["valid"] is True

    repository = OperationalRepository(tmp_path)
    lookup = StreetLookupService(repository).lookup(StreetLookupQuery(street="Rua Índice", number=12))

    assert lookup.confidence == "EXACT"
    assert lookup.segment_id == "segment-1"
    assert repository._segments is None
    assert repository._lookup_index is not None


def test_stale_persisted_index_falls_back_to_full_geojson(tmp_path):
    _write_source(tmp_path)
    build_lookup_index(tmp_path, source_sha256=False)
    source = tmp_path / "data" / "cache" / "geosampa_segmento_logradouro.geojson"
    source.write_text(source.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    assert index_status(tmp_path)["valid"] is False
    repository = OperationalRepository(tmp_path)
    lookup = StreetLookupService(repository).lookup(StreetLookupQuery(street="Rua Índice", number=12))

    assert lookup.confidence == "EXACT"
    assert repository._lookup_index is None
    assert repository._segments is not None


def test_spatial_lookup_keeps_full_spatial_resource_separate(tmp_path):
    _write_source(tmp_path)
    build_lookup_index(tmp_path, source_sha256=False)
    repository = OperationalRepository(tmp_path)

    pairs = repository.spatial_segments(-23.5, -46.7, max_distance_m=50)

    assert pairs
    assert repository._segments is not None
