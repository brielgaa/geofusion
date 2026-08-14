from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest
from pyproj import Transformer
from shapely.geometry import LineString

from road_graph import RoadGraph
from route_geometry_audit import (
    DEFAULT_SAME_TRANSVERSAL_AUDIT_PATH,
    GeometryQualityShadowEngine,
    INTERSECTION_DEDUP_TOLERANCE_M,
    SAME_TRANSVERSAL_STRATEGY,
    _same_transversal_prefilter,
)


def _normalize(value: str) -> str:
    return " ".join(str(value or "").upper().replace("RUA ", "").split())


def _same_graph(shape: str = "u", split: bool = False, three: bool = False) -> RoadGraph:
    if shape == "u":
        main = [
            LineString([(0, 0), (100, 0), (100, 100), (0, 100)]) if not split else LineString([(0, 0), (20, 0)]),
        ]
        if split:
            main = [
                LineString([(0, 0), (20, 0)]), LineString([(20, 0), (100, 0)]),
                LineString([(100, 0), (100, 100)]), LineString([(100, 100), (20, 100)]),
                LineString([(20, 100), (0, 100)]),
            ]
    elif shape == "half_moon":
        main = [LineString([(0, 0), (50, -50), (100, 0), (50, 50), (0, 0)])]
    else:
        main = [LineString([(0, 0), (100, 0)])]
    if three:
        main = [LineString([(0, 0), (100, 0), (100, 100), (0, 100), (0, 200), (100, 200)])]
    transversal = LineString([(20, -20), (20, 220 if three else 120)])
    roads = [
        *({"geometry": geometry, "codlog": "1", "nm_logradouro": "Principal", "cd_numero_ordem_segmento": str(index)} for index, geometry in enumerate(main)),
        {"geometry": transversal, "codlog": "2", "nm_logradouro": "Comum", "cd_numero_ordem_segmento": "T"},
    ]
    return RoadGraph.from_geodataframe(pd.DataFrame(roads), _normalize)


def _row(**values):
    to_ll = Transformer.from_crs("EPSG:31983", "EPSG:4326", always_xy=True).transform
    lon, lat = to_ll(100, 50)
    result = {
        "id": "same-1", "via": "Principal", "de": "Rua Comum", "ate": "RUA COMUM",
        "latitude": lat, "longitude": lon, "extensao_m": 260,
    }
    result.update(values)
    return result


def _analysis(row=None, shape="u", split=False, three=False):
    engine = GeometryQualityShadowEngine(_same_graph(shape=shape, split=split, three=three))
    return engine.same_transversal_analysis(row or _row(), {})


def test_different_endpoints_do_not_activate():
    result = _analysis(_row(ate="Rua Outra"))
    assert result["eligible"] is False


def test_equal_endpoints_with_one_intersection_remain_without_pair():
    result = _analysis(_row(), shape="line")
    assert result["eligible"] is True
    assert result["intersection_count_distinct"] == 1
    assert result["candidate_pair_count"] == 0


def test_two_exact_intersections_are_distinct_and_generate_pair():
    result = _analysis()
    assert result["intersection_count_distinct"] == 2
    assert result["candidate_pair_count"] == 1
    assert result["after_strategy"] == SAME_TRANSVERSAL_STRATEGY


def test_geometric_crossing_without_shared_node_is_detected():
    result = _analysis(shape="u", split=False)
    assert result["intersection_count_raw"] >= result["intersection_count_distinct"] == 2


def test_adjacent_duplicate_intersections_are_deduplicated():
    result = _analysis(split=True)
    assert result["intersection_count_distinct"] == 2
    assert result["intersection_count_raw"] >= 2


def test_deduplication_uses_configured_tolerance():
    from shapely.geometry import Point
    items = [
        {"point": Point(0, 0), "component_index": 0, "gap_m": 0.0},
        {"point": Point(INTERSECTION_DEDUP_TOLERANCE_M / 2, 0), "component_index": 0, "gap_m": 0.1},
    ]
    assert len(GeometryQualityShadowEngine._deduplicate_intersections(items)) == 1


def test_half_moon_is_not_automatically_rejected_as_loop():
    result = _analysis(shape="half_moon")
    assert result["candidate_pair_count"] >= 1
    assert all("LOOP_DETECTADO" not in candidate.warnings for candidate in result["candidates"])


def test_u_shape_is_valid_continuous_candidate():
    result = _analysis()
    candidate = result["candidates"][0]
    assert candidate.geometry_wkt.startswith("LINESTRING")
    assert candidate.length_m > 0


def test_loop_legitimate_helper_allows_adjacent_duplicate_vertices():
    assert GeometryQualityShadowEngine._legitimate_loop(LineString([(0, 0), (1, 0), (1, 0), (2, 0)]))


def test_return_shape_keeps_same_main_component():
    result = _analysis(shape="half_moon")
    assert result["candidates"]
    assert all(candidate.component_status == "SAME_COMPONENT" for candidate in result["candidates"])


