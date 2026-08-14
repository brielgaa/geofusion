from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd
from shapely.geometry import LineString
from shapely.ops import transform

from src.consensus_calibration import (
    _available_families,
    _candidate_match_status,
    _classify,
    _conflict_category,
    _human_comparison,
    _reachability,
    _synthetic_bundle,
    _synthetic_positive_audit,
    audit_positive_controls,
    run_audit,
)
from src.consensus_evidence import (
    EvidenceRecord,
    GeometryEquivalenceConfig,
    WGS84_TO_METRIC,
    dependency_graph,
    geometry_hash,
)


WKT = "LINESTRING (0 0, 10 0)"
OTHER_WKT = "LINESTRING (0 10, 10 10)"


def record(source: str, classification: str, *, wkt: str = WKT, group: str | None = None, **kwargs) -> EvidenceRecord:
    return EvidenceRecord(
        record_id="1",
        source=source,
        family=kwargs.pop("family", source.upper()),
        classification=classification,
        candidate_wkt=wkt,
        provenance=kwargs.pop("provenance", {"snapshot_id": "test-snapshot", "source_version": "test-v1"}),
        independent_group=group or source,
        topology_ok=kwargs.pop("topology_ok", True),
        component_ok=kwargs.pop("component_ok", True),
        candidate_count=kwargs.pop("candidate_count", 1),
        candidate_margin=kwargs.pop("candidate_margin", 1.0),
        **kwargs,
    )


def test_positive_not_evaluable_missing_second_family():
    bundle = [record("geometry_validator", "VALIDATED_HIGH", family="GEOMETRY_VALIDATION")]
    assert len(_available_families(bundle)) == 1
    assert _classify("1", bundle).consensus_class == "CONSENSUS_MEDIUM"


def test_positive_evaluable_accepted():
    result = _classify("1", [record("geometry_validator", "VALIDATED_HIGH", family="GEOMETRY_VALIDATION"), record("boundary_audit", "BOUNDARIES_VALIDATED_HIGH", family="BOUNDARY_GEOMETRY")])
    assert result.consensus_class == "CONSENSUS_HIGH"


def test_positive_candidate_does_not_match_official():
    official = type("Geometry", (), {"wkt": WKT})()
    candidate = record("geometry_validator", "VALIDATED_HIGH", wkt=OTHER_WKT)
    status, comparison = _candidate_match_status(official, candidate, GeometryEquivalenceConfig())
    assert status == "DIFFERENT_FROM_OFFICIAL"
    assert comparison == "DIFFERENT"


def test_missing_evidence_is_not_conflict():
    result = _classify("1", [record("geometry_validator", "VALIDATED_MEDIUM")])
    assert result.consensus_class == "CONSENSUS_MEDIUM"
    assert "CANDIDATE_GEOMETRY_MISMATCH" not in result.hard_failures


def test_actual_disagreement_is_conflict():
    result = _classify("1", [record("geometry_validator", "VALIDATED_HIGH"), record("boundary_audit", "BOUNDARIES_VALIDATED_HIGH", wkt=OTHER_WKT)])
    assert result.consensus_class == "CONFLICTING_EVIDENCE"
    assert _conflict_category({"hard_failures": "CANDIDATE_GEOMETRY_MISMATCH", "conflicting_sources_json": "[\"boundary_audit\"]"}) == "GEOMETRY_MISMATCH"


def test_missing_codlog_is_not_mismatch():
    result = _classify("1", [record("geometry_validator", "VALIDATED_HIGH"), record("boundary_audit", "BOUNDARIES_VALIDATED_HIGH")])
    assert result.codlog_ok is None
    assert "CODLOG_DIVERGENCE" not in result.hard_failures


def test_missing_component_is_not_mismatch():
    result = _classify("1", [record("geometry_validator", "VALIDATED_HIGH", component_ok=None), record("boundary_audit", "BOUNDARIES_VALIDATED_HIGH")])
    assert result.component_ok is None
    assert "WRONG_COMPONENT" not in result.hard_failures
    assert result.consensus_class == "INSUFFICIENT_EVIDENCE"


def test_synthetic_complete_positive_reaches_high():
    report = _synthetic_positive_audit({"1": type("Geometry", (), {"wkt": WKT})()}, GeometryEquivalenceConfig(), 1)
    assert report["complete_reaches_high"] is True


