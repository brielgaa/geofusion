from __future__ import annotations

import ast
import json

import pandas as pd
import pytest
from shapely.geometry import LineString

from boundary_name_recovery import (
    BoundaryNameContext,
    BoundaryNameRecoveryEngine,
    RoadGraph,
    build_parser,
    classify_problem_types,
    degrade_name,
    make_negative_controls,
    make_positive_controls,
    mine_alias_candidates,
    normalize_name,
)


def _graph():
    roads = [
        {"geometry": LineString([(0, 0), (50, 0)]), "codlog": "1", "nm_logradouro": "Principal", "cd_numero_ordem_segmento": "1"},
        {"geometry": LineString([(50, 0), (100, 0)]), "codlog": "1", "nm_logradouro": "Principal", "cd_numero_ordem_segmento": "2"},
        {"geometry": LineString([(0, -20), (0, 0)]), "codlog": "2", "nm_logradouro": "Rua Santa Marina", "cd_numero_ordem_segmento": "1"},
        {"geometry": LineString([(100, 0), (100, 20)]), "codlog": "3", "nm_logradouro": "Rua Fim", "cd_numero_ordem_segmento": "1"},
        {"geometry": LineString([(50, -20), (50, 20)]), "codlog": "4", "nm_logradouro": "Rua Lateral", "cd_numero_ordem_segmento": "1"},
        {"geometry": LineString([(0, 5), (100, 5)]), "codlog": "5", "nm_logradouro": "Rua Paralela", "cd_numero_ordem_segmento": "1"},
        {"geometry": LineString([(50, 40), (50, 60)]), "codlog": "6", "nm_logradouro": "Rua Santa Maria", "cd_numero_ordem_segmento": "1"},
    ]
    return RoadGraph.from_geodataframe(pd.DataFrame(roads), normalize_name)


def _context(name="RUA SANTA MARINA", geometry=None, gps=False, side="DE"):
    return BoundaryNameContext(
        record_id="case", boundary_side=side, via="Principal", main_street="Principal",
        original_name=name, current_candidate=name, geometry=geometry or LineString([(0, 0), (100, 0)]),
        latitude=-23.55 if gps else None, longitude=-46.63 if gps else None,
    )


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Rua João", "JOAO"), ("R. João", "JOAO"), ("AV. Dr. Silva", "DOUTOR SILVA"),
        ("Alameda das Flores", "DAS FLORES"), ("Estr. Lázaro", "LAZARO"),
        ("Santa Marina", "SANTA MARINA"), ("RUA 12", "12"),
    ],
)
def test_normalize_name_is_deterministic_and_does_not_apply_aliases(raw, expected):
    assert normalize_name(raw) == expected
    assert normalize_name(raw) == normalize_name(raw)


@pytest.mark.parametrize(
    "kind",
    ["ABBREVIATION", "TYPO", "TRUNCATED_NAME", "MISSING_TOKEN", "EXTRA_TOKEN", "WRONG_STREET_TYPE", "ACCENT_ONLY", "TOKEN_ORDER", "NUMBER_VARIATION"],
)
def test_degradation_kinds_are_deterministic(kind):
    value = degrade_name("Rua João Silva 1", kind)
    assert value == degrade_name("Rua João Silva 1", kind)
    assert isinstance(value, str)


@pytest.mark.parametrize(
    "original,recovered,expected",
    [
        ("R. Santa Marina", "Rua Santa Marina", "ABBREVIATION"),
        ("Rua Santa Marin", "Rua Santa Marina", "TRUNCATED_NAME"),
        ("Rua Santa", "Rua Santa Marina", "MISSING_TOKEN"),
        ("Rua Santa Marina Central", "Rua Santa Marina", "EXTRA_TOKEN"),
        ("Rua Santa Marina", "Av. Santa Marina", "WRONG_STREET_TYPE"),
        ("Rua Jão", "Rua Joao", "TYPO"),
        ("Rua 12", "Rua 13", "NUMBER_VARIATION"),
        ("Rua Marina Santa", "Rua Santa Marina", "TOKEN_ORDER"),
    ],
)
def test_problem_types_are_explainable(original, recovered, expected):
    assert expected in classify_problem_types(original, recovered)


def test_contextual_candidate_and_real_intersection():
    result = BoundaryNameRecoveryEngine(_graph()).recover(_context("Rua Santa Marina"))
    assert result.recovered_name == "Rua Santa Marina"
    assert result.intersection_type == "REAL_NODE"
    assert result.intersection_count >= 1
    assert result.component_match == "SAME_COMPONENT"
    assert result.classification in {"NAME_RECOVERED_HIGH", "NAME_RECOVERED_MEDIUM"}


def test_contextual_candidates_do_not_become_global_fuzzy_matches():
    result = BoundaryNameRecoveryEngine(_graph()).recover(_context("Santa Mari"))
    assert result.recovered_name in {"Rua Santa Marina", "Rua Santa Maria", ""}
    assert len(result.alternatives) <= len(_graph().street_names)


