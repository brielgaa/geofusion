from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from route_geometry_estimated_pareto import (
    EXPECTED_ESTIMATED,
    build_pareto,
    calculate_promotion_potential,
    classify_root_cause,
    derive_secondary_causes,
    load_effective_estimated_cases,
    run_analysis,
    simulate_scenarios,
)


def _case(**overrides):
    row = {
        "id": "case-1",
        "root_cause": "AUSENCIA_DE_INTERSECAO",
        "root_cause_primary": "AUSENCIA_DE_INTERSECAO",
        "root_causes": "AUSENCIA_DE_INTERSECAO | MULTIPLOS_SEGMENTOS_POSSIVEIS",
        "strategy_selected": "NEAREST_SEGMENT_ESTIMATED",
        "de_status": "EXATO",
        "ate_status": "FUZZY",
        "score": 95.0,
        "extension_deviation": 5.0,
        "gps_distance": 2.0,
        "component_count": 1.0,
        "candidate_count": 1.0,
        "margin_top2": 100.0,
        "same_intersection_count_distinct": 2.0,
        "topology_status": "SAME_COMPONENT",
        "component_status": "SAME_COMPONENT",
        "loop_detected": False,
        "loop": False,
        "warnings": "",
        "same_warnings": "",
        "ambiguous_candidates": False,
        "with_de": 1,
        "with_ate": 1,
        "with_both": 1,
        "with_none": 0,
        "multiple_components": 0,
        "critical_warning_count": 0,
        "review_decision": "",
        "review_approved": False,
        "promotion_potential": "VERY_HIGH",
    }
    row.update(overrides)
    return row


def test_classifies_existing_primary_cause():
    assert classify_root_cause({"root_cause_primary": "PROBLEMA_TOPOLOGICO"}) == "PROBLEMA_TOPOLOGICO"


def test_derives_fallback_cause_from_missing_boundaries():
    assert classify_root_cause({"de_status": "UNRESOLVED", "ate_status": "NAO_RESOLVIDA"}) == "SEM_DE_E_ATE"


def test_derives_multiple_secondary_causes():
    row = _case(
        strategy_selected="GPS_SNAP_LINEAR_GROWTH_FORWARD",
        component_count=3,
        candidate_count=4,
        loop=True,
        loop_detected=True,
        warnings="LOOP_DETECTADO | DESVIO_EXTENSAO_ACIMA_50_PCT",
        extension_deviation=75,
    )
    causes = derive_secondary_causes(row, "AUSENCIA_DE_INTERSECAO")
    assert {"GPS_LINEAR_GROWTH", "MULTIPLE_COMPONENTS", "MULTIPLE_EQUIVALENT_CANDIDATES", "HIGH_EXTENSION_DEVIATION", "LOOP_WARNING"}.issubset(causes)


def test_promotion_potential_very_high_for_strong_evidence():
    assert calculate_promotion_potential(_case()) == "VERY_HIGH"


def test_promotion_potential_drops_for_nulls_components_loops_and_warnings():
    row = _case(
        score=None,
        extension_deviation=None,
        gps_distance=None,
        component_count=4,
        loop=True,
        warnings="LOOP_DETECTADO | DESVIO_EXTENSAO_ACIMA_50_PCT",
        candidate_count=8,
        same_intersection_count_distinct=0,
    )
    assert calculate_promotion_potential(row) in {"LOW", "VERY_LOW"}


def test_pareto_is_descending_and_cumulative():
    cases = pd.DataFrame([
        _case(id="a", root_cause="A", promotion_potential="HIGH"),
        _case(id="b", root_cause="A", promotion_potential="LOW"),
        _case(id="c", root_cause="B", promotion_potential="MEDIUM"),
    ])
    result = build_pareto(cases)
    assert result.iloc[0]["root_cause"] == "A"
    assert result.iloc[0]["count"] == 2
    assert result.iloc[-1]["cumulative_percentage"] == 100.0