def test_complete_positive_reaches_medium_when_one_family_removed():
    report = _synthetic_positive_audit({"1": type("Geometry", (), {"wkt": WKT})()}, GeometryEquivalenceConfig(), 1)
    assert report["variants"]["minus_validator"]["class_counts"] == {"CONSENSUS_MEDIUM": 1}


def test_negative_evaluable_rejected():
    result = _classify("1", [record("geometry_validator", "REJECTED", wkt=""), record("boundary_audit", "KEEP_CONTRADICTION", wkt="")])
    assert result.consensus_class == "REJECTED_BY_CONSENSUS"


def test_negative_not_evaluable_separated_by_missing_family():
    result = _classify("1", [record("geometry_validator", "REJECTED", wkt="")])
    assert result.consensus_class != "REJECTED_BY_CONSENSUS"
    assert len(_available_families([record("geometry_validator", "REJECTED", wkt="")])) == 1


def test_snapshot_conflict():
    result = _classify("1", [record("geometry_validator", "VALIDATED_HIGH", provenance={"snapshot_id": "a", "source_version": "v1"}), record("boundary_audit", "BOUNDARIES_VALIDATED_HIGH", provenance={"snapshot_id": "b", "source_version": "v1"})])
    assert result.snapshot_status == "SNAPSHOT_CONFLICT"
    assert result.consensus_class == "CONFLICTING_EVIDENCE"


def test_geometry_mismatch():
    result = _classify("1", [record("geometry_validator", "VALIDATED_HIGH"), record("boundary_audit", "BOUNDARIES_VALIDATED_HIGH", wkt=OTHER_WKT)])
    assert "CANDIDATE_GEOMETRY_MISMATCH" in result.hard_failures


def test_family_count():
    bundle = [record("geometry_validator", "VALIDATED_HIGH", family="GEOMETRY_VALIDATION"), record("boundary_audit", "BOUNDARIES_VALIDATED_HIGH", family="BOUNDARY_GEOMETRY"), record("name_recovery", "NAME_RECOVERED_HIGH", family="BOUNDARY_LEXICAL", group="BOUNDARY_CHAIN", wkt="")]
    assert _available_families(bundle) == {"GEOMETRY_VALIDATION", "BOUNDARY_CHAIN"}


def test_dependency_graph():
    graph = dependency_graph()
    assert next(item for item in graph if item["source"] == "name_recovery")["independent_group"] == "BOUNDARY_CHAIN"


def test_reachability_all_classes():
    report = _reachability(GeometryEquivalenceConfig())
    assert report["all_requested_classes_reachable"] is True
    assert set(report["cases"].values()) == {"CONSENSUS_HIGH", "CONSENSUS_MEDIUM", "INSUFFICIENT_EVIDENCE", "CONFLICTING_EVIDENCE", "REJECTED_BY_CONSENSUS"}


def test_high_reachable():
    assert _reachability(GeometryEquivalenceConfig())["cases"]["CONSENSUS_HIGH"] == "CONSENSUS_HIGH"


def test_medium_reachable():
    assert _reachability(GeometryEquivalenceConfig())["cases"]["CONSENSUS_MEDIUM"] == "CONSENSUS_MEDIUM"


def test_insufficient_reachable():
    assert _reachability(GeometryEquivalenceConfig())["cases"]["INSUFFICIENT_EVIDENCE"] == "INSUFFICIENT_EVIDENCE"


def test_conflicting_reachable():
    assert _reachability(GeometryEquivalenceConfig())["cases"]["CONFLICTING_EVIDENCE"] == "CONFLICTING_EVIDENCE"


def test_rejected_reachable():
    assert _reachability(GeometryEquivalenceConfig())["cases"]["REJECTED_BY_CONSENSUS"] == "REJECTED_BY_CONSENSUS"


def test_human_approve_comparison(tmp_path):
    processed = tmp_path / "data" / "processed"
    processed.mkdir(parents=True)
    pd.DataFrame([{"id": "1", "decision": "APROVAR_GEOMETRIA"}]).to_csv(processed / "route_geometry_human_review.csv", index=False)
    consensus = pd.DataFrame([{"id": "1", "consensus_class": "CONSENSUS_HIGH"}])
    assert _human_comparison(tmp_path, consensus)["approved_to_consensus"] == {"CONSENSUS_HIGH": 1}