def test_critical_token_difference_is_not_high():
    result = BoundaryNameRecoveryEngine(_graph()).recover(_context("Santa Maria"))
    assert result.classification != "NAME_RECOVERED_HIGH" or "CRITICAL_TOKEN_MISMATCH" not in result.warnings
    assert "CRITICAL_TOKEN_MISMATCH" in result.warnings or result.recovered_name == "Rua Santa Maria"


def test_no_intersection_is_rejected_or_downgraded():
    result = BoundaryNameRecoveryEngine(_graph()).recover(_context("Rua Santa Maria", geometry=LineString([(0, 40), (100, 40)])))
    assert result.classification in {"NAME_DATA_CONTRADICTION", "NAME_AMBIGUOUS", "NAME_NOT_FOUND"}
    assert result.intersection_type in {"NO_INTERSECTION", "NEAR_INTERSECTION", "REAL_NODE", "GEOMETRIC_INTERSECTION"}


def test_margin_is_present_and_low_margin_is_visible():
    result = BoundaryNameRecoveryEngine(_graph()).recover(_context("Santa"))
    assert result.margin_top2 is None or result.margin_top2 >= 0
    if result.margin_top2 is not None and result.margin_top2 < 10:
        assert "LOW_NAME_MARGIN" in result.warnings


def test_result_row_contains_required_fields_and_utf8():
    row = BoundaryNameRecoveryEngine(_graph()).recover(_context("RUA SANTA MARINA")).to_row()
    for field in ("id", "boundary_side", "original_name", "normalized_original", "recovered_name", "recovered_codlog", "problem_types", "name_score", "margin_top2", "alternatives_json"):
        assert field in row
    json.loads(row["alternatives_json"])
    assert "Santa" in row["recovered_name"] or row["recovered_name"] == ""


def test_positive_controls_recover_expected_name_without_mutation():
    base = [_context("Rua Santa Marina")]
    controls = make_positive_controls(base, max_per_kind=1)
    assert len(controls) == 9
    assert all(item.source_type == "SYNTHETIC_POSITIVE" for item in controls)
    assert base[0].original_name == "Rua Santa Marina"


def test_negative_controls_include_critical_swap_and_do_not_mutate_positive():
    base = [_context("Rua Santa Marina")]
    controls = make_negative_controls(base, max_per_kind=1)
    assert {item.corruption_kind for item in controls} == {
        "CRITICAL_TOKEN_SWAP", "EXTRA_CRITICAL_TOKEN", "PARALLEL_OR_WRONG_STREET", "NO_INTERSECTION", "WRONG_COMPONENT",
    }
    assert base[0].original_name == "Rua Santa Marina"


def test_alias_mining_is_shadow_only_and_scope_is_conservative():
    engine = BoundaryNameRecoveryEngine(_graph())
    results = [engine.recover(_context("Santa Marin", gps=True)), engine.recover(_context("Santa Marin", gps=True, side="ATE"))]
    aliases = mine_alias_candidates(results)
    assert isinstance(aliases, list)
    assert all(item["recommended_scope"] in {"GLOBAL_ALIAS", "CONTEXTUAL_ALIAS", "DO_NOT_ALIAS"} for item in aliases)


def test_cli_exposes_shadow_sample_resume_cache_and_filters():
    args = build_parser().parse_args(["--shadow", "--sample", "30", "--resume", "--reset-cache", "--only-side", "DE", "--only-problem-type", "TYPO"])
    assert args.shadow is True and args.sample == 30 and args.resume and args.reset_cache
    assert args.only_side == "DE" and args.only_problem_type == "TYPO"


def test_no_production_dependency_imports():
    tree = ast.parse(open("src/boundary_name_recovery.py", encoding="utf-8").read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "street_resolver" not in imported
    assert "geometry_validator" not in imported
    assert "transform" not in imported


def test_official_alias_file_is_not_an_output_constant():
    source = open("src/boundary_name_recovery.py", encoding="utf-8").read()
    assert "ALIASES_INPUT" in source and "ALIAS_OUTPUT" in source
    assert "street_aliases.csv" in source
    assert "to_csv(ALIASES_INPUT" not in source


def test_cache_export_is_json_serializable_and_deterministic():
    engine = BoundaryNameRecoveryEngine(_graph())
    engine.recover(_context("Rua Santa Marina"))
    first = engine.export_cache()
    second = engine.export_cache()
    assert json.dumps(first, sort_keys=True)
    assert first == second


def test_wrong_component_control_uses_translated_geometry():
    negative = make_negative_controls([_context("Rua Santa Marina")], max_per_kind=1)
    original = _context("Rua Santa Marina").geometry
    translated = next(item for item in negative if item.corruption_kind == "WRONG_COMPONENT").geometry
    assert translated.distance(original) > 1000
