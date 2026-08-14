from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from street_resolution_review import (
    ReviewDataError, alternatives_table, approve_cases_in_bulk, atomic_write_csv,
    batch_approval_preview, build_review_key,
    canonicalize_columns, export_alias_candidates, export_approved, filter_cases,
    load_audit, load_reviews, merge_reviews, review_metrics, save_decision,
    normalize_batch_result, stratified_sample, text_value,
)


def audit_frame() -> pd.DataFrame:
    return pd.DataFrame({
        "id": ["0001", "0002", "0003", "0004"],
        "nome_normalizado": ["ALFA", "BETA", "GAMA", "DELTA"],
        "via_original": ["Rua Alfa", "Rua Beta", "Rua Gama", "Rua Delta"],
        "latitude": ["-23.5", "-23.6", "-23.7", "-23.8"],
        "longitude": ["-46.6", "-46.7", "-46.8", "-46.9"],
        "de_original": ["A", "B", "C", "D"], "ate_original": ["E", "F", "G", "H"],
        "resolucao_atual": ["ATUAL A", "ATUAL B", "ATUAL C", "ATUAL D"],
        "candidato_recomendado": ["ALFA", "BETA", "GAMA", "DELTA"],
        "codlog_recomendado": ["000123", "000124", "000125", "000126"],
        "confianca": ["HIGH", "MEDIUM", "LOW", "UNRESOLVED"],
        "diverge_resolucao_atual": ["True", "true", "false", "1"],
        "street_requires_review": ["False", "1", "0", "False"],
        "route_requires_review": ["False", "False", "True", "False"],
        "score_final": ["98", "80", "50", "10"], "margem_top2": ["20", "10", "2", ""],
        "distance_m": ["2", "15", "100", ""],
        "alternativas_json": [json.dumps([{"street_name": "ALFA", "codlog": "000123", "final_score": 98}]), "not-json", "", ""],
    })


def canonical_audit() -> pd.DataFrame:
    return canonicalize_columns(audit_frame())


def test_load_utf8_sig_and_codlog_preserved(tmp_path):
    source = tmp_path / "audit.csv"
    audit_frame().to_csv(source, index=False, encoding="utf-8-sig")
    loaded = load_audit(source)
    assert loaded.loc[0, "codlog_recomendado"] == "000123"
    assert bool(loaded.loc[0, "diverge_resolucao_atual"]) is True
    assert bool(loaded.loc[2, "diverge_resolucao_atual"]) is False


def test_column_alias_and_missing_required_error():
    raw = audit_frame().rename(columns={"confianca": "street_confidence"})
    assert canonicalize_columns(raw).loc[0, "confianca"] == "HIGH"
    with pytest.raises(ReviewDataError, match="Campos encontrados"):
        canonicalize_columns(raw.drop(columns="id"))


def test_default_filter_is_high_divergence_ordered():
    filtered = filter_cases(canonical_audit())
    assert filtered["id"].tolist() == ["0001", "0002", "0004"]
    assert filter_cases(canonical_audit(), {"confidence": ["HIGH"]})["id"].tolist() == ["0001"]


def test_review_key_is_stable_when_column_order_changes():
    row = canonical_audit().iloc[0]
    assert build_review_key(row) == build_review_key(row[[*reversed(row.index)]])


def test_save_update_and_no_duplicate(tmp_path):
    review_path = tmp_path / "reviews.csv"
    case = canonical_audit().iloc[0]
    first = save_decision(case, {"decision": "APROVAR_RECOMENDACAO", "reviewed_by": "Ana"}, review_path)
    second = save_decision(case, {"decision": "MANTER_RESOLUCAO_ATUAL", "reviewed_by": "Bia"}, review_path)
    assert len(first) == 1
    assert len(second) == 1
    saved = load_reviews(review_path)
    assert saved.iloc[0]["decision"] == "MANTER_RESOLUCAO_ATUAL"
    assert saved.iloc[0]["manual_resolved_street"] == "ATUAL A"


def test_missing_review_file_and_merge(tmp_path):
    reviews = load_reviews(tmp_path / "absent.csv")
    merged = merge_reviews(canonical_audit(), reviews)
    assert reviews.empty
    assert merged["decision"].isna().all()


def test_alternatives_valid_and_invalid_json():
    assert len(alternatives_table(canonical_audit().iloc[0]["alternativas_json"])) == 1
    assert alternatives_table("not-json").empty


def test_approved_and_alias_exports(tmp_path):
    case = canonical_audit().iloc[0]
    reviews = save_decision(case, {"decision": "APROVAR_RECOMENDACAO", "approved_for_alias": True}, tmp_path / "reviews.csv")
    approved = export_approved(reviews, tmp_path / "approved.csv")
    aliases = export_alias_candidates(reviews, tmp_path / "aliases.csv")
    assert len(approved) == 1
    assert aliases.iloc[0]["source"] == "HUMAN_REVIEW"
    reviews.loc[0, "approved_for_alias"] = False
    assert export_alias_candidates(reviews, tmp_path / "empty_aliases.csv").empty


def test_sampling_is_deterministic():
    audit = canonical_audit()
    duplicated = pd.concat([audit.assign(id=lambda x: x.id + str(index)) for index in range(20)], ignore_index=True)
    duplicated["review_key"] = duplicated.apply(build_review_key, axis=1)
    sample_one = stratified_sample(duplicated, {"HIGH": 5, "MEDIUM": 4, "LOW": 2, "UNRESOLVED": 3}, seed=7)
    sample_two = stratified_sample(duplicated, {"HIGH": 5, "MEDIUM": 4, "LOW": 2, "UNRESOLVED": 3}, seed=7)
    assert sample_one["id"].tolist() == sample_two["id"].tolist()
    assert len(sample_one) == 12


