from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from route_geometry_review import (
    DECISIONS,
    REVIEW_COLUMNS,
    ReviewDataError,
    alternatives_table,
    approve_cases_in_bulk,
    atomic_write_csv,
    batch_approval_preview,
    build_review_key,
    export_approved,
    export_rejected,
    filter_cases,
    geometry_from_wkt,
    geometry_geojson,
    is_valid_wkt,
    load_review_data,
    load_reviews,
    merge_reviews,
    parse_alternatives,
    review_metrics,
    save_decision,
    stratified_sample,
    validate_decision,
    write_report,
)


LINE = "LINESTRING (338000 7390000, 338100 7390000)"
ALT_LINE = "LINESTRING (338000 7390000, 338000 7390100)"


def cases_frame() -> pd.DataFrame:
    rows = []
    values = [
        ("1", "HIGH", "STRONG", LINE, 95, 0.0, 1, 0.0, False, ""),
        ("2", "MEDIUM", "BOUNDARY", LINE, 85, 2.0, 1, 0.0, False, ""),
        ("3", "ESTIMATED", "GPS", LINE, 90, 1.0, 1, 0.0, False, "fallback_estimado"),
        ("4", "HIGH", "LOOP", LINE, 96, 0.0, 1, 0.0, True, "LOOP_DETECTADO"),
        ("5", "HIGH", "MULTI", LINE, 96, 0.0, 2, 0.0, False, ""),
        ("6", "HIGH", "INVALID", "not-wkt", 96, 0.0, 1, 0.0, False, ""),
        ("7", "HIGH", "WARNING", LINE, 96, 0.0, 1, 0.0, False, "NAO_APLICAR_SEM_REVISAO"),
    ]
    for identifier, confidence, strategy, wkt, score, deviation, components, gap, loop, warnings in values:
        row = {
            "id": identifier, "geometry_confidence": f"RECONSTRUCTED_{confidence}" if confidence != "ESTIMATED" else confidence,
            "confidence_class": confidence, "strategy_selected": strategy, "geometry_score": score,
            "geometry_wkt": wkt, "geometry_geojson": geometry_geojson(LINE), "path_length_m": 100.0,
            "extension_deviation_pct": deviation, "segment_count": 2, "component_count": components,
            "snap_used": False, "snap_distance_de_m": 0.5, "max_gap_m": gap, "loop_detected": loop,
            "warnings": warnings, "alternatives_json": json.dumps([{
                "strategy": "ALT", "geometry_wkt": ALT_LINE, "confidence": "MEDIUM", "score": 88,
                "length_m": 100, "deviation_pct": 1, "segment_count": 2, "component_count": 1,
                "snap_used": False, "max_gap_m": 0,
            }]), "original_failure_category": "MULTIPLOS_SEGMENTOS_POSSIVEIS",
            "root_cause_primary": "MULTIPLOS_SEGMENTOS_POSSIVEIS", "root_causes": "MULTIPLOS_SEGMENTOS_POSSIVEIS",
            "source_audit_version": "test-v1", "source_geometry_signature": "sig-" + identifier,
            "candidate_available": wkt != "not-wkt", "is_candidate": True, "is_baseline_reconstructed": False,
            "current_geometry_wkt": "", "has_current_geometry": False, "via_original": f"Rua {identifier}",
            "via_resolvida": f"RUA {identifier}", "codlog": f"000{identifier}", "de": "A", "ate": "B",
            "latitude": -23.5, "longitude": -46.6, "extensao_m": 100.0, "status_atual": "SEM_CAMINHO",
        }
        row["review_key"] = build_review_key(row)
        rows.append(row)
    official = rows[0].copy()
    official.update({
        "id": "OFFICIAL", "confidence_class": "OFFICIAL", "geometry_confidence": "OFFICIAL",
        "is_candidate": False, "is_baseline_reconstructed": False, "current_geometry_wkt": LINE,
        "has_current_geometry": True, "candidate_available": False, "review_key": "official-key",
    })
    return pd.DataFrame(rows + [official])


def test_utf8_sig_union_keeps_all_sources_and_classes(tmp_path):
    recapes = pd.DataFrame({"id": ["1", "OFFICIAL", "x"], "via": ["Rua Um", "Rua Oficial", "Rua X"], "path": ["", LINE, ""], "latitude": [-23.5] * 3, "longitude": [-46.6] * 3})
    quality = pd.DataFrame({"id": ["1"], "via": ["Rua Um"], "strategy_selected": ["GPS"], "geometry_confidence": ["ESTIMATED"], "geometry_wkt": [LINE], "geometry_score": [80]})
    audit = pd.DataFrame({"id": ["OFFICIAL"], "geometry_confidence": ["RECONSTRUCTED_HIGH"], "geometry_wkt": [LINE], "strategy_selected": ["BASE"]})
    for frame, name in ((recapes, "recapes.csv"), (quality, "quality.csv"), (audit, "audit.csv")):
        frame.to_csv(tmp_path / name, index=False, encoding="utf-8-sig")
    loaded = load_review_data(tmp_path / "quality.csv", tmp_path / "audit.csv", tmp_path / "recapes.csv")
    assert len(loaded) == 3
    assert loaded.loc[loaded.id == "1", "confidence_class"].item() == "ESTIMATED"
    assert loaded.loc[loaded.id == "OFFICIAL", "confidence_class"].item() == "HIGH"
    assert loaded.loc[loaded.id == "x", "confidence_class"].item() == "NO_CANDIDATE"


