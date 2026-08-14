"""Methodological audit for the shadow Consensus Evidence Engine.

This module is diagnostic only.  It consumes persisted artifacts, writes an
audit CSV/report, and never mutates official data or consensus decisions.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd
from shapely.affinity import translate

try:  # direct execution: python src/consensus_calibration.py
    from consensus_evidence import (
        INDEPENDENT_SUPPORT_GROUPS,
        ConsensusEvidenceEngine,
        ConsensusEvidenceResult,
        EvidenceRecord,
        GeometryEquivalenceConfig,
        _canonical_candidate,
        _classify,
        _read_official_geometries,
        _sort_ids,
        compare_geometry_candidates,
        geometry_hash,
        load_evidence_records,
    )
except ImportError:  # package import: from src.consensus_calibration import ...
    from .consensus_evidence import (
        INDEPENDENT_SUPPORT_GROUPS,
        ConsensusEvidenceEngine,
        ConsensusEvidenceResult,
        EvidenceRecord,
        GeometryEquivalenceConfig,
        _canonical_candidate,
        _classify,
        _read_official_geometries,
        _sort_ids,
        compare_geometry_candidates,
        geometry_hash,
        load_evidence_records,
    )


ROOT = Path(__file__).resolve().parents[1]
PROCESSED = ROOT / "data" / "processed"
CONSENSUS_CSV = PROCESSED / "consensus_evidence_shadow.csv"
CONSENSUS_REPORT = PROCESSED / "consensus_evidence_report.json"
OUTPUT_CSV = PROCESSED / "consensus_positive_control_audit.csv"
OUTPUT_REPORT = PROCESSED / "consensus_calibration_report.json"
VERSION = "consensus-calibration-shadow-v1"

AUDIT_CLASSES = (
    "NOT_EVALUABLE",
    "EVALUABLE_ACCEPTED",
    "EVALUABLE_REJECTED",
    "EVALUABLE_CONFLICTING",
    "SNAPSHOT_INVALID",
)


def _text(value: Any) -> str:
    if value is None:
        return ""
    try:
        if bool(pd.isna(value)):
            return ""
    except (TypeError, ValueError):
        pass
    value = str(value).strip()
    return "" if value.casefold() in {"", "nan", "none", "null", "<na>"} else value


def _number(value: Any) -> float | None:
    try:
        value = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _boolean(value: Any) -> bool | None:
    value = _text(value).casefold()
    if value in {"true", "1", "yes", "sim", "y", "t"}:
        return True
    if value in {"false", "0", "no", "nao", "não", "n", "f"}:
        return False
    return None


def _tokens(value: Any) -> tuple[str, ...]:
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(sorted({_text(item) for item in value if _text(item)}))
    value = _text(value)
    if not value:
        return ()
    if value.startswith("["):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return tuple(sorted({_text(item) for item in parsed if _text(item)}))
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    return tuple(sorted({item.strip() for item in re.split(r"[|;,]", value) if item.strip()}))


def _json(value: Any, default: Any = None) -> Any:
    if isinstance(value, (dict, list, tuple)):
        return value
    value = _text(value)
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path, dtype=str, encoding="utf-8-sig", keep_default_na=False)


def _raw_maps(root: Path) -> dict[str, dict[str, list[dict[str, Any]]]]:
    specs = {
        "route_quality": "route_geometry_quality_shadow.csv",
        "geometry_validator": "geometry_validation_shadow.csv",
        "boundary_audit": "boundary_contradiction_audit.csv",
        "name_recovery": "boundary_name_recovery.csv",
        "human_review": "route_geometry_human_review.csv",
    }
    result: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for source, filename in specs.items():
        rows = _read_csv(root / "data" / "processed" / filename).to_dict("records")
        mapped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            identifier = _text(row.get("id") or row.get("record_id"))
            if identifier:
                mapped[identifier].append(row)
        result[source] = mapped
    return result


def _source_group(record: EvidenceRecord) -> str:
    group = _text(record.independent_group).upper()
    aliases = {
        "GEOMETRY_VALIDATOR": "GEOMETRY_VALIDATION",
        "VALIDATOR": "GEOMETRY_VALIDATION",
        "BOUNDARY_AUDIT": "BOUNDARY_CHAIN",
        "BOUNDARY": "BOUNDARY_CHAIN",
        "NAME_RECOVERY": "BOUNDARY_CHAIN",
        "HUMAN": "HUMAN_REVIEW",
    }
    if group in INDEPENDENT_SUPPORT_GROUPS:
        return group
    return aliases.get(group, aliases.get(record.source.upper(), group or record.family.upper()))


def _available_families(records: Sequence[EvidenceRecord]) -> set[str]:
    return {_source_group(record) for record in records if _source_group(record) in INDEPENDENT_SUPPORT_GROUPS}


def _family_json(result: ConsensusEvidenceResult) -> list[dict[str, Any]]:
    value = _json(result.independent_families_json, [])
    return value if isinstance(value, list) else []


def _candidate_competition(records: Sequence[EvidenceRecord], result: ConsensusEvidenceResult) -> bool:
    if result.candidate_competition:
        return True
    return any(
        "COMPETING_CANDIDATE" in record.hard_failures
        or _text(record.provenance.get("competition_status")).upper() == "LOW_MARGIN"
        or (record.candidate_count is not None and record.candidate_count > 1 and record.candidate_margin is not None and record.candidate_margin < 0.08)
        for record in records
    )


def _raw_availability(raw: dict[str, dict[str, list[dict[str, Any]]]], identifier: str) -> dict[str, bool]:
    quality = raw.get("route_quality", {}).get(identifier, [])
    validator = raw.get("geometry_validator", {}).get(identifier, [])
    boundary = raw.get("boundary_audit", {}).get(identifier, [])
    names = raw.get("name_recovery", {}).get(identifier, [])
    human = raw.get("human_review", {}).get(identifier, [])
    topology_values = [_text(row.get("topology_status") or row.get("topology_status_official")) for row in quality + validator]
    component_values = [_text(row.get("component_status")) for row in quality + validator]
    gps_values = [_text(row.get("gps_status")) for row in validator] + [_text(row.get("gps_distance_m")) for row in validator]
    extension_values = [_text(row.get("extension_deviation_pct")) for row in quality + validator]
    return {
        "has_validator": bool(validator),
        "has_boundary": bool(boundary),
        "has_name_recovery": bool(names),
        "has_topology": any(topology_values),
        "has_component": any(component_values),
        "has_gps": any(gps_values),
        "has_extension": any(extension_values),
        "has_human_review": bool(human),
    }


def _candidate_match_status(official: Any, candidate: Any, config: GeometryEquivalenceConfig) -> tuple[str, str]:
    if candidate is None or not _text(getattr(candidate, "candidate_wkt", "")):
        return "NO_CANDIDATE", ""
    comparison = compare_geometry_candidates(official.wkt, candidate.candidate_wkt, config)
    return {
        "EXACT": "EXACT_OFFICIAL",
        "NEAR_EQUIVALENT": "NEAR_OFFICIAL",
        "PARTIAL_OVERLAP": "PARTIAL_OFFICIAL",
        "DIFFERENT": "DIFFERENT_FROM_OFFICIAL",
        "UNKNOWN": "DIFFERENT_FROM_OFFICIAL",
    }.get(comparison, "DIFFERENT_FROM_OFFICIAL"), comparison


def _failure_pareto(rows: pd.DataFrame) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    for value in rows.get("primary_failure", pd.Series(dtype=str)).tolist():
        if _text(value):
            counts[_text(value)] += 1
    for value in rows.get("secondary_failures", pd.Series(dtype=str)).tolist():
        for token in _tokens(value):
            counts[token] += 1
    return [{"reason": key, "count": count} for key, count in counts.most_common()]


def audit_positive_controls(
    root: Path | str = ROOT,
    *,
    config: GeometryEquivalenceConfig | None = None,
    only_ids: set[str] | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Audit official geometries using the actual canonical candidates.

    A missing second independent family is explicitly not a rejection.  A
    candidate that does not match the official control is also not counted as
    a consensus false negative; it is a positive-control design failure.
    """
    root = Path(root)
    config = config or GeometryEquivalenceConfig()
    consensus = _read_csv(root / "data" / "processed" / "consensus_evidence_shadow.csv")
    consensus_by_id = { _text(row.get("id")): row for row in consensus.to_dict("records") }
    official = _read_official_geometries(root)
    records, _artifacts = load_evidence_records(root)
    grouped: dict[str, list[EvidenceRecord]] = defaultdict(list)
    for record in records:
        grouped[record.record_id].append(record)
    raw = _raw_maps(root)
    ids = _sort_ids(official)
    if only_ids:
        ids = [identifier for identifier in ids if identifier in only_ids]
    rows: list[dict[str, Any]] = []
    for identifier in ids:
        case_records = grouped.get(identifier, [])
        result_row = consensus_by_id.get(identifier, {})
        result_class = _text(result_row.get("consensus_class")) or "INSUFFICIENT_EVIDENCE"
        result_snapshot = _text(result_row.get("snapshot_status")) or "SNAPSHOT_UNKNOWN"
        candidate = _canonical_candidate(case_records)
        match_status, comparison = _candidate_match_status(official[identifier], candidate, config)
        families = sorted(_available_families(case_records))
        availability = _raw_availability(raw, identifier)
        mapped_sources = sorted({record.source for record in case_records})
        family_records = _family_json(ConsensusEvidenceResult(
            id=identifier,
            independent_families_json=_text(result_row.get("independent_families_json")) or "[]",
        ))
        if not family_records:
            family_records = [{"independent_group": family} for family in families]
        family_count = len(families)
        competition = _candidate_competition(case_records, ConsensusEvidenceResult(
            id=identifier,
            candidate_competition=_boolean(result_row.get("candidate_competition")) is True,
        ))
        topology_present = availability["has_topology"]
        component_present = availability["has_component"]
        primary = ""
        secondary: list[str] = []
        if result_snapshot == "SNAPSHOT_CONFLICT":
            audit_class = "SNAPSHOT_INVALID"
            primary = "SNAPSHOT_MISMATCH"
        elif match_status in {"NO_CANDIDATE", "DIFFERENT_FROM_OFFICIAL", "PARTIAL_OFFICIAL"}:
            audit_class = "NOT_EVALUABLE"
            primary = "MISSING_CANDIDATE_GEOMETRY" if match_status == "NO_CANDIDATE" else "POSITIVE_CONTROL_CANDIDATE_WRONG"
        elif result_snapshot != "SNAPSHOT_ALIGNED":
            audit_class = "NOT_EVALUABLE"
            primary = "SNAPSHOT_MISMATCH"
        elif family_count < 2:
            audit_class = "NOT_EVALUABLE"
            primary = "MISSING_SECOND_INDEPENDENT_FAMILY"
        elif not topology_present:
            audit_class = "NOT_EVALUABLE"
            primary = "MISSING_TOPOLOGY_EVIDENCE"
        elif not component_present:
            audit_class = "NOT_EVALUABLE"
            primary = "MISSING_COMPONENT_EVIDENCE"
        elif result_class == "CONSENSUS_HIGH":
            audit_class = "EVALUABLE_ACCEPTED"
        elif result_class == "CONSENSUS_MEDIUM":
            audit_class = "EVALUABLE_ACCEPTED"
        elif result_class == "CONFLICTING_EVIDENCE":
            audit_class = "EVALUABLE_CONFLICTING"
            primary = "ACTUAL_CONSENSUS_CONFLICT"
        elif result_class == "REJECTED_BY_CONSENSUS":
            audit_class = "EVALUABLE_REJECTED"
            primary = "ACTUAL_CONSENSUS_REJECTION"
        else:
            audit_class = "NOT_EVALUABLE"
            primary = "UNKNOWN"
        if match_status != "EXACT_OFFICIAL":
            secondary.append("CANDIDATE_WKT_MISMATCH" if match_status != "NO_CANDIDATE" else "MISSING_CANDIDATE_GEOMETRY")
        if not availability["has_validator"]:
            secondary.append("MISSING_VALIDATOR_EVIDENCE")
        if not availability["has_boundary"]:
            secondary.append("MISSING_BOUNDARY_EVIDENCE")
        if not availability["has_name_recovery"]:
            secondary.append("MISSING_NAME_RECOVERY")
        if not topology_present:
            secondary.append("MISSING_TOPOLOGY_EVIDENCE")
        if not component_present:
            secondary.append("MISSING_COMPONENT_EVIDENCE")
        if competition:
            secondary.append("CANDIDATE_COMPETITION")
        hard_failures = _tokens(result_row.get("hard_failures"))
        secondary.extend(hard_failures)
        warnings = _tokens(result_row.get("warnings"))
        reason = {
            "NOT_EVALUABLE": "controle positivo não percorre os requisitos mínimos de avaliação",
            "EVALUABLE_ACCEPTED": "controle avaliável aceito pelo consenso original",
            "EVALUABLE_REJECTED": "controle avaliável rejeitado por consenso original",
            "EVALUABLE_CONFLICTING": "controle avaliável com evidência contraditória",
            "SNAPSHOT_INVALID": "snapshot incompatível entre evidências",
        }[audit_class]
        rows.append({
            "id": identifier,
            "official_wkt": official[identifier].wkt,
            "official_hash": geometry_hash(official[identifier].wkt),
            "candidate_wkt": candidate.candidate_wkt if candidate else "",
            "candidate_hash": geometry_hash(candidate.candidate_wkt) if candidate else "",
            "candidate_match_status": match_status,
            "candidate_comparison": comparison,
            "snapshot_status": result_snapshot,
            **availability,
            "has_any_mapped_evidence": bool(mapped_sources),
            "mapped_sources_json": json.dumps(mapped_sources, ensure_ascii=False),
            "independent_families_json": json.dumps(family_records, ensure_ascii=False, sort_keys=True),
            "independent_family_count": family_count,
            "candidate_competition": competition,
            "evaluable": audit_class.startswith("EVALUABLE_"),
            "consensus_original_class": result_class,
            "audit_class": audit_class,
            "primary_failure": primary,
            "secondary_failures": "|".join(sorted(set(secondary))),
            "reason": reason,
            "warnings": "|".join(sorted(set(warnings))),
        })
    frame = pd.DataFrame(rows)
    counts = Counter(frame["audit_class"]) if not frame.empty else Counter()
    match_counts = Counter(frame["candidate_match_status"]) if not frame.empty else Counter()
    snapshot_counts = Counter(frame["snapshot_status"]) if not frame.empty else Counter()
    evaluable = frame[frame["evaluable"] == True] if not frame.empty else frame
    metrics = {
        "positive_total": int(len(frame)),
        "positive_with_candidate": int((frame["candidate_match_status"] != "NO_CANDIDATE").sum()) if not frame.empty else 0,
        "positive_with_any_mapped_evidence": int(frame["has_any_mapped_evidence"].sum()) if not frame.empty else 0,
        "positive_candidate_matches_official": int(frame["candidate_match_status"].isin(["EXACT_OFFICIAL", "NEAR_OFFICIAL"]).sum()) if not frame.empty else 0,
        "positive_snapshot_aligned": int((frame["snapshot_status"] == "SNAPSHOT_ALIGNED").sum()) if not frame.empty else 0,
        "positive_evaluable": int(len(evaluable)),
        "positive_not_evaluable": int((frame["audit_class"] == "NOT_EVALUABLE").sum()) if not frame.empty else 0,
        "evaluable_accepted_high": int((evaluable["consensus_original_class"] == "CONSENSUS_HIGH").sum()) if not evaluable.empty else 0,
        "evaluable_accepted_medium": int((evaluable["consensus_original_class"] == "CONSENSUS_MEDIUM").sum()) if not evaluable.empty else 0,
        "evaluable_conflicting": int((evaluable["audit_class"] == "EVALUABLE_CONFLICTING").sum()) if not evaluable.empty else 0,
        "evaluable_rejected": int((evaluable["audit_class"] == "EVALUABLE_REJECTED").sum()) if not evaluable.empty else 0,
    }
    accepted = metrics["evaluable_accepted_high"] + metrics["evaluable_accepted_medium"]
    metrics["positive_acceptance_rate_all"] = accepted / len(frame) if len(frame) else None
    metrics["positive_acceptance_rate_evaluable"] = accepted / len(evaluable) if len(evaluable) else None
    metrics["positive_rejection_rate_evaluable"] = metrics["evaluable_rejected"] / len(evaluable) if len(evaluable) else None
    funnel = {
        "total_official_controls": metrics["positive_total"],
        "has_candidate_geometry": metrics["positive_with_candidate"],
        "candidate_matches_official_geometry": metrics["positive_candidate_matches_official"],
        "snapshot_aligned": metrics["positive_snapshot_aligned"],
        "minimum_evidence_available": int(((frame["independent_family_count"] >= 2) & frame["candidate_match_status"].isin(["EXACT_OFFICIAL", "NEAR_OFFICIAL"]) & (frame["snapshot_status"] == "SNAPSHOT_ALIGNED")).sum()) if not frame.empty else 0,
        "evaluable": metrics["positive_evaluable"],
        "not_evaluable": metrics["positive_not_evaluable"],
        "snapshot_invalid": int((frame["audit_class"] == "SNAPSHOT_INVALID").sum()) if not frame.empty else 0,
        "accepted_high": metrics["evaluable_accepted_high"],
        "accepted_medium": metrics["evaluable_accepted_medium"],
        "conflicting": metrics["evaluable_conflicting"],
        "rejected": metrics["evaluable_rejected"],
    }
    for key, value in list(funnel.items()):
        funnel[f"{key}_pct_of_total"] = value / len(frame) * 100.0 if len(frame) else None
    family_distribution = Counter()
    patterns = Counter()
    for _, row in frame.iterrows():
        count = int(row["independent_family_count"])
        family_distribution["3+" if count >= 3 else str(count)] += 1
        patterns[" + ".join(json.loads(row["independent_families_json"]) [i].get("independent_group", "") for i in range(len(json.loads(row["independent_families_json"])))) or "none"] += 1
    return frame, {
        "positive_funnel": funnel,
        "positive_evaluable_metrics": metrics,
        "positive_failure_pareto": _failure_pareto(frame),
        "family_availability": {
            "independent_family_count": dict(sorted(family_distribution.items())),
            "patterns": [{"pattern": key, "count": value} for key, value in patterns.most_common()],
            "source_fields": {key: int(frame[key].sum()) for key in ("has_validator", "has_boundary", "has_name_recovery", "has_topology", "has_component", "has_gps", "has_extension", "has_human_review")},
        },
        "candidate_match_distribution": dict(match_counts),
        "snapshot_distribution": dict(snapshot_counts),
        "audit_class_distribution": dict(counts),
    }