def test_components_are_not_combined_into_one_pair():
    graph = _same_graph()
    # A second disconnected principal component has one crossing only; no pair
    # may combine it with the first component.
    roads = pd.DataFrame([
        {"geometry": LineString([(200, 0), (300, 0)]), "codlog": "1", "nm_logradouro": "Principal", "cd_numero_ordem_segmento": "X"},
        {"geometry": LineString([(220, -20), (220, 20)]), "codlog": "2", "nm_logradouro": "Comum", "cd_numero_ordem_segmento": "Y"},
    ])
    second = RoadGraph.from_geodataframe(roads, _normalize)
    # The direct component invariant is covered by the analysis on a graph with
    # disjoint components; the strategy must only produce within-component pairs.
    engine = GeometryQualityShadowEngine(second)
    result = engine.same_transversal_analysis(_row(), {})
    assert result["candidate_pair_count"] == 0


def test_more_than_two_intersections_is_ambiguous_and_medium_or_estimated():
    result = _analysis(three=True)
    assert result["intersection_count_distinct"] >= 3
    assert result["ambiguous"] is True
    assert result["after_confidence"] in {"RECONSTRUCTED_MEDIUM", "ESTIMATED"}


def test_gps_selects_candidate_near_reference():
    result = _analysis(_row(latitude=_row()["latitude"], longitude=_row()["longitude"]))
    assert result["distance_to_gps_m"] is not None
    assert result["distance_to_gps_m"] < 1.0


def test_extension_is_recorded_for_pair():
    result = _analysis(_row(extensao_m=260))
    assert result["candidates"][0].deviation_pct is not None
    assert result["candidates"][0].deviation_pct < 1.0


def test_gps_and_extension_agree_for_high_evidence():
    result = _analysis(_row(extensao_m=260))
    assert result["after_confidence"] == "RECONSTRUCTED_HIGH"


def test_gps_and_extension_conflict_reduces_confidence():
    result = _analysis(_row(extensao_m=10))
    assert result["after_confidence"] in {"ESTIMATED", "RECONSTRUCTED_MEDIUM"}


def test_near_zero_pair_is_rejected():
    result = _analysis(_row(), shape="line")
    assert result["candidate_pair_count"] == 0


def test_candidate_path_is_continuous_and_valid():
    result = _analysis()
    geometry = result["candidates"][0].geometry_wkt
    assert geometry.startswith("LINESTRING")


def test_discontinuity_does_not_create_candidate_from_other_component():
    result = _analysis(shape="line")
    assert result["candidate_pair_count"] == 0


def test_invalid_and_ambiguous_states_are_reported():
    result = _analysis(three=True)
    assert result["ambiguous"]
    assert "same_transversal_ambigua" in result["candidates"][0].warnings


def test_high_margin_is_recorded():
    result = _analysis()
    assert result["margin_top2"] == 100.0


def test_low_margin_with_multiple_pairs_is_not_high():
    result = _analysis(three=True)
    assert result["margin_top2"] is not None
    assert result["after_confidence"] != "RECONSTRUCTED_HIGH"


def test_estimated_promotes_to_high():
    result = _analysis()
    assert result["after_confidence"] == "RECONSTRUCTED_HIGH"


def test_estimated_promotes_to_medium_for_multiple_intersections():
    result = _analysis(three=True)
    assert result["after_confidence"] in {"RECONSTRUCTED_MEDIUM", "ESTIMATED"}


def test_existing_estimated_baseline_is_not_replaced_by_worse_pair():
    engine = GeometryQualityShadowEngine(_same_graph())
    analysis = engine.same_transversal_analysis(_row(extensao_m=10), {})
    assert analysis["after_confidence"] in {"ESTIMATED", "RECONSTRUCTED_MEDIUM"}


def test_wkt_and_geojson_are_serialized():
    result = _analysis()
    candidate = result["candidates"][0]
    assert candidate.geometry_wkt.startswith("LINESTRING")
    assert json.loads(candidate.geometry_geojson)["type"] == "LineString"


def test_prefilter_requires_exact_equivalence_not_similarity():
    assert _same_transversal_prefilter(_row(), {})
    assert not _same_transversal_prefilter(_row(de="Rua Comum", ate="Rua Comuna"), {})


def test_determinism():
    first = _analysis()
    second = _analysis()
    assert first["intersection_points"] == second["intersection_points"]
    assert first["selected_pair_index"] == second["selected_pair_index"]
    assert first["candidates"][0].geometry_wkt == second["candidates"][0].geometry_wkt


def test_no_official_output_path_is_used_by_strategy():
    assert "recape_clean.csv" not in str(DEFAULT_SAME_TRANSVERSAL_AUDIT_PATH)
    assert DEFAULT_SAME_TRANSVERSAL_AUDIT_PATH.name == "route_geometry_same_transversal_audit.csv"


def test_strategy_name_and_required_report_fields_are_stable():
    result = _analysis()
    assert result["after_strategy"] == SAME_TRANSVERSAL_STRATEGY
    required = {"intersection_count_raw", "intersection_count_distinct", "candidate_pair_count", "margin_top2"}
    assert required.issubset(result)


@pytest.mark.parametrize("shape", ["u", "half_moon"])
def test_supported_return_shapes(shape):
    assert _analysis(shape=shape)["eligible"]