def test_review_key_is_stable_and_changes_with_version_or_strategy():
    row = cases_frame().iloc[0]
    reversed_row = row[[*reversed(row.index)]]
    assert build_review_key(row) == build_review_key(reversed_row)
    changed = row.copy()
    changed["strategy_selected"] = "OTHER"
    assert build_review_key(row) != build_review_key(changed)


def test_geometry_validation_is_lazy_and_geojson_is_available():
    assert is_valid_wkt(LINE)
    assert not is_valid_wkt("not-wkt")
    assert geometry_from_wkt(LINE) is not None
    assert json.loads(geometry_geojson(LINE))["type"] == "LineString"


def test_alternatives_parser_handles_json_and_invalid_payload():
    raw = cases_frame().iloc[0]["alternatives_json"]
    assert len(parse_alternatives(raw)) == 1
    table = alternatives_table(raw)
    assert table.iloc[0]["strategy"] == "ALT"
    assert alternatives_table("not-json").empty


def test_missing_and_legacy_review_file_are_safe(tmp_path):
    assert load_reviews(tmp_path / "missing.csv").empty
    legacy = pd.DataFrame({"review_key": ["k"], "id": ["1"], "decision": ["APROVAR_GEOMETRIA"], "approved_for_official_use": ["False"]})
    legacy.to_csv(tmp_path / "legacy.csv", index=False, encoding="utf-8-sig")
    loaded = load_reviews(tmp_path / "legacy.csv")
    assert list(loaded.columns) == REVIEW_COLUMNS
    assert bool(loaded.iloc[0]["approved"]) is True
    assert bool(loaded.iloc[0]["approved_for_official_use"]) is False


def test_save_approve_high_is_incremental_and_default_not_official(tmp_path):
    case = cases_frame().iloc[0]
    path = tmp_path / "review.csv"
    first = save_decision(case, {"decision": "APROVAR_GEOMETRIA", "reviewed_by": "Ana"}, path)
    second = save_decision(case, {"decision": "REJEITAR_GEOMETRIA", "review_notes": "WKT inadequado", "reviewed_by": "Bia"}, path)
    assert len(first) == len(second) == 1
    loaded = load_reviews(path)
    assert loaded.iloc[0]["decision"] == "REJEITAR_GEOMETRIA"
    assert bool(loaded.iloc[0]["approved_for_official_use"]) is False


def test_medium_requires_no_extra_flag_but_estimated_does(tmp_path):
    medium = cases_frame().iloc[1]
    estimated = cases_frame().iloc[2]
    assert validate_decision(medium, {"decision": "APROVAR_GEOMETRIA"})["approved"] is True
    with pytest.raises(ReviewDataError, match="ESTIMATED"):
        validate_decision(estimated, {"decision": "APROVAR_GEOMETRIA"})
    assert validate_decision(estimated, {"decision": "APROVAR_GEOMETRIA"}, allow_estimated=True)["approved"] is True


def test_alternative_requires_note_and_persists_selected_geometry(tmp_path):
    case = cases_frame().iloc[0]
    with pytest.raises(ReviewDataError, match="nota"):
        validate_decision(case, {"decision": "ESCOLHER_ALTERNATIVA", "selected_candidate_index": 0})
    path = tmp_path / "reviews.csv"
    save_decision(case, {"decision": "ESCOLHER_ALTERNATIVA", "selected_candidate_index": 0, "review_notes": "Alternativa mais compatível"}, path)
    saved = load_reviews(path).iloc[0]
    assert saved["selected_strategy"] == "ALT"
    assert saved["manual_geometry_wkt"] == ALT_LINE


@pytest.mark.parametrize("decision", ["REJEITAR_GEOMETRIA", "MANTER_SEM_GEOMETRIA"])
def test_negative_decisions_require_justification(decision):
    case = cases_frame().iloc[0]
    with pytest.raises(ReviewDataError, match="justificativa"):
        validate_decision(case, {"decision": decision})
    assert validate_decision(case, {"decision": decision, "review_notes": "Sem evidência suficiente"})["approved"] is False


def test_decision_vocabulary_is_exact():
    assert DECISIONS == ("APROVAR_GEOMETRIA", "REJEITAR_GEOMETRIA", "ESCOLHER_ALTERNATIVA", "MANTER_SEM_GEOMETRIA", "ADIAR_REVISAO")


def test_batch_defaults_to_high_and_blocks_structural_risks(tmp_path):
    frame = cases_frame()
    preview = batch_approval_preview(frame)
    assert preview["distribution"] == {"HIGH": 1}
    assert preview["ignored_reasons"]["CONFIANCA_NAO_INCLUIDA"] >= 2
    assert preview["ignored_reasons"]["LOOP_DETECTADO"] == 1
    assert preview["ignored_reasons"]["MULTIPLOS_COMPONENTES"] == 1
    result = approve_cases_in_bulk(frame, tmp_path / "reviews.csv")
    assert result["approved"] == 1
    assert load_reviews(tmp_path / "reviews.csv").iloc[0]["id"] == "1"