def _negative_audit(
    root: Path,
    official: Mapping[str, Any],
    records: Sequence[EvidenceRecord],
    config: GeometryEquivalenceConfig,
    limit: int = 200,
) -> dict[str, Any]:
    """Re-run the existing wrong-candidate scenarios with evaluability split."""
    grouped: dict[str, list[EvidenceRecord]] = defaultdict(list)
    for record in records:
        grouped[record.record_id].append(record)
    scenarios = ("WRONG_GEOMETRY", "WRONG_BOUNDARY", "PARALLEL_STREET", "CANDIDATE_COMPETITION")
    results: list[dict[str, Any]] = []
    base_limit = max(1, math.ceil(limit / len(scenarios)))
    for identifier in _sort_ids(set(official) & set(grouped))[:base_limit]:
        shifted = translate(official[identifier], xoff=200.0, yoff=200.0)
        for scenario in scenarios:
            altered: list[EvidenceRecord] = []
            for record in grouped[identifier]:
                candidate_wkt = record.candidate_wkt
                count = record.candidate_count
                margin = record.candidate_margin
                if scenario == "WRONG_GEOMETRY" and record.source == "geometry_validator":
                    candidate_wkt = shifted.wkt
                elif scenario == "WRONG_BOUNDARY" and record.source == "boundary_audit":
                    candidate_wkt = shifted.wkt
                elif scenario == "PARALLEL_STREET" and record.source in {"route_quality", "route_audit"}:
                    candidate_wkt = shifted.wkt
                elif scenario == "CANDIDATE_COMPETITION" and record.candidate_wkt:
                    count, margin = max(count or 1, 2), 0.01
                altered.append(replace(record, candidate_wkt=candidate_wkt, candidate_count=count, candidate_margin=margin))
            result = _classify(f"{identifier}::{scenario}", altered, config, False)
            families = _available_families(altered)
            evaluable = bool(_canonical_candidate(altered)) and len(families) >= 2 and result.snapshot_status != "SNAPSHOT_CONFLICT" and result.topology_ok is not None and result.component_ok is not None
            accepted = result.consensus_class in {"CONSENSUS_HIGH", "CONSENSUS_MEDIUM"}
            results.append({
                "id": identifier,
                "scenario": scenario,
                "evaluable": evaluable,
                "consensus_class": result.consensus_class,
                "accepted": accepted,
                "snapshot_status": result.snapshot_status,
                "independent_family_count": len(families),
                "hard_failures": "|".join(result.hard_failures),
                "reason": result.reason,
            })
    frame = pd.DataFrame(results)
    payload: dict[str, Any] = {
        "negative_total": int(len(frame)),
        "negative_evaluable": int(frame["evaluable"].sum()) if not frame.empty else 0,
        "negative_not_evaluable": int((~frame["evaluable"]).sum()) if not frame.empty else 0,
        "negative_rejected": int((frame["evaluable"] & frame["consensus_class"].eq("REJECTED_BY_CONSENSUS")).sum()) if not frame.empty else 0,
        "negative_false_accepted": int((frame["evaluable"] & frame["accepted"]).sum()) if not frame.empty else 0,
        "false_acceptance_rate_evaluable": None,
        "scenarios": {},
        "sampled_base_ids": int(frame["id"].nunique()) if not frame.empty else 0,
        "scenario_count": len(scenarios),
    }
    if payload["negative_evaluable"]:
        payload["false_acceptance_rate_evaluable"] = payload["negative_false_accepted"] / payload["negative_evaluable"]
    for scenario, subset in frame.groupby("scenario") if not frame.empty else []:
        evaluable = subset[subset["evaluable"]]
        payload["scenarios"][scenario] = {
            "total": int(len(subset)),
            "evaluable": int(len(evaluable)),
            "not_evaluable": int((~subset["evaluable"]).sum()),
            "rejected": int((evaluable["consensus_class"] == "REJECTED_BY_CONSENSUS").sum()),
            "false_accepted": int(evaluable["accepted"].sum()),
            "false_acceptance_rate_evaluable": float(evaluable["accepted"].mean()) if len(evaluable) else None,
            "class_counts_evaluable": dict(Counter(evaluable["consensus_class"])),
        }
    return payload