def test_metrics_and_atomic_write(tmp_path):
    audit = merge_reviews(canonical_audit(), load_reviews(tmp_path / "absent.csv"))
    audit.loc[audit["id"] == "0001", "decision"] = "APROVAR_RECOMENDACAO"
    audit.loc[audit["id"] == "0001", "approved_for_alias"] = True
    metrics = review_metrics(audit)
    assert metrics["total_divergences"] == 3
    assert metrics["total_reviewed"] == 1
    assert metrics["aliases_marked"] == 1
    target = tmp_path / "atomic.csv"
    atomic_write_csv(pd.DataFrame({"a": [1]}), target)
    assert pd.read_csv(target).iloc[0, 0] == 1


def test_batch_approval_updates_selected_cases_without_duplicates(tmp_path):
    audit = canonical_audit()
    batch = audit[audit["id"].isin(["0001", "0002", "0004"])].copy()
    batch.loc[batch["id"] == "0002", "codlog_recomendado"] = pd.NA
    review_path = tmp_path / "reviews.csv"
    # Uma decisao anterior do caso alvo deve ser substituida; uma fora do filtro,
    # preservada.
    save_decision(audit.iloc[0], {"decision": "MANTER_RESOLUCAO_ATUAL"}, review_path)
    save_decision(audit.iloc[2], {"decision": "ADIAR_REVISAO"}, review_path)
    preview = batch_approval_preview(batch, include_unresolved=False)
    assert preview["changed"] == 1
    assert preview["skipped_missing_codlog"] == 1
    assert preview["skipped_unresolved"] == 1
    result = approve_cases_in_bulk(batch, include_unresolved=False, path=review_path)
    saved = load_reviews(review_path).set_index("id")
    assert set(result) == {"changed", "approved", "ignored", "unresolved", "elapsed_seconds"}
    assert result["changed"] == 1
    assert result["approved"] == 1
    assert result["ignored"] == 2
    assert result["unresolved"] == 0
    assert saved.loc["0001", "decision"] == "APROVAR_RECOMENDACAO"
    assert saved.loc["0001", "manual_resolved_street"] == "ALFA"
    assert saved.loc["0001", "manual_codlog"] == "000123"
    assert saved.loc["0001", "review_notes"] == "Aprovado em lote"
    assert saved.loc["0001", "reviewed_by"] == "batch"
    assert bool(saved.loc["0001", "approved_for_alias"]) is False
    assert saved.loc["0003", "decision"] == "ADIAR_REVISAO"
    assert len(saved) == 2


def test_batch_can_mark_unresolved_without_approving_recommendation(tmp_path):
    audit = canonical_audit()
    unresolved = audit[audit["id"] == "0004"]
    result = approve_cases_in_bulk(unresolved, include_unresolved=True, path=tmp_path / "reviews.csv")
    saved = load_reviews(tmp_path / "reviews.csv")
    assert result["approved"] == 0
    assert result["unresolved"] == 1
    assert saved.iloc[0]["decision"] == "MARCAR_COMO_NAO_RESOLVIDO"
    assert pd.isna(saved.iloc[0]["manual_resolved_street"])
    assert pd.isna(saved.iloc[0]["manual_codlog"])


def test_batch_result_contract_for_empty_operation(tmp_path):
    result = approve_cases_in_bulk(canonical_audit().iloc[0:0], path=tmp_path / "reviews.csv")
    assert set(result) == {"changed", "approved", "ignored", "unresolved", "elapsed_seconds"}
    assert result["changed"] == result["approved"] == result["ignored"] == result["unresolved"] == 0
    assert isinstance(result["elapsed_seconds"], float)


def test_normalize_batch_result_handles_legacy_and_partial_results():
    assert normalize_batch_result({"changed": 2, "approved": 1, "ignored": 0, "unresolved": 1, "elapsed_seconds": 0.2}) == {
        "changed": 2, "approved": 1, "ignored": 0, "unresolved": 1, "elapsed_seconds": 0.2,
    }
    assert normalize_batch_result(None) == {
        "changed": 0, "approved": 0, "ignored": 0, "unresolved": 0, "elapsed_seconds": 0.0,
    }
    # Resultado de versao anterior: propriedades calculadas nao existiam em __dict__.
    legacy = {"approved": 3, "marked_unresolved": 2, "skipped_missing_codlog": 1, "elapsed_seconds": "0.5"}
    assert normalize_batch_result(legacy) == {
        "changed": 5, "approved": 3, "ignored": 1, "unresolved": 2, "elapsed_seconds": 0.5,
    }
    assert normalize_batch_result({"changed": 4}) == {
        "changed": 4, "approved": 0, "ignored": 0, "unresolved": 0, "elapsed_seconds": 0.0,
    }
    assert normalize_batch_result({"approved": 4, "unresolved": 1}) == {
        "changed": 5, "approved": 4, "ignored": 0, "unresolved": 1, "elapsed_seconds": 0.0,
    }


def test_app_renders_legacy_batch_result_without_key_error():
    from streamlit.testing.v1 import AppTest

    app_path = Path(__file__).resolve().parents[1] / "src" / "street_resolution_review_app.py"
    app = AppTest.from_file(str(app_path))
    app.session_state["batch_approval_result"] = {"approved": 2, "marked_unresolved": 1}
    app.run(timeout=30)
    assert not app.exception


def test_missing_values_do_not_render_nan():
    assert text_value(float("nan")) == "—"
    assert text_value(pd.NA) == "—"
