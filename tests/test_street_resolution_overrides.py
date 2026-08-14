from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from street_resolution_overrides import HumanReviewOverrides, review_key_for_record


class FakeGraph:
    street_segments = {"ALFA": ["a"], "BETA": ["b"], "GAMMA": ["c"]}
    codlog_to_street = {"0001": "ALFA", "0002": "BETA", "0003": "ALFA"}


def record(case_id: str = "1", name: str = "ALFA") -> dict[str, object]:
    return {
        "id": case_id, "nome_normalizado": name, "latitude": "-23.5", "longitude": "-46.6",
        "de": "RUA DE", "ate": "RUA ATE",
    }


def review_row(case: dict[str, object], decision: str, **values: object) -> dict[str, object]:
    row = {"review_key": review_key_for_record(case), "id": case["id"], "decision": decision,
           "reviewed_at": "2026-08-05T10:00:00+00:00", "reviewed_by": "batch"}
    row.update(values)
    return row


def write_reviews(path: Path, rows: list[dict[str, object]]) -> None:
    pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8-sig")


def test_valid_approval_and_fallback_candidate(tmp_path):
    first, second = record("1"), record("2", "BETA")
    rows = [
        review_row(first, "APROVAR_RECOMENDACAO", manual_resolved_street="ALFA", manual_codlog="0001"),
        review_row(second, "APROVAR_RECOMENDACAO", candidato_recomendado="BETA", codlog_recomendado="0002"),
    ]
    path = tmp_path / "review.csv"
    write_reviews(path, rows)
    index = HumanReviewOverrides.load(FakeGraph(), str.upper, path)
    assert index.for_record(first).valid is True
    assert index.for_record(first).resolved_codlog == "0001"
    assert index.for_record(second).resolved_street == "BETA"
    assert index.for_record(second).resolved_codlog == "0002"


def test_choose_other_requires_manual_street_and_keep_current(tmp_path):
    chosen, kept = record("1"), record("2")
    path = tmp_path / "review.csv"
    write_reviews(path, [
        review_row(chosen, "ESCOLHER_OUTRO_CANDIDATO", manual_resolved_street="BETA", manual_codlog="0002"),
        review_row(kept, "MANTER_RESOLUCAO_ATUAL"),
    ])
    index = HumanReviewOverrides.load(FakeGraph(), str.upper, path)
    assert index.for_record(chosen).resolved_street == "BETA"
    assert index.for_record(chosen).applicable is True
    assert index.for_record(kept).applicable is False


def test_unresolved_blocks_fuzzy_and_deferred_falls_back(tmp_path):
    unresolved, deferred = record("1"), record("2")
    path = tmp_path / "review.csv"
    write_reviews(path, [review_row(unresolved, "MARCAR_COMO_NAO_RESOLVIDO"), review_row(deferred, "ADIAR_REVISAO")])
    index = HumanReviewOverrides.load(FakeGraph(), str.upper, path)
    human_unresolved = index.for_record(unresolved)
    assert human_unresolved.block_fuzzy is True
    assert human_unresolved.applicable is True
    assert index.for_record(deferred).applicable is False


def test_invalid_street_codlog_and_mismatch_are_reported(tmp_path):
    path = tmp_path / "review.csv"
    write_reviews(path, [
        review_row(record("1"), "APROVAR_RECOMENDACAO", manual_resolved_street="INEXISTENTE", manual_codlog="0001"),
        review_row(record("2"), "APROVAR_RECOMENDACAO", manual_resolved_street="ALFA", manual_codlog="9999"),
        review_row(record("3"), "APROVAR_RECOMENDACAO", manual_resolved_street="ALFA", manual_codlog="0002"),
    ])
    index = HumanReviewOverrides.load(FakeGraph(), str.upper, path)
    assert sum(item.valid for item in (index.for_record(record("1")), index.for_record(record("2")), index.for_record(record("3")))) == 0
    report = index.write_report(tmp_path / "report.json", tmp_path / "errors.csv")
    assert report["invalid_overrides"] == 3
    assert report["invalid_streets"] == 1
    assert report["invalid_codlogs"] == 1
    assert report["street_codlog_mismatches"] == 1
    assert len(pd.read_csv(tmp_path / "errors.csv", encoding="utf-8-sig")) == 3


def test_missing_codlog_unique_and_ambiguous(tmp_path):
    unique, ambiguous = record("1"), record("2")
    path = tmp_path / "review.csv"
    write_reviews(path, [
        review_row(unique, "APROVAR_RECOMENDACAO", manual_resolved_street="BETA"),
        review_row(ambiguous, "APROVAR_RECOMENDACAO", manual_resolved_street="ALFA"),
    ])
    index = HumanReviewOverrides.load(FakeGraph(), str.upper, path)
    assert index.for_record(unique).resolved_codlog == "0002"
    assert index.for_record(ambiguous).resolved_codlog is None
    assert index.for_record(ambiguous).valid is True


def test_exact_key_id_fallback_duplicates_and_missing_file(tmp_path):
    first, duplicate = record("1"), record("1", "BETA")
    path = tmp_path / "review.csv"
    write_reviews(path, [review_row(first, "APROVAR_RECOMENDACAO", manual_resolved_street="ALFA", manual_codlog="0001"),
                         review_row(duplicate, "APROVAR_RECOMENDACAO", manual_resolved_street="BETA", manual_codlog="0002")])
    index = HumanReviewOverrides.load(FakeGraph(), str.upper, path)
    assert index.for_record(record("1", "GAMMA")) is None
    assert index.overrides_not_found == 1
    missing = HumanReviewOverrides.load(FakeGraph(), str.upper, tmp_path / "absent.csv")
    assert missing.total_reviews_loaded == 0
    assert missing.for_record(record("9")) is None