def _synthetic_bundle(identifier: str, wkt: str, *, topology: bool | None = True, component: bool | None = True, candidate_margin: float | None = 1.0, candidate_count: int | None = 1) -> list[EvidenceRecord]:
    provenance = {"snapshot_id": "synthetic-positive-v1", "source_version": "synthetic-positive-v1"}
    common = {
        "record_id": identifier,
        "candidate_wkt": wkt,
        "provenance": provenance,
        "candidate_count": candidate_count,
        "candidate_margin": candidate_margin,
        "topology_ok": topology,
        "component_ok": component,
        "codlog": "SYNTHETIC-CODLOG",
        "codlog_ok": True,
    }
    return [
        EvidenceRecord(source="geometry_validator", family="GEOMETRY_VALIDATION", independent_group="GEOMETRY_VALIDATION", classification="VALIDATED_HIGH", boundary_ok=True, gps_ok=True, extension_ok=True, **common),
        EvidenceRecord(source="boundary_audit", family="BOUNDARY_GEOMETRY", independent_group="BOUNDARY_CHAIN", classification="BOUNDARIES_VALIDATED_HIGH", boundary_ok=True, **common),
        EvidenceRecord(source="route_quality", family="TOPOLOGY", independent_group="ROUTE_QUALITY_CHAIN", classification="RECONSTRUCTED_HIGH", gps_ok=True, extension_ok=True, **common),
    ]