def test_human_reject_comparison(tmp_path):
    processed = tmp_path / "data" / "processed"
    processed.mkdir(parents=True)
    pd.DataFrame([{"id": "1", "decision": "REJEITAR_GEOMETRIA"}]).to_csv(processed / "route_geometry_human_review.csv", index=False)
    consensus = pd.DataFrame([{"id": "1", "consensus_class": "REJECTED_BY_CONSENSUS"}])
    assert _human_comparison(tmp_path, consensus)["rejected_to_consensus"] == {"REJECTED_BY_CONSENSUS": 1}


def _fixture(root: Path) -> tuple[str, str]:
    processed = root / "data" / "processed"
    processed.mkdir(parents=True)
    metric = transform(WGS84_TO_METRIC.transform, LineString([(-46.0, -23.0), (-45.9999, -23.0)]))
    wkt = metric.wkt
    version = "fixture-v1"
    pd.DataFrame([{"id": "1", "path": json.dumps([[-46.0, -23.0], [-45.9999, -23.0]])}]).to_csv(processed / "recape_clean.csv", index=False)
    pd.DataFrame([{"id": "1", "geometry_wkt": wkt, "geometry_confidence": "RECONSTRUCTED_HIGH", "candidate_count": "1", "topology_status": "VALID", "component_status": "SAME_COMPONENT", "extension_deviation_pct": "1", "shadow_version": version, "snapshot_id": "fixture-snapshot"}]).to_csv(processed / "route_geometry_quality_shadow.csv", index=False)
    pd.DataFrame([{"id": "1", "validation_class": "VALIDATED_HIGH", "geometry_wkt": wkt, "geometry_valid": "True", "topology_status": "VALID", "component_status": "SAME_COMPONENT", "extension_deviation_pct": "1", "candidate_count": "1", "top2_margin": "1", "validator_version": version, "snapshot_id": "fixture-snapshot"}]).to_csv(processed / "geometry_validation_shadow.csv", index=False)
    pd.DataFrame([{"id": "1", "recommendation": "BOUNDARIES_VALIDATED_HIGH", "candidate_geometry_wkt": wkt, "source_audit_version": version, "snapshot_id": "fixture-snapshot"}]).to_csv(processed / "boundary_contradiction_audit.csv", index=False)
    pd.DataFrame([{"id": "1", "classification": "NAME_RECOVERED_HIGH", "source_audit_version": version, "snapshot_id": "fixture-snapshot"}]).to_csv(processed / "boundary_name_recovery.csv", index=False)
    pd.DataFrame([{"id": "1", "decision": "APROVAR_GEOMETRIA", "manual_geometry_wkt": wkt, "source_audit_version": version, "snapshot_id": "fixture-snapshot"}]).to_csv(processed / "route_geometry_human_review.csv", index=False)
    pd.DataFrame([{"id": "1", "consensus_class": "CONSENSUS_HIGH", "snapshot_status": "SNAPSHOT_ALIGNED", "candidate_competition": "False", "independent_families_json": json.dumps([{ "independent_group": "BOUNDARY_CHAIN" }, { "independent_group": "GEOMETRY_VALIDATION" }]), "hard_failures": "", "warnings": ""}]).to_csv(processed / "consensus_evidence_shadow.csv", index=False)
    return wkt, hashlib.sha256((processed / "recape_clean.csv").read_bytes()).hexdigest()


def test_report_json_and_unique_ids(tmp_path):
    _fixture(tmp_path)
    report = run_audit(tmp_path, synthetic_limit=1, negative_limit=1)
    assert (tmp_path / "data" / "processed" / "consensus_calibration_report.json").exists()
    loaded = json.loads((tmp_path / "data" / "processed" / "consensus_calibration_report.json").read_text(encoding="utf-8"))
    assert loaded["official_promotions_applied"] == 0
    output = pd.read_csv(tmp_path / "data" / "processed" / "consensus_positive_control_audit.csv", dtype=str)
    assert output["id"].is_unique
    assert report["positive_evaluable_metrics"]["positive_total"] == 1


def test_protected_hash_unchanged(tmp_path):
    _fixture(tmp_path)
    before = hashlib.sha256((tmp_path / "data" / "processed" / "recape_clean.csv").read_bytes()).hexdigest()
    run_audit(tmp_path, synthetic_limit=1, negative_limit=1)
    after = hashlib.sha256((tmp_path / "data" / "processed" / "recape_clean.csv").read_bytes()).hexdigest()
    assert before == after
    assert geometry_hash(WKT) != geometry_hash(OTHER_WKT)
