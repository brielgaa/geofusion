from __future__ import annotations

import hashlib
import json
from argparse import Namespace
from pathlib import Path

import pandas as pd
import pytest
from shapely.geometry import LineString

from src.consensus_evidence import (
    ConsensusEvidenceEngine,
    EvidenceRecord,
    GeometryEquivalenceConfig,
    _ablation,
    _negative_controls,
    _positive_controls,
    compare_geometry_candidates,
    dependency_graph,
    geometry_hash,
    run_shadow,
)


WKT = "LINESTRING (0 0, 10 0)"
NEAR_WKT = "LINESTRING (0.5 0, 10.5 0)"
OTHER_WKT = "LINESTRING (0 10, 10 10)"


def evidence(source: str, classification: str, *, wkt: str = WKT, group: str | None = None, **kwargs) -> EvidenceRecord:
    return EvidenceRecord(
        record_id="1", source=source, family=kwargs.pop("family", source.upper()),
        classification=classification, candidate_wkt=wkt,
        provenance=kwargs.pop("provenance", {"snapshot_id": "snapshot-1", "source_version": "test-v1"}),
        independent_group=group or source,
        topology_ok=kwargs.pop("topology_ok", True),
        component_ok=kwargs.pop("component_ok", True),
        candidate_count=kwargs.pop("candidate_count", 1),
        candidate_margin=kwargs.pop("candidate_margin", 1.0),
        **kwargs,
    )


def classify(*records: EvidenceRecord):
    return ConsensusEvidenceEngine().evaluate_case("1", list(records))


def test_geometry_equivalence_exact_near_different():
    assert compare_geometry_candidates(WKT, WKT) == "EXACT"
    assert compare_geometry_candidates(WKT, NEAR_WKT) == "NEAR_EQUIVALENT"
    assert compare_geometry_candidates(WKT, OTHER_WKT) == "DIFFERENT"


def test_geometry_hash_is_validator_compatible():
    assert geometry_hash(WKT) == hashlib.sha256(WKT.encode()).hexdigest()


def test_exact_same_geometry_two_independent_high_is_high():
    result = classify(
        evidence("geometry_validator", "VALIDATED_HIGH", family="GEOMETRY_VALIDATION"),
        evidence("boundary_audit", "BOUNDARIES_VALIDATED_HIGH", family="BOUNDARY_GEOMETRY"),
    )
    assert result.consensus_class == "CONSENSUS_HIGH"
    assert result.independent_evidence_count == 2
    assert result.reason == "TWO_INDEPENDENT_HIGH_SOURCES_SAME_GEOMETRY"


def test_high_medium_is_medium():
    result = classify(
        evidence("geometry_validator", "VALIDATED_HIGH", family="GEOMETRY_VALIDATION"),
        evidence("boundary_audit", "BOUNDARIES_VALIDATED_MEDIUM", family="BOUNDARY_GEOMETRY"),
    )
    assert result.consensus_class == "CONSENSUS_MEDIUM"
    assert result.reason == "VALIDATOR_HIGH_BOUNDARY_MEDIUM_NO_CONFLICT"


def test_same_classification_different_geometry_conflicts():
    result = classify(
        evidence("geometry_validator", "VALIDATED_HIGH", family="GEOMETRY_VALIDATION"),
        evidence("boundary_audit", "BOUNDARIES_VALIDATED_HIGH", family="BOUNDARY_GEOMETRY", wkt=OTHER_WKT),
    )
    assert result.consensus_class == "CONFLICTING_EVIDENCE"
    assert "CANDIDATE_GEOMETRY_MISMATCH" in result.hard_failures
    assert result.reason == "CANDIDATE_HASH_MISMATCH"


def test_dependent_sources_do_not_count_twice():
    result = classify(
        evidence("boundary_audit", "BOUNDARIES_VALIDATED_HIGH", family="BOUNDARY_GEOMETRY", group="BOUNDARY_CHAIN"),
        evidence("name_recovery", "NAME_RECOVERED_HIGH", family="BOUNDARY_LEXICAL", group="BOUNDARY_CHAIN", wkt=""),
    )
    assert result.independent_evidence_count == 1
    assert result.consensus_class == "INSUFFICIENT_EVIDENCE"


def test_snapshot_conflict_blocks_high():
    result = classify(
        evidence("geometry_validator", "VALIDATED_HIGH", family="GEOMETRY_VALIDATION", provenance={"snapshot_id": "a", "source_version": "v1"}),
        evidence("boundary_audit", "BOUNDARIES_VALIDATED_HIGH", family="BOUNDARY_GEOMETRY", provenance={"snapshot_id": "b", "source_version": "v1"}),
    )
    assert result.snapshot_status == "SNAPSHOT_CONFLICT"
    assert result.consensus_class == "CONFLICTING_EVIDENCE"
    assert "SNAPSHOT_CONFLICT" in result.hard_failures