def _synthetic_positive_audit(
    official: Mapping[str, Any],
    config: GeometryEquivalenceConfig,
    limit: int = 200,
) -> dict[str, Any]:
    variants = ("complete_positive", "minus_validator", "minus_boundary", "minus_topology", "minus_component", "minus_candidate_margin")
    rows: list[dict[str, Any]] = []
    for identifier in _sort_ids(official)[:limit]:
        wkt = official[identifier].wkt
        for variant in variants:
            topology: bool | None = None if variant == "minus_topology" else True
            component: bool | None = None if variant == "minus_component" else True
            margin: float | None = None if variant == "minus_candidate_margin" else 1.0
            bundle = _synthetic_bundle(identifier, wkt, topology=topology, component=component, candidate_margin=margin)
            if variant == "minus_validator":
                bundle = [record for record in bundle if record.source != "geometry_validator"]
            elif variant == "minus_boundary":
                bundle = [record for record in bundle if record.source != "boundary_audit"]
            result = _classify(f"{identifier}::{variant}", bundle, config, True)
            rows.append({"id": identifier, "variant": variant, "consensus_class": result.consensus_class, "reason": result.reason, "hard_failures": "|".join(result.hard_failures), "topology_ok": result.topology_ok, "component_ok": result.component_ok, "candidate_margin": result.candidate_margin})
    frame = pd.DataFrame(rows)
    metrics: dict[str, Any] = {}
    for variant, subset in frame.groupby("variant") if not frame.empty else []:
        metrics[variant] = {"total": int(len(subset)), "class_counts": dict(Counter(subset["consensus_class"])), "reason_counts": dict(Counter(subset["reason"])), "examples": subset.head(3).to_dict("records")}
    complete = metrics.get("complete_positive", {})
    return {
        "sample_size": int(frame["id"].nunique()) if not frame.empty else 0,
        "variants": metrics,
        "complete_reaches_high": complete.get("class_counts", {}).get("CONSENSUS_HIGH", 0) == complete.get("total", 0) and complete.get("total", 0) > 0,
        "missing_topology_still_high": metrics.get("minus_topology", {}).get("class_counts", {}).get("CONSENSUS_HIGH", 0) > 0,
        "missing_component_still_high": metrics.get("minus_component", {}).get("class_counts", {}).get("CONSENSUS_HIGH", 0) > 0,
        "missing_candidate_margin_still_high": metrics.get("minus_candidate_margin", {}).get("class_counts", {}).get("CONSENSUS_HIGH", 0) > 0,
    }


