from __future__ import annotations

import ast
import hashlib

import pandas as pd
import pytest
from shapely.geometry import LineString

from boundary_contradiction_audit import (
    BoundaryCalibration,
    BoundaryContext,
    BoundaryContradictionAuditEngine,
    RoadGraph,
    build_parser,
    make_negative_controls,
    normalize_name,
)


def _graph():
    roads = [
        {"geometry": LineString([(0, 0), (50, 0)]), "codlog": "1", "nm_logradouro": "Principal", "cd_numero_ordem_segmento": "1"},
        {"geometry": LineString([(50, 0), (100, 0)]), "codlog": "1", "nm_logradouro": "Principal", "cd_numero_ordem_segmento": "2"},
        {"geometry": LineString([(0, -20), (0, 0)]), "codlog": "2", "nm_logradouro": "Inicio", "cd_numero_ordem_segmento": "1"},
        {"geometry": LineString([(100, 0), (100, 20)]), "codlog": "3", "nm_logradouro": "Fim", "cd_numero_ordem_segmento": "1"},
        {"geometry": LineString([(50, -20), (50, 20)]), "codlog": "4", "nm_logradouro": "Lateral", "cd_numero_ordem_segmento": "1"},
        {"geometry": LineString([(0, 5), (100, 5)]), "codlog": "5", "nm_logradouro": "Paralela", "cd_numero_ordem_segmento": "1"},
    ]
    return RoadGraph.from_geodataframe(pd.DataFrame(roads), normalize_name)


def _calibration():
    return BoundaryCalibration(30, 10, 180, 60, 8, 25, 4, 3, 12, 60, 70, 45, 5, 3, 0.08)


def _context(de="Inicio", ate="Fim", geometry=None, de_current=None, ate_current=None):
    return BoundaryContext(
        record_id="case", via="Principal", via_resolved="Principal", de=de, ate=ate,
        de_current=de if de_current is None else de_current,
        ate_current=ate if ate_current is None else ate_current,
        geometry=geometry or LineString([(0, 0), (100, 0)]), extension_m=100,
        latitude=None, longitude=None, gps_status="UNAVAILABLE", gps_distance_m=None,
        extension_deviation_pct=0.0, main_street="Principal", root_cause_primary="AUSENCIA_DE_INTERSECAO",
    )


@pytest.mark.parametrize(
    "label,kwargs",
    [
        ("de_correct", {}), ("ate_correct", {}), ("both_correct", {}),
        ("de_wrong", {"de": "Rua Desconhecida"}), ("ate_wrong", {"ate": "Rua Desconhecida"}),
        ("both_wrong", {"de": "Rua X", "ate": "Rua Y"}),
        ("reversed", {"geometry": LineString([(100, 0), (0, 0)])}),
        ("abbreviated", {"de": "R. Inicio"}), ("incomplete", {"de": "Ini"}),
        ("not_found", {"de": "Rua Que Nao Existe"}),
        ("geometric_no_node", {"de": "Lateral"}), ("small_gap", {"de": "Lateral"}),
        ("large_gap", {"geometry": LineString([(0, 0), (50, 20), (100, 0)])}),
        ("wrong_component", {"de_current": "Paralela"}), ("parallel", {"de": "Paralela"}),
        ("multiple_intersections", {"de": "Lateral"}), ("same_transversal", {"de": "Inicio", "ate": "Inicio"}),
        ("only_de", {"ate": ""}), ("only_ate", {"de": ""}), ("null_like", {"de": "", "ate": ""}),
    ],
)
def test_boundary_categories_are_deterministic_and_total(label, kwargs):
    graph = _graph()
    engine = BoundaryContradictionAuditEngine(graph, _calibration())
    first = engine.validate(_context(**kwargs))
    second = engine.validate(_context(**kwargs))
    assert first.to_row() == second.to_row(), label
    assert first.recommendation in {
        "BOUNDARIES_VALIDATED_HIGH", "BOUNDARIES_VALIDATED_MEDIUM", "ONE_BOUNDARY_VALIDATED",
        "BOUNDARIES_REVERSED", "KEEP_CONTRADICTION", "DATA_INSUFFICIENT",
    }
    assert first.boundary_validation_score >= 0
    assert first.candidate_geometry_wkt


def test_both_valid_boundaries_generate_temporary_diagnostic_comparison():
    result = BoundaryContradictionAuditEngine(_graph(), _calibration()).validate(_context())
    assert result.recovered_both is True
    assert result.de_status in {"VALID", "PLAUSIBLE"}
    assert result.ate_status in {"VALID", "PLAUSIBLE"}
    assert result.diagnostic_geometry_wkt
    assert result.length_difference_pct is not None


def test_negative_controls_are_created_without_mutating_positive_context():
    positive = [_context()]
    negatives = make_negative_controls(positive, max_per_label=1)
    assert {item.control_label for item in negatives} == {
        "SWAP_DE", "SWAP_ATE", "INVERT_ONE_BOUNDARY", "PARALLEL_STREET", "WRONG_COMPONENT", "NEAR_WRONG_BOUNDARY",
    }
    assert positive[0].de == "Inicio" and positive[0].ate == "Fim"


def test_wkt_and_null_geometry_are_safe():
    engine = BoundaryContradictionAuditEngine(_graph(), _calibration())
    context = _context()
    context.geometry = None
    result = engine.validate(context)
    assert result.recommendation == "DATA_INSUFFICIENT"
    assert result.candidate_geometry_wkt == ""


def test_no_generator_or_resolver_dependency():
    tree = ast.parse(open("src/boundary_contradiction_audit.py", encoding="utf-8").read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "geometry_validator" not in imported
    assert "street_resolver" not in imported
    assert "route_geometry_audit" not in imported


def test_no_official_file_is_written_by_module_constants():
    source = open("src/boundary_contradiction_audit.py", encoding="utf-8").read()
    assert "recape_clean.csv" in source  # positive-control input only
    assert "outputs oficiais" not in source.lower()


def test_cli_exposes_shadow_sample_resume_cache_and_filters():
    args = build_parser().parse_args([
        "--shadow", "--sample", "30", "--resume", "--reset-cache",
        "--only-root-cause", "BOTH_INVALID", "--only-id", "123",
    ])
    assert args.shadow is True
    assert args.sample == 30
    assert args.resume and args.reset_cache
    assert args.only_root_cause == ["BOTH_INVALID"]
    assert args.only_id == ["123"]


def test_result_row_preserves_id_and_diagnostic_wkt():
    result = BoundaryContradictionAuditEngine(_graph(), _calibration()).validate(_context())
    row = result.to_row()
    assert row["id"] == "case"
    assert row["candidate_geometry_wkt"].startswith("LINESTRING")
    assert "geometry_score" not in row


def test_positive_control_has_no_official_promotion_side_effect():
    context = _context()
    result = BoundaryContradictionAuditEngine(_graph(), _calibration()).validate(context)
    assert context.source_type == "ESTIMATED"
    assert result.recommendation.startswith("BOUNDARIES_") or result.recommendation == "ONE_BOUNDARY_VALIDATED"
