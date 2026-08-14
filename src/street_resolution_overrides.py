"""Integração isolada das decisões humanas de resolução de logradouros.

O módulo não conhece o ETL nem chama ``RoadGraph.route``. Ele transforma o CSV
de revisão em decisões validadas que o ETL pode consultar por registro.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

import pandas as pd


PROJECT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_REVIEW_PATH = PROJECT_DIR / "data" / "processed" / "street_resolution_human_review.csv"
DEFAULT_APPROVED_PATH = PROJECT_DIR / "data" / "processed" / "street_resolution_approved.csv"
DEFAULT_REPORT_PATH = PROJECT_DIR / "data" / "processed" / "street_resolution_override_report.json"
DEFAULT_ERRORS_PATH = PROJECT_DIR / "data" / "processed" / "street_resolution_override_errors.csv"
APPLICABLE_DECISIONS = {"APROVAR_RECOMENDACAO", "ESCOLHER_OUTRO_CANDIDATO", "MARCAR_COMO_NAO_RESOLVIDO"}
VALID_DECISIONS = APPLICABLE_DECISIONS | {"MANTER_RESOLUCAO_ATUAL", "ADIAR_REVISAO"}
ERROR_COLUMNS = ["review_key", "id", "decisao", "rua", "CODLOG", "motivo", "acao_tomada"]


def _text(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    text = str(value).strip()
    return "" if text.casefold() in {"", "nan", "none", "null", "<na>"} else text


def _key_piece(value: Any) -> str:
    return _text(value).casefold()


def review_key_for_record(row: Mapping[str, Any]) -> str:
    """Replica a chave da camada de revisão para localizar o recape exato."""
    name = row.get("nome_normalizado", row.get("rua_norm", row.get("rua_raw", "")))
    payload = "|".join(_key_piece(row.get(field, "")) for field in (
        "id", "nome_normalizado", "latitude", "longitude", "de_original", "ate_original",
    ))
    if not _text(row.get("nome_normalizado", "")):
        payload = "|".join(_key_piece(value) for value in (
            row.get("id", ""), name, row.get("latitude", ""), row.get("longitude", ""),
            row.get("de", ""), row.get("ate", ""),
        ))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _atomic_csv(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8-sig", newline="", dir=path.parent, suffix=".tmp", delete=False) as stream:
            temporary = stream.name
            frame.to_csv(stream, index=False)
        os.replace(temporary, path)
    finally:
        if temporary and Path(temporary).exists():
            Path(temporary).unlink(missing_ok=True)


@dataclass(frozen=True)
class StreetResolutionOverride:
    review_key: str
    recape_id: str
    decision: str
    resolved_street: str | None
    resolved_codlog: str | None
    source: str
    reviewed_at: str | None
    reviewed_by: str | None
    valid: bool
    validation_reason: str
    applicable: bool = False
    block_fuzzy: bool = False


class HumanReviewOverrides:
    """Índice validado de decisões humanas, por chave e por ID único."""

    def __init__(self, reviews: pd.DataFrame, graph: Any, normalizer: Callable[[str], str], source_path: Path):
        self.source_path = source_path
        self.graph = graph
        self.normalizer = normalizer
        self.total_reviews_loaded = len(reviews)
        self.duplicate_review_keys = int(reviews["review_key"].duplicated(keep=False).sum()) if "review_key" in reviews else 0
        self.invalid_streets = 0
        self.invalid_codlogs = 0
        self.street_codlog_mismatches = 0
        self._errors: list[dict[str, Any]] = []
        self._by_key: dict[str, StreetResolutionOverride] = {}
        self._by_id: dict[str, StreetResolutionOverride] = {}
        self._ambiguous_ids: set[str] = set()
        self.overrides_applied = 0
        self.overrides_not_found = 0
        self._build(reviews)

    @classmethod
    def load(
        cls,
        graph: Any,
        normalizer: Callable[[str], str],
        review_path: Path | str = DEFAULT_REVIEW_PATH,
        approved_path: Path | str = DEFAULT_APPROVED_PATH,
    ) -> "HumanReviewOverrides":
        source = Path(review_path)
        if not source.exists():
            source = Path(approved_path)
        if source.exists():
            reviews = pd.read_csv(source, encoding="utf-8-sig", dtype=str, keep_default_na=True, na_values=[""])
        else:
            reviews = pd.DataFrame(columns=["review_key", "id", "decision"])
        return cls(reviews, graph, normalizer, source)

    @property
    def errors(self) -> list[dict[str, Any]]:
        return list(self._errors)

    def _error(self, row: Mapping[str, Any], reason: str, action: str = "FALLBACK_ATUAL") -> None:
        street = _text(row.get("manual_resolved_street")) or _text(row.get("candidato_recomendado"))
        codlog = _text(row.get("manual_codlog")) or _text(row.get("codlog_recomendado"))
        self._errors.append({
            "review_key": _text(row.get("review_key")), "id": _text(row.get("id")),
            "decisao": _text(row.get("decision")), "rua": street, "CODLOG": codlog,
            "motivo": reason, "acao_tomada": action,
        })

    def _build(self, reviews: pd.DataFrame) -> None:
        if reviews.empty:
            return
        work = reviews.copy()
        for column in ("review_key", "id", "decision", "manual_resolved_street", "manual_codlog", "candidato_recomendado", "codlog_recomendado", "reviewed_at", "reviewed_by"):
            if column not in work:
                work[column] = pd.NA
        work["_reviewed_at_sort"] = pd.to_datetime(work["reviewed_at"], errors="coerce", utc=True)
        work = work.sort_values(["_reviewed_at_sort"], na_position="first", kind="stable")
        for _, row in work.iterrows():
            override = self._validate(row)
            key = _text(row.get("review_key"))
            if not key:
                key = review_key_for_record(row)
            self._by_key[key] = override
        for override in self._by_key.values():
            if override.recape_id:
                if override.recape_id in self._by_id:
                    self._ambiguous_ids.add(override.recape_id)
                    self._by_id.pop(override.recape_id, None)
                elif override.recape_id not in self._ambiguous_ids:
                    self._by_id[override.recape_id] = override

    def _validate(self, row: Mapping[str, Any]) -> StreetResolutionOverride:
        key = _text(row.get("review_key")) or review_key_for_record(row)
        recape_id = _text(row.get("id"))
        decision = _text(row.get("decision")).upper()
        reviewed_at = _text(row.get("reviewed_at")) or None
        reviewed_by = _text(row.get("reviewed_by")) or None
        if decision not in VALID_DECISIONS:
            reason = "Decisão humana vazia ou inválida"
            self._error(row, reason)
            return StreetResolutionOverride(key, recape_id, decision, None, None, "HUMAN_REVIEW", reviewed_at, reviewed_by, False, reason)
        if decision == "ADIAR_REVISAO":
            return StreetResolutionOverride(key, recape_id, decision, None, None, "HUMAN_REVIEW", reviewed_at, reviewed_by, True, "ADIAR_REVISAO; fallback atual", False)
        if decision == "MANTER_RESOLUCAO_ATUAL":
            return StreetResolutionOverride(key, recape_id, decision, None, None, "HUMAN_REVIEW", reviewed_at, reviewed_by, True, "MANTER_RESOLUCAO_ATUAL; fallback atual", False)
        if decision == "MARCAR_COMO_NAO_RESOLVIDO":
            return StreetResolutionOverride(key, recape_id, decision, None, None, "HUMAN_REVIEW", reviewed_at, reviewed_by, True, "HUMAN_UNRESOLVED; fuzzy bloqueado", True, True)

        if decision == "ESCOLHER_OUTRO_CANDIDATO":
            street_raw = _text(row.get("manual_resolved_street"))
        else:
            street_raw = _text(row.get("manual_resolved_street")) or _text(row.get("candidato_recomendado"))
        codlog = _text(row.get("manual_codlog")) or _text(row.get("codlog_recomendado"))
        street = self.normalizer(street_raw) if street_raw else ""
        street_index = getattr(self.graph, "street_segments", {}) or {}
        codlog_index = getattr(self.graph, "codlog_to_street", {}) or {}
        if not street or street not in street_index:
            self.invalid_streets += 1
            reason = f"Rua inexistente no índice GeoSampa: {street_raw or '(vazia)'}"
            self._error(row, reason)
            return StreetResolutionOverride(key, recape_id, decision, street or None, codlog or None, "HUMAN_REVIEW", reviewed_at, reviewed_by, False, reason)
        if codlog:
            if codlog not in codlog_index:
                self.invalid_codlogs += 1
                reason = f"CODLOG inexistente no índice GeoSampa: {codlog}"
                self._error(row, reason)
                return StreetResolutionOverride(key, recape_id, decision, street, codlog, "HUMAN_REVIEW", reviewed_at, reviewed_by, False, reason)
            reason = "Rua e CODLOG validados"
            if self.normalizer(str(codlog_index[codlog])) != street:
                self.street_codlog_mismatches += 1
                reason = "Nome da rua e CODLOG apontam para vias diferentes"
                self._error(row, reason)
                return StreetResolutionOverride(key, recape_id, decision, street, codlog, "HUMAN_REVIEW", reviewed_at, reviewed_by, False, reason)
        else:
            compatible = sorted({str(value) for value, target in codlog_index.items() if self.normalizer(str(target)) == street and _text(value)})
            reason = "Rua validada; CODLOG ausente e não inequívoco" if len(compatible) != 1 else "Rua validada; CODLOG inequívoco recuperado"
            if len(compatible) == 1:
                codlog = compatible[0]
        return StreetResolutionOverride(key, recape_id, decision, street, codlog or None, "HUMAN_REVIEW", reviewed_at, reviewed_by, True, reason, True)

    def for_record(self, row: Mapping[str, Any]) -> StreetResolutionOverride | None:
        key = review_key_for_record(row)
        override = self._by_key.get(key)
        if override is None:
            recape_id = _text(row.get("id"))
            override = None if recape_id in self._ambiguous_ids else self._by_id.get(recape_id)
        if override is None:
            self.overrides_not_found += 1
        return override

    def mark_applied(self) -> None:
        self.overrides_applied += 1

    def report(self, **extra: Any) -> dict[str, Any]:
        decisions = {"APROVAR_RECOMENDACAO": 0, "ESCOLHER_OUTRO_CANDIDATO": 0, "MANTER_RESOLUCAO_ATUAL": 0, "MARCAR_COMO_NAO_RESOLVIDO": 0, "ADIAR_REVISAO": 0}
        for override in self._by_key.values():
            if override.decision in decisions:
                decisions[override.decision] += 1
        return {
            "source_path": str(self.source_path), "total_reviews_loaded": self.total_reviews_loaded,
            "approved_recommendations": decisions["APROVAR_RECOMENDACAO"],
            "chosen_candidates": decisions["ESCOLHER_OUTRO_CANDIDATO"], "keep_current": decisions["MANTER_RESOLUCAO_ATUAL"],
            "human_unresolved": decisions["MARCAR_COMO_NAO_RESOLVIDO"], "deferred": decisions["ADIAR_REVISAO"],
            "valid_overrides": int(sum(override.valid for override in self._by_key.values())),
            "invalid_overrides": int(sum(not override.valid for override in self._by_key.values())),
            "overrides_applied": self.overrides_applied, "overrides_not_found": self.overrides_not_found,
            "duplicate_review_keys": self.duplicate_review_keys, "invalid_streets": self.invalid_streets,
            "invalid_codlogs": self.invalid_codlogs, "street_codlog_mismatches": self.street_codlog_mismatches,
            **extra,
        }

    def write_report(self, report_path: Path | str = DEFAULT_REPORT_PATH, errors_path: Path | str = DEFAULT_ERRORS_PATH, **extra: Any) -> dict[str, Any]:
        report = self.report(**extra)
        target = Path(report_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, target)
        errors = pd.DataFrame(self._errors, columns=ERROR_COLUMNS)
        _atomic_csv(errors, Path(errors_path))
        return report


def load_human_review_overrides(graph: Any, normalizer: Callable[[str], str], review_path: Path | str = DEFAULT_REVIEW_PATH) -> HumanReviewOverrides:
    return HumanReviewOverrides.load(graph, normalizer, review_path=review_path)