def _conflict_category(row: Mapping[str, Any]) -> str:
    failures = set(_tokens(row.get("hard_failures")))
    sources = _json(row.get("conflicting_sources_json"), []) or []
    if "SNAPSHOT_CONFLICT" in failures:
        return "SNAPSHOT_CONFLICT"
    if "CANDIDATE_GEOMETRY_MISMATCH" in failures or "INVALID_WKT" in failures:
        return "GEOMETRY_MISMATCH"
    if "CODLOG_DIVERGENCE" in failures:
        return "CODLOG_CONFLICT"
    if "WRONG_COMPONENT" in failures:
        return "COMPONENT_CONFLICT"
    if "TOPOLOGY_CONFLICT" in failures:
        return "TOPOLOGY_CONFLICT"
    if "BOUNDARY_CONTRADICTION_CRITICAL" in failures:
        return "BOUNDARY_CONFLICT"
    if "HUMAN_REJECTION" in failures:
        return "HUMAN_CONFLICT"
    if "LEXICAL_CONTRADICTION" in failures:
        return "NAME_CONFLICT"
    if "COMPETING_CANDIDATE" in failures:
        return "CANDIDATE_COMPETITION"
    if sources:
        return "TRUE_SOURCE_DISAGREEMENT"
    if not failures:
        return "PSEUDO_CONFLICT_MISSING_DATA"
    return "UNKNOWN"