def test_same_and_different_codlog():
    same = classify(
        evidence("geometry_validator", "VALIDATED_HIGH", family="GEOMETRY_VALIDATION", codlog="123"),
        evidence("boundary_audit", "BOUNDARIES_VALIDATED_HIGH", family="BOUNDARY_GEOMETRY", codlog="123"),
    )
    different = classify(
        evidence("geometry_validator", "VALIDATED_HIGH", family="GEOMETRY_VALIDATION", codlog="123"),
        evidence("boundary_audit", "BOUNDARIES_VALIDATED_HIGH", family="BOUNDARY_GEOMETRY", codlog="999"),
    )
    assert same.codlog_ok is True
    assert different.codlog_ok is False
    assert "CODLOG_DIVERGENCE" in different.hard_failures


def test_component_and_topology_failures_block_consensus():
    component = classify(
        evidence("geometry_validator", "VALIDATED_HIGH", family="GEOMETRY_VALIDATION", component_ok=False),
        evidence("boundary_audit", "BOUNDARIES_VALIDATED_HIGH", family="BOUNDARY_GEOMETRY"),
    )
    topology = classify(
        evidence("geometry_validator", "VALIDATED_HIGH", family="GEOMETRY_VALIDATION", topology_ok=False),
        evidence("boundary_audit", "BOUNDARIES_VALIDATED_HIGH", family="BOUNDARY_GEOMETRY"),
    )
    assert component.consensus_class == "CONFLICTING_EVIDENCE"
    assert "WRONG_COMPONENT" in component.hard_failures
    assert "TOPOLOGY_CONFLICT" in topology.hard_failures


def test_candidate_competition_blocks_high():
    result = classify(
        evidence("geometry_validator", "VALIDATED_HIGH", family="GEOMETRY_VALIDATION", candidate_count=2, candidate_margin=0.01),
        evidence("boundary_audit", "BOUNDARIES_VALIDATED_HIGH", family="BOUNDARY_GEOMETRY"),
    )
    assert result.candidate_competition is True
    assert result.consensus_class != "CONSENSUS_HIGH"
    assert "COMPETING_CANDIDATE" in result.hard_failures


def test_human_approve_is_read_only_strong_evidence():
    result = classify(
        evidence("geometry_validator", "VALIDATED_HIGH", family="GEOMETRY_VALIDATION"),
        evidence("human_review", "APPROVED", family="HUMAN_REVIEW"),
    )
    assert result.human_review_class == "APPROVED"
    assert result.consensus_class == "CONSENSUS_HIGH"


def test_human_reject_conflicts_and_missing_review_is_unreviewed():
    rejected = classify(
        evidence("geometry_validator", "VALIDATED_HIGH", family="GEOMETRY_VALIDATION"),
        evidence("human_review", "REJECTED", family="HUMAN_REVIEW", wkt=""),
    )
    missing = classify(evidence("geometry_validator", "VALIDATED_HIGH", family="GEOMETRY_VALIDATION"))
    assert rejected.consensus_class == "CONFLICTING_EVIDENCE"
    assert missing.human_review_class == "UNREVIEWED"


def test_insufficient_and_rejected_consensus():
    insufficient = classify(evidence("geometry_validator", "VALIDATED_MEDIUM", family="GEOMETRY_VALIDATION"))
    rejected = classify(
        evidence("geometry_validator", "REJECTED", family="GEOMETRY_VALIDATION", wkt=""),
        evidence("boundary_audit", "KEEP_CONTRADICTION", family="BOUNDARY_GEOMETRY", wkt=""),
    )
    assert insufficient.consensus_class == "CONSENSUS_MEDIUM" or insufficient.consensus_class == "INSUFFICIENT_EVIDENCE"
    assert rejected.consensus_class == "REJECTED_BY_CONSENSUS"


def test_score_determinism_and_unique_ids():
    records = [
        evidence("geometry_validator", "VALIDATED_HIGH", family="GEOMETRY_VALIDATION"),
        evidence("boundary_audit", "BOUNDARIES_VALIDATED_HIGH", family="BOUNDARY_GEOMETRY"),
    ]
    engine = ConsensusEvidenceEngine()
    left = engine.evaluate(records + records)
    right = engine.evaluate(records)
    assert len(left) == len(right) == 1
    assert left[0].consensus_score == right[0].consensus_score
    assert left[0].to_row()["id"] == "1"


def test_dependency_graph_is_explicit_and_name_recovery_is_dependent():
    graph = dependency_graph()
    name = next(item for item in graph if item["source"] == "name_recovery")
    boundary = next(item for item in graph if item["source"] == "boundary_audit")
    assert "boundary_audit" in name["depends_on"]
    assert name["independent_group"] == boundary["independent_group"]


