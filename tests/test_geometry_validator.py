from __future__ import annotations

import ast

import pandas as pd
import pytest
from shapely.geometry import LineString, MultiLineString, Point

from geometry_validator import (
    Calibration,
    GeometryValidationContext,
    IndependentGeometryValidator,
    NEGATIVE_LABELS,
    make_negative_controls,
    normalize_name,
)
from road_graph import RoadGraph


def _graph():
    roads = [
        {"geometry": LineString([(0, 0), (100, 0)]), "codlog": "1", "nm_logradouro": "Principal", "cd_numero_ordem_segmento": "1"},
        {"geometry": LineString([(0, 0), (0, 20)]), "codlog": "2", "nm_logradouro": "Inicio", "cd_numero_ordem_segmento": "1"},
        {"geometry": LineString([(100, 0), (100, 20)]), "codlog": "3", "nm_logradouro": "Fim", "cd_numero_ordem_segmento": "1"},
        {"geometry": LineString([(50, 0), (50, -20)]), "codlog": "4", "nm_logradouro": "Lateral", "cd_numero_ordem_segmento": "1"},
    ]
    return RoadGraph.from_geodataframe(pd.DataFrame(roads), normalize_name)


def _calibration():
    return Calibration(
        positive_calibration_count=30,
        positive_validation_count=10,
        negative_calibration_count=150,
        negative_validation_count=50,
        high_score_threshold=70,
        medium_score_threshold=45,
        high_evidence_threshold=3,
        medium_evidence_threshold=2,
        main_alignment_reject_ratio=0.25,
        continuity_hard_gap_m=5,
        boundary_hard_distance_m=5,
        extension_hard_deviation_pct=50,
        gps_on_path_tolerance_m=5,
        gps_near_path_tolerance_m=15,
    )


def _context(geometry=None, **values):
    graph = values.pop("graph", _graph())
    calibration = values.pop("calibration", _calibration())
    defaults = dict(
        record_id="case",
        geometry=geometry if geometry is not None else LineString([(0, 0), (100, 0)]),
        via="Principal",
        de="Inicio",
        ate="Fim",
        extension_m=100,
        graph=graph,
        calibration=calibration,
    )
    defaults.update(values)
    return GeometryValidationContext(**defaults)


@pytest.mark.parametrize(
    "category",
    [
        "straight", "short", "long", "multipart", "empty", "point", "wrong_street",
        "offset", "loop", "self_intersection", "duplicate_vertex", "gap", "crossing",
        "same_component", "multiple_components", "de_confirmed", "ate_confirmed", "de_missing",
        "ate_missing", "boundary_contradiction", "extension_equal", "extension_low", "extension_high",
        "gps_missing", "gps_near", "gps_off", "no_graph", "one_candidate", "many_candidates",
        "alternative_competition",
    ],
)
def test_thirty_geometry_evidence_categories_are_total(category):
    geometry = LineString([(0, 0), (100, 0)])
    values = {}
    if category == "multipart":
        geometry = MultiLineString([[(0, 0), (50, 0)], [(50, 0), (100, 0)]])
    elif category == "empty":
        geometry = None
    elif category == "point":
        geometry = Point(0, 0)
    elif category in {"wrong_street", "offset"}:
        geometry = LineString([(0, 100), (100, 100)])
    elif category in {"loop", "self_intersection"}:
        geometry = LineString([(0, 0), (100, 0), (0, 0), (100, 0)])
    elif category == "duplicate_vertex":
        geometry = LineString([(0, 0), (50, 0), (50, 0), (100, 0)])
    elif category == "gap":
        geometry = LineString([(0, 0), (50, 0), (550, 500), (100, 0)])
    elif category == "multiple_components":
        geometry = MultiLineString([[(0, 0), (20, 0)], [(80, 0), (100, 0)]])
    elif category == "boundary_contradiction":
        values["de"] = "Inexistente"
    elif category == "extension_high":
        values["extension_m"] = 10
    elif category == "extension_low":
        values["extension_m"] = 95
    elif category == "gps_near":
        values.update(latitude=-23.0, longitude=-46.0)
    elif category == "gps_off":
        values.update(latitude=-20.0, longitude=-40.0)
    elif category == "no_graph":
        values.update(graph=None)
    elif category == "alternative_competition":
        values["alternatives"] = [LineString([(0, 0), (100, 0)])]
    result = IndependentGeometryValidator(values.get("graph", _graph()), _calibration()).validate(_context(geometry, **values))
    assert result.validation_class in {"VALIDATED_HIGH", "VALIDATED_MEDIUM", "INSUFFICIENT_EVIDENCE", "REJECTED"}
    assert 0 <= result.validation_score <= 100
    assert result.evidence.geometry_type or category in {"empty"}


def test_positive_geometry_is_high_and_negative_controls_are_not_promoted():
    context = _context()
    validator = IndependentGeometryValidator(context.graph, context.calibration)
    positive = validator.validate(context)
    assert positive.validation_class == "VALIDATED_HIGH"
    negatives = make_negative_controls([context])
    results = [validator.validate(item) for item in negatives]
    assert {item.control_label for item in negatives} == set(NEGATIVE_LABELS)
    assert any(result.validation_class == "REJECTED" for result in results)
    assert all(result.promotion_recommendation != "PROMOTE_HIGH" for result in results)


def test_validator_does_not_import_generation_or_resolution_modules():
    tree = ast.parse(open("src/geometry_validator.py", encoding="utf-8").read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "route_geometry_audit" not in imported
    assert "street_resolver" not in imported
    assert "transform" not in imported