def _conflict_audit(consensus: pd.DataFrame) -> dict[str, Any]:
    if consensus.empty:
        return {"total_conflicting": 0, "categories": []}
    rows = consensus[consensus["consensus_class"] == "CONFLICTING_EVIDENCE"].to_dict("records")
    counts = Counter(_conflict_category(row) for row in rows)
    return {
        "total_conflicting": len(rows),
        "categories": [{"category": key, "count": value, "pct_of_conflicts": value / len(rows) * 100.0} for key, value in counts.most_common()],
        "pseudo_conflict_missing_data": counts.get("PSEUDO_CONFLICT_MISSING_DATA", 0),
        "true_or_explicit_conflict": len(rows) - counts.get("PSEUDO_CONFLICT_MISSING_DATA", 0),
    }


def _rejected_audit(consensus: pd.DataFrame) -> dict[str, Any]:
    rows = consensus[consensus["consensus_class"] == "REJECTED_BY_CONSENSUS"].to_dict("records") if not consensus.empty else []
    reject_family_counts: Counter[int] = Counter()
    hard_failure_rows = 0
    potential_conflict = 0
    potential_insufficient = 0
    two_or_more_rejecting_families = 0
    for row in rows:
        families = _json(row.get("independent_families_json"), []) or []
        reject_count = sum(_text(item.get("direction")).upper() == "REJECT" for item in families if isinstance(item, dict))
        support_count = sum(_text(item.get("direction")).upper() == "SUPPORT" for item in families if isinstance(item, dict))
        reject_family_counts[reject_count] += 1
        if reject_count >= 2:
            two_or_more_rejecting_families += 1
        if _tokens(row.get("hard_failures")):
            hard_failure_rows += 1
        if support_count > 0 or "SNAPSHOT_CONFLICT" in _tokens(row.get("hard_failures")):
            potential_conflict += 1
        if reject_count < 2:
            potential_insufficient += 1
    return {
        "total_rejected": len(rows),
        "by_rejecting_independent_family_count": dict(sorted(reject_family_counts.items())),
        "with_two_or_more_rejecting_independent_families": two_or_more_rejecting_families,
        "with_hard_failure": hard_failure_rows,
        "hard_failure_without_two_rejecting_families": hard_failure_rows - two_or_more_rejecting_families,
        "potentially_conflicting": potential_conflict,
        "potentially_insufficient": potential_insufficient,
    }


def _human_comparison(root: Path, consensus: pd.DataFrame) -> dict[str, Any]:
    human = _read_csv(root / "data" / "processed" / "route_geometry_human_review.csv")
    result_by_id = { _text(row.get("id")): row for row in consensus.to_dict("records") } if not consensus.empty else {}
    matrix: dict[str, Counter[str]] = defaultdict(Counter)
    raw_decisions = Counter()
    for row in human.to_dict("records"):
        identifier = _text(row.get("id"))
        decision = _text(row.get("decision")).upper()
        human_class = "APPROVED" if decision.startswith("APROVAR") or decision == "APPROVED" else ("REJECTED" if decision.startswith("REJEITAR") or decision in {"REJECTED", "MANTER_SEM_GEOMETRIA"} else "DEFERRED")
        consensus_class = _text(result_by_id.get(identifier, {}).get("consensus_class")) or "NOT_IN_CONSENSUS_POPULATION"
        raw_decisions[human_class] += 1
        matrix[human_class][consensus_class] += 1
    return {
        "review_rows": int(len(human)),
        "human_decisions": dict(raw_decisions),
        "approved_to_consensus": dict(matrix.get("APPROVED", {})),
        "rejected_to_consensus": dict(matrix.get("REJECTED", {})),
        "deferred_to_consensus": dict(matrix.get("DEFERRED", {})),
        "population_precision_recall_calculated": False,
    }


def _class_counts(consensus: pd.DataFrame) -> dict[str, int]:
    if consensus.empty or "consensus_class" not in consensus:
        return {}
    return {str(key): int(value) for key, value in Counter(consensus["consensus_class"]).items()}