def test_scenario_conservative_uses_only_very_high():
    cases = pd.DataFrame([
        _case(id="a", promotion_potential="VERY_HIGH"),
        _case(id="b", promotion_potential="HIGH"),
        _case(id="c", promotion_potential="MEDIUM"),
    ])
    result = simulate_scenarios(cases, 50.0, 1000)
    assert result["scenarios"]["CONSERVADOR"]["potentially_promoted"] == 0
    assert result["scenarios"]["CONSERVADOR"]["estimated_remaining"] == 3


def test_scenario_moderate_adds_high():
    cases = pd.DataFrame([
        _case(id="a", promotion_potential="VERY_HIGH"),
        _case(id="b", promotion_potential="HIGH"),
        _case(id="c", promotion_potential="HIGH"),
    ])
    result = simulate_scenarios(cases, 50.0, 100)
    assert result["scenarios"]["MODERADO"]["potentially_promoted"] == 1
    assert result["scenarios"]["MODERADO"]["estimated_remaining"] == 2


def test_scenario_aggressive_adds_part_of_medium():
    cases = pd.DataFrame([_case(id=str(i), promotion_potential="MEDIUM") for i in range(4)])
    result = simulate_scenarios(cases, 50.0, 100)
    assert result["scenarios"]["AGRESSIVO"]["potentially_promoted"] == 2


@pytest.mark.private_data
def test_actual_population_reconciles_same_transversal_promotions():
    cases, reconciliation = load_effective_estimated_cases()
    assert len(cases) == EXPECTED_ESTIMATED
    assert reconciliation["quality_shadow_estimated"] == 2121
    assert reconciliation["same_transversal_promoted_from_estimated"] == 42
    assert reconciliation["same_transversal_promoted_high"] == 32
    assert reconciliation["same_transversal_promoted_medium"] == 10


@pytest.mark.private_data
def test_human_review_is_joined_and_small_sample_is_visible():
    cases, _ = load_effective_estimated_cases()
    reviewed = cases[cases["review_decision"].fillna("").ne("")]
    assert len(reviewed) == 2
    assert int(reviewed["review_approved"].map(lambda value: str(value).lower() == "true").sum()) == 1


@pytest.mark.private_data
def test_analysis_outputs_have_one_line_per_estimated_and_expected_columns(tmp_path):
    output_report = tmp_path / "report.json"
    output_pareto = tmp_path / "pareto.csv"
    output_cases = tmp_path / "cases.csv"
    report = run_analysis(pareto_path=output_pareto, cases_path=output_cases, report_path=output_report)
    cases = pd.read_csv(output_cases, encoding="utf-8-sig", dtype=str)
    pareto = pd.read_csv(output_pareto, encoding="utf-8-sig", dtype=str)
    assert report["total_estimated"] == EXPECTED_ESTIMATED
    assert len(cases) == EXPECTED_ESTIMATED
    assert set(["id", "strategy", "root_cause", "secondary_causes", "promotion_potential", "recommended_next_heuristic"]).issubset(cases.columns)
    assert set(["root_cause", "count", "percentage", "cumulative_percentage", "human_approval_rate"]).issubset(pareto.columns)
    assert json.loads(output_report.read_text(encoding="utf-8"))["official_outputs_changed"] is False


@pytest.mark.private_data
def test_analysis_is_deterministic(tmp_path):
    first = run_analysis(
        pareto_path=tmp_path / "one_pareto.csv",
        cases_path=tmp_path / "one_cases.csv",
        report_path=tmp_path / "one_report.json",
    )
    second = run_analysis(
        pareto_path=tmp_path / "two_pareto.csv",
        cases_path=tmp_path / "two_cases.csv",
        report_path=tmp_path / "two_report.json",
    )
    assert first["total_estimated"] == second["total_estimated"]
    assert first["promotion_potential_distribution"] == second["promotion_potential_distribution"]
    assert (tmp_path / "one_pareto.csv").read_bytes() == (tmp_path / "two_pareto.csv").read_bytes()
    assert (tmp_path / "one_cases.csv").read_bytes() == (tmp_path / "two_cases.csv").read_bytes()