def test_batch_medium_and_estimated_are_explicit_opt_in(tmp_path):
    frame = cases_frame().iloc[[1, 2]].copy()
    assert batch_approval_preview(frame)["approved"] == 0
    preview = batch_approval_preview(frame, include_medium=True, include_estimated=True)
    assert preview["approved"] == 2
    result = approve_cases_in_bulk(frame, tmp_path / "reviews.csv", include_medium=True, include_estimated=True)
    assert result["approved"] == 2


def test_batch_skips_existing_review_without_duplicate(tmp_path):
    frame = cases_frame().iloc[[0, 1]].copy()
    path = tmp_path / "reviews.csv"
    save_decision(frame.iloc[0], {"decision": "ADIAR_REVISAO", "review_notes": "Aguardando"}, path)
    result = approve_cases_in_bulk(frame, path, include_medium=True)
    assert result["approved"] == 1
    assert "JA_REVISADO" in result["ignored_reasons"]
    assert len(load_reviews(path)) == 2


def test_filter_combines_confidence_strategy_decision_and_text():
    frame = merge_reviews(cases_frame(), load_reviews(Path("definitely-missing-review.csv")))
    result = filter_cases(frame, {"confidence": ["HIGH"], "strategy": ["STRONG"], "decision": "PENDENTE", "id": "1"})
    assert result["id"].tolist() == ["1"]


def test_sampling_is_deterministic_and_can_filter_strategy():
    frame = pd.concat([cases_frame().iloc[:3]] * 10, ignore_index=True)
    frame["id"] = [f"{index:03d}" for index in range(len(frame))]
    frame["review_key"] = frame.apply(build_review_key, axis=1)
    one = stratified_sample(frame, {"HIGH": 2, "MEDIUM": 2, "ESTIMATED": 2}, seed=17)
    two = stratified_sample(frame, {"HIGH": 2, "MEDIUM": 2, "ESTIMATED": 2}, seed=17)
    assert one["id"].tolist() == two["id"].tolist()
    assert len(stratified_sample(frame, {"HIGH": 10}, strategy="STRONG")) == 10


def test_metrics_cover_current_baseline_shadow_and_human_approval(tmp_path):
    frame = cases_frame()
    path = tmp_path / "reviews.csv"
    save_decision(frame.iloc[0], {"decision": "APROVAR_GEOMETRIA"}, path)
    merged = merge_reviews(frame, load_reviews(path))
    metrics = review_metrics(merged)
    assert metrics["total_cases"] == 8
    assert metrics["candidate_cases"] == 7
    assert metrics["coverage"]["shadow_projected_pct"] == 100.0
    assert metrics["approved"] == 1
    assert metrics["approved_for_official_use"] == 0
    assert metrics["by_strategy"]["STRONG"]["human_approved"] == 1


def test_exports_are_new_files_and_do_not_touch_source_inputs(tmp_path):
    source = tmp_path / "official.csv"
    source.write_text("id,path\nOFFICIAL,keep\n", encoding="utf-8")
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    frame = cases_frame()
    path = tmp_path / "reviews.csv"
    save_decision(frame.iloc[0], {"decision": "APROVAR_GEOMETRIA"}, path)
    save_decision(frame.iloc[1], {"decision": "REJEITAR_GEOMETRIA", "review_notes": "Não"}, path)
    merged = merge_reviews(frame, load_reviews(path))
    approved = export_approved(merged, tmp_path / "approved.csv")
    rejected = export_rejected(merged, tmp_path / "rejected.csv")
    assert len(approved) == len(rejected) == 1
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert source.read_text(encoding="utf-8") == "id,path\nOFFICIAL,keep\n"


def test_report_contains_coverage_gain_strategy_results_and_ranking(tmp_path):
    report_source = tmp_path / "quality.json"
    report_source.write_text(json.dumps({"version": "quality-test", "before": {"projected_coverage_with_estimated_pct": 90.0}, "after": {"projected_coverage_with_estimated_pct": 100.0}}), encoding="utf-8")
    report = write_report(cases_frame(), tmp_path / "report.json", quality_report_path=report_source)
    assert report["mode"] == "shadow_diagnostic_only"
    assert "coverage_new" in report and "strategy_results" in report and report["strategy_ranking"]
    saved = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert saved["official_application"] is False


def test_atomic_write_creates_utf8_sig_and_replaces_once(tmp_path):
    path = tmp_path / "atomic.csv"
    atomic_write_csv(pd.DataFrame({"texto": ["ação"]}), path)
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    assert pd.read_csv(path, encoding="utf-8-sig").iloc[0, 0] == "ação"


def test_source_module_isolated_from_motor_files():
    source = (Path(__file__).parents[1] / "src" / "route_geometry_review.py").read_text(encoding="utf-8")
    assert "import RoadGraph" not in source
    assert "import StreetResolver" not in source
    assert "from transform" not in source