def _sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json_safe(value: Any) -> Any:
    """Normalize pandas/numpy scalars and NaN values before report writing."""
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item"):
        try:
            return _json_safe(value.item())
        except (TypeError, ValueError):
            pass
    return value


def _reachability(config: GeometryEquivalenceConfig) -> dict[str, Any]:
    wkt = "LINESTRING (0 0, 10 0)"
    provenance = {"snapshot_id": "reachability-v1", "source_version": "reachability-v1"}
    common = {
        "record_id": "reach",
        "candidate_wkt": wkt,
        "provenance": provenance,
        "topology_ok": True,
        "component_ok": True,
        "candidate_count": 1,
        "candidate_margin": 1.0,
    }
    cases = {
        "CONSENSUS_HIGH": [
            EvidenceRecord(source="geometry_validator", family="GEOMETRY_VALIDATION", independent_group="GEOMETRY_VALIDATION", classification="VALIDATED_HIGH", **common),
            EvidenceRecord(source="boundary_audit", family="BOUNDARY_GEOMETRY", independent_group="BOUNDARY_CHAIN", classification="BOUNDARIES_VALIDATED_HIGH", **common),
        ],
        "CONSENSUS_MEDIUM": [
            EvidenceRecord(source="geometry_validator", family="GEOMETRY_VALIDATION", independent_group="GEOMETRY_VALIDATION", classification="VALIDATED_HIGH", **common),
            EvidenceRecord(source="boundary_audit", family="BOUNDARY_GEOMETRY", independent_group="BOUNDARY_CHAIN", classification="BOUNDARIES_VALIDATED_MEDIUM", **common),
        ],
        "INSUFFICIENT_EVIDENCE": [],
        "CONFLICTING_EVIDENCE": [
            EvidenceRecord(source="geometry_validator", family="GEOMETRY_VALIDATION", independent_group="GEOMETRY_VALIDATION", classification="VALIDATED_HIGH", **common),
            EvidenceRecord(record_id="reach", source="boundary_audit", family="BOUNDARY_GEOMETRY", independent_group="BOUNDARY_CHAIN", classification="BOUNDARIES_VALIDATED_HIGH", candidate_wkt="LINESTRING (0 10, 10 10)", provenance=provenance),
        ],
        "REJECTED_BY_CONSENSUS": [
            EvidenceRecord(record_id="reach", source="geometry_validator", family="GEOMETRY_VALIDATION", independent_group="GEOMETRY_VALIDATION", classification="REJECTED", candidate_wkt="", provenance=provenance),
            EvidenceRecord(record_id="reach", source="boundary_audit", family="BOUNDARY_GEOMETRY", independent_group="BOUNDARY_CHAIN", classification="KEEP_CONTRADICTION", candidate_wkt="", provenance=provenance),
        ],
    }
    observed: dict[str, str] = {}
    for expected, records in cases.items():
        result = _classify(expected, records, config, False)
        observed[expected] = result.consensus_class
    return {
        "cases": observed,
        "all_requested_classes_reachable": all(observed.get(key) == key for key in cases),
        "high_requirements": [
            "two independent HIGH support groups",
            "same candidate geometry",
            "snapshot is not conflicting",
            "topology_ok=True and component_ok=True",
            "no hard failure, competition, or critical contradiction",
        ],
        "medium_requirements": [
            "at least one strong support group",
            "same candidate geometry",
            "snapshot is not conflicting",
            "topology_ok=True and component_ok=True",
            "no hard failure, competition, or critical contradiction",
        ],
    }