def test_positive_and_negative_control_helpers():
    records = [
        evidence("geometry_validator", "VALIDATED_HIGH", family="GEOMETRY_VALIDATION"),
        evidence("boundary_audit", "BOUNDARIES_VALIDATED_HIGH", family="BOUNDARY_GEOMETRY"),
    ]
    positive = _positive_controls({"1": LineString([(0, 0), (10, 0)])}, [classify(*records)])
    negative = _negative_controls(records, {"1": LineString([(0, 0), (10, 0)])}, GeometryEquivalenceConfig())
    assert positive["official_geometries"] == 1
    assert negative["synthetic_cases"] == 4


def test_ablation_reports_source_removal():
    records = [
        evidence("geometry_validator", "VALIDATED_HIGH", family="GEOMETRY_VALIDATION"),
        evidence("boundary_audit", "BOUNDARIES_VALIDATED_HIGH", family="BOUNDARY_GEOMETRY"),
    ]
    report = _ablation(records, set(), GeometryEquivalenceConfig())
    assert set(report) == {"all", "without_boundary", "without_name_recovery", "without_validator", "without_topology"}
    assert report["all"]["total"] == 1


def _write_fixture(root: Path) -> None:
    processed = root / "data" / "processed"
    processed.mkdir(parents=True)
    pd.DataFrame([{"id": "1", "geometry_wkt": WKT, "geometry_confidence": "RECONSTRUCTED_HIGH", "geometry_score": "90", "candidate_count": "1", "component_status": "SAME_COMPONENT", "topology_status": "TOPOLOGICAL", "warnings": "", "shadow_version": "fixture-v1"}]).to_csv(processed / "route_geometry_quality_shadow.csv", index=False)
    pd.DataFrame([{"id": "1", "validation_class": "VALIDATED_HIGH", "validation_score_independent": "90", "geometry_wkt": WKT, "geometry_hash_sha256": geometry_hash(WKT), "geometry_valid": "True", "component_status": "SAME_COMPONENT", "topology_status": "SAME_COMPONENT", "hard_failures": "", "warnings": "", "validator_version": "fixture-v1"}]).to_csv(processed / "geometry_validation_shadow.csv", index=False)
    pd.DataFrame([{"id": "1", "recommendation": "BOUNDARIES_VALIDATED_HIGH", "boundary_validation_score": "90", "candidate_geometry_wkt": WKT, "warnings": ""}]).to_csv(processed / "boundary_contradiction_audit.csv", index=False)
    pd.DataFrame([{"id": "1", "boundary_side": "DE", "classification": "NAME_RECOVERED_HIGH", "recovered_codlog": ""}]).to_csv(processed / "boundary_name_recovery.csv", index=False)
    pd.DataFrame([{"id": "1", "decision": "APROVAR_GEOMETRIA", "approved": "True", "manual_geometry_wkt": WKT, "geometry_score": "90"}]).to_csv(processed / "route_geometry_human_review.csv", index=False)
    pd.DataFrame([{"id": "1", "codlog_recomendado": "123", "confianca": "HIGH", "score_final": "90", "requer_revisao": "False"}]).to_csv(processed / "street_resolution_audit.csv", index=False)
    official_path = json.dumps([[-46.0, -23.0], [-45.9999, -23.0]])
    pd.DataFrame([{"id": "1", "path": official_path}]).to_csv(processed / "recape_clean.csv", index=False)


def test_run_shadow_report_json_population_and_no_official_mutation(tmp_path):
    _write_fixture(tmp_path)
    official = tmp_path / "data" / "processed" / "recape_clean.csv"
    before = hashlib.sha256(official.read_bytes()).hexdigest()
    report = run_shadow(Namespace(sample=None, only_id=[], only_class=[], reset_cache=False, resume=False), tmp_path)
    after = hashlib.sha256(official.read_bytes()).hexdigest()
    assert before == after
    assert report["official_promotions_applied"] == 0
    assert report["population"]["population_total"] == 1
    assert (tmp_path / "data" / "processed" / "consensus_evidence_shadow.csv").exists()
    loaded = json.loads((tmp_path / "data" / "processed" / "consensus_evidence_report.json").read_text(encoding="utf-8"))
    assert loaded["official_promotions_applied"] == 0
    assert loaded["protected_hashes_unchanged"] is True
    output = pd.read_csv(tmp_path / "data" / "processed" / "consensus_evidence_shadow.csv", dtype=str)
    assert output["id"].is_unique


@pytest.mark.parametrize("bad_wkt", ["", "NOT WKT", "POINT (0 0)"])
def test_invalid_or_missing_wkt_is_not_high(bad_wkt):
    result = classify(
        evidence("geometry_validator", "VALIDATED_HIGH", family="GEOMETRY_VALIDATION", wkt=bad_wkt),
        evidence("boundary_audit", "BOUNDARIES_VALIDATED_HIGH", family="BOUNDARY_GEOMETRY", wkt=bad_wkt),
    )
    assert result.consensus_class != "CONSENSUS_HIGH"