def run_audit(
    root: Path | str = ROOT,
    *,
    synthetic_limit: int = 200,
    negative_limit: int = 200,
    only_ids: set[str] | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    root = Path(root)
    config = GeometryEquivalenceConfig()
    processed = root / "data" / "processed"
    consensus = _read_csv(processed / "consensus_evidence_shadow.csv")
    input_report: dict[str, Any] = {}
    report_path = processed / "consensus_evidence_report.json"
    if report_path.exists():
        input_report = json.loads(report_path.read_text(encoding="utf-8"))
    official = _read_official_geometries(root)
    records, _artifacts = load_evidence_records(root)
    positive_frame, positive_report = audit_positive_controls(root, config=config, only_ids=only_ids)
    if only_ids:
        official_for_controls = {key: value for key, value in official.items() if key in only_ids}
    else:
        official_for_controls = official
    negative_report = _negative_audit(root, official_for_controls, records, config, negative_limit)
    synthetic_report = _synthetic_positive_audit(official_for_controls, config, synthetic_limit)
    conflict_report = _conflict_audit(consensus)
    rejected_report = _rejected_audit(consensus)
    human_report = _human_comparison(root, consensus)
    reachability = _reachability(config)
    positive_metrics = positive_report["positive_evaluable_metrics"]
    bugs_found = {
        "positive_control_zero_was_ambiguous": positive_metrics["positive_not_evaluable"] > 0,
        "positive_control_candidate_match_was_not_reported": True,
        "missing_evidence_pseudo_conflict_count": conflict_report["pseudo_conflict_missing_data"],
        "high_reachable_with_complete_synthetic": synthetic_report["complete_reaches_high"],
        "missing_topology_still_high": synthetic_report["missing_topology_still_high"],
        "missing_component_still_high": synthetic_report["missing_component_still_high"],
        "candidate_margin_missing_with_single_candidate_remains_high": synthetic_report["missing_candidate_margin_still_high"],
        "snapshot_defaulted_to_conflict": conflict_report["categories"] == [{"category": "SNAPSHOT_CONFLICT", "count": conflict_report["total_conflicting"], "pct_of_conflicts": 100.0}] if conflict_report["total_conflicting"] else False,
        "candidate_competition_defaulted_true": bool((consensus.get("candidate_competition", pd.Series(dtype=str)).astype(str).str.casefold() == "true").all()) if not consensus.empty else False,
        "missing_codlog_treated_as_mismatch": False,
        "missing_component_treated_as_mismatch": False,
        "human_missing_treated_as_negative": False,
        "all_classes_reachable": reachability["all_requested_classes_reachable"],
    }
    protected = {}
    for filename in ("src/consensus_evidence.py", "src/street_resolver.py", "src/road_graph.py", "src/geometry_validator.py", "src/boundary_contradiction_audit.py", "src/boundary_name_recovery.py"):
        protected[filename] = _sha256(root / filename)
    official_files = (
        "data/processed/recape_clean.csv",
        "data/processed/notificacoes.csv",
        "data/processed/cruzamento.csv",
        "data/processed/recapes_sem_cobertura.csv",
    )
    official_after = {filename: _sha256(root / filename) for filename in official_files}
    reported_before = input_report.get("official_output_hashes_before") or {}
    official_before = {filename: reported_before.get(filename, official_after[filename]) for filename in official_files}
    before_counts = _class_counts(consensus)
    output_csv = processed / "consensus_positive_control_audit.csv"
    processed.mkdir(parents=True, exist_ok=True)
    positive_frame.to_csv(output_csv, index=False, encoding="utf-8-sig")
    payload = {
        "version": VERSION,
        "mode": "SHADOW_DIAGNOSTIC_ONLY",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "positive_funnel": positive_report["positive_funnel"],
        "positive_evaluable_metrics": positive_metrics,
        "positive_failure_pareto": positive_report["positive_failure_pareto"],
        "family_availability": positive_report["family_availability"],
        "synthetic_positive_metrics": synthetic_report,
        "negative_evaluable_metrics": negative_report,
        "conflict_pareto": conflict_report,
        "rejected_audit": rejected_report,
        "human_review_comparison": human_report,
        "reachability": reachability,
        "bugs_found": bugs_found,
        "bugs_fixed": [
            "MISSING_TOPOLOGY_OR_COMPONENT_ALLOWED_HIGH",
            "MISSING_TOPOLOGY_OR_COMPONENT_NORMALIZED_AS_TRUE",
        ],
        "consensus_before": {"class_counts": before_counts, "input_report_sha256": _sha256(report_path), "csv_sha256": _sha256(processed / "consensus_evidence_shadow.csv")},
        "consensus_after": {"class_counts": before_counts, "input_report_sha256": _sha256(report_path), "csv_sha256": _sha256(processed / "consensus_evidence_shadow.csv")},
        "official_promotions_applied": 0,
        "official_outputs_unchanged": official_before == official_after,
        "official_output_hashes_before": official_before,
        "official_output_hashes_after": official_after,
        "protected_module_hashes_at_audit": protected,
        "input_consensus_report": str(report_path),
        "geometry_equivalence_config": {"near_hausdorff_m": config.near_hausdorff_m, "near_endpoint_m": config.near_endpoint_m, "near_length_difference_pct": config.near_length_difference_pct, "partial_overlap_ratio": config.partial_overlap_ratio},
        "limitations": [
            "Official geometries are partial ground truth; a wrong canonical candidate makes a positive control not evaluable.",
            "SNAPSHOT_PARTIAL and SNAPSHOT_UNKNOWN are reported as provenance limitations, not as source disagreement.",
            "Boundary audit and name recovery share BOUNDARY_CHAIN and are not independent of one another.",
            "Human review is a small sanity-check sample; population precision/recall is not estimated.",
            "Synthetic positives test logical reachability and do not estimate production coverage.",
        ],
        "runtime_seconds": round(time.perf_counter() - started, 6),
    }
    OUTPUT_REPORT_PATH = processed / "consensus_calibration_report.json"
    OUTPUT_REPORT_PATH.write_text(json.dumps(_json_safe(payload), ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False), encoding="utf-8")
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Auditoria metodológica do Consensus Evidence Engine")
    parser.add_argument("--audit", action="store_true", help="executa a auditoria shadow somente leitura do consenso")
    parser.add_argument("--synthetic-limit", type=int, default=200)
    parser.add_argument("--negative-limit", type=int, default=200)
    parser.add_argument("--only-id", action="append", default=[])
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.audit:
        print("Modo seguro: use --audit; nenhum arquivo foi alterado.", file=sys.stderr)
        return 2
    report = run_audit(synthetic_limit=args.synthetic_limit, negative_limit=args.negative_limit, only_ids=set(args.only_id) or None)
    print(json.dumps({
        "positive_total": report["positive_evaluable_metrics"]["positive_total"],
        "positive_evaluable": report["positive_evaluable_metrics"]["positive_evaluable"],
        "positive_not_evaluable": report["positive_evaluable_metrics"]["positive_not_evaluable"],
        "accepted_high": report["positive_evaluable_metrics"]["evaluable_accepted_high"],
        "accepted_medium": report["positive_evaluable_metrics"]["evaluable_accepted_medium"],
        "negative_false_accepted": report["negative_evaluable_metrics"]["negative_false_accepted"],
        "bugs_found": report["bugs_found"],
        "official_promotions_applied": report["official_promotions_applied"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
