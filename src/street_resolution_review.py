"""Camada de dados para a revisao humana do Street Resolution Engine.

Este modulo somente le o relatorio de auditoria ja gerado. Ele nao importa o
resolver, RoadGraph ou o ETL e nunca altera os arquivos de origem.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_AUDIT_PATH = ROOT / "data" / "processed" / "street_resolution_audit.csv"
DEFAULT_REVIEW_PATH = ROOT / "data" / "processed" / "street_resolution_human_review.csv"
DEFAULT_APPROVED_PATH = ROOT / "data" / "processed" / "street_resolution_approved.csv"
DEFAULT_ALIAS_PATH = ROOT / "data" / "processed" / "street_alias_candidates.csv"
DEFAULT_REPORT_PATH = ROOT / "data" / "processed" / "street_resolution_human_review_report.json"

DECISIONS = (
    "APROVAR_RECOMENDACAO",
    "MANTER_RESOLUCAO_ATUAL",
    "ESCOLHER_OUTRO_CANDIDATO",
    "MARCAR_COMO_NAO_RESOLVIDO",
    "ADIAR_REVISAO",
)
REVIEW_COLUMNS = [
    "review_key", "id", "original_norm", "resolucao_atual", "candidato_recomendado",
    "codlog_recomendado", "confianca", "score_final", "distance_m", "decision",
    "manual_resolved_street", "manual_codlog", "review_notes", "approved_for_alias",
    "reviewed_at", "reviewed_by",
]

# A ordem permite ler relatorios de versoes anteriores sem perder as colunas
# originais: as canonicas sao apenas copias de conveniencia no DataFrame em memoria.
COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "id": ("id", "ID", "case_id", "registro_id"),
    "numero_processo": ("numero_processo", "processo", "numero_process"),
    "via_original": ("via_original", "logradouro_original", "street_original"),
    "logradouro_geosampa_original": ("logradouro_geosampa_original",),
    "nome_normalizado": ("nome_normalizado", "original_norm", "street_norm"),
    "codlog_informado": ("codlog_informado", "codlog"),
    "latitude": ("latitude", "lat"),
    "longitude": ("longitude", "lon", "lng"),
    "de_original": ("de_original", "de", "from_original"),
    "ate_original": ("ate_original", "ate", "to_original"),
    "resolucao_atual": ("resolucao_atual", "resolved_street", "rua_atual"),
    "metodo_atual": ("metodo_atual", "current_method"),
    "score_atual": ("score_atual", "current_score"),
    "candidato_recomendado": ("candidato_recomendado", "recommended_candidate", "resolved_norm"),
    "codlog_recomendado": ("codlog_recomendado", "recommended_codlog"),
    "metodo_recomendado": ("metodo_recomendado", "recommended_method"),
    "confianca": ("street_confidence", "confianca", "confidence"),
    "street_requires_review": ("street_requires_review", "requer_revisao"),
    "street_review_reasons": ("street_review_reasons", "motivos_revisao"),
    "score_final": ("score_final", "final_score"),
    "margem_top2": ("margem_top2", "top2_margin"),
    "distance_m": ("distance_m", "distancia_m", "distance"),
    "token_coverage": ("token_coverage", "cobertura_tokens"),
    "route_context_status": ("route_context_status", "status_contexto_rota"),
    "route_requires_review": ("route_requires_review", "requer_revisao_rota"),
    "route_review_reasons": ("route_review_reasons", "motivos_revisao_rota"),
    "diverge_resolucao_atual": ("diverge_resolucao_atual", "diverge", "resolution_diverges"),
    "alternativas_json": ("alternativas_json", "alternatives_json"),
    "de_resolution_status": ("de_resolution_status",),
    "ate_resolution_status": ("ate_resolution_status",),
    "de_candidate": ("de_candidate",),
    "ate_candidate": ("ate_candidate",),
    "de_intersection_status": ("de_intersection_status",),
    "ate_intersection_status": ("ate_intersection_status",),
}
REQUIRED_COLUMNS = ("id", "resolucao_atual", "candidato_recomendado", "confianca", "diverge_resolucao_atual")
BOOL_COLUMNS = ("diverge_resolucao_atual", "street_requires_review", "route_requires_review")
NUMERIC_COLUMNS = ("latitude", "longitude", "score_atual", "score_final", "margem_top2", "distance_m", "token_coverage")
CONFIDENCE_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "UNRESOLVED": 3}


class ReviewDataError(ValueError):
    """Dados de auditoria insuficientes para a tela de revisao."""


class ReviewPersistenceError(RuntimeError):
    """A decisao nao foi substituida com seguranca no arquivo de revisao."""


BATCH_RESULT_FIELDS = ("changed", "approved", "ignored", "unresolved", "elapsed_seconds")


def _empty_to_na(series: pd.Series) -> pd.Series:
    empty = series.notna() & series.astype(str).str.strip().eq("")
    return series.mask(empty, pd.NA)


def parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if pd.isna(value):
        return False
    return str(value).strip().casefold() in {"true", "1", "yes", "sim", "y", "t"}


def _result_number(value: Any, cast: type[int] | type[float]) -> int | float:
    """Converte resultados legados sem deixar dados ruins quebrarem a interface."""
    try:
        return cast(value)
    except (TypeError, ValueError):
        return cast(0)


def normalize_batch_result(result: Mapping[str, Any] | None) -> dict[str, int | float]:
    """Retorna o contrato unico da aprovacao em lote, inclusive para sessao legada.

    As chaves antigas sao reconhecidas somente para exibir corretamente uma
    operacao ja concluida por versoes anteriores. O retorno sempre contem
    exatamente as cinco chaves publicas atuais.
    """
    raw = result if isinstance(result, Mapping) else {}
    approved = int(_result_number(raw.get("approved", 0), int))
    unresolved = int(_result_number(raw.get("unresolved", raw.get("marked_unresolved", 0)), int))
    ignored_default = sum(int(_result_number(raw.get(key, 0), int)) for key in (
        "skipped_missing_candidate", "skipped_missing_codlog", "skipped_unresolved",
    ))
    return {
        "changed": int(_result_number(raw.get("changed", approved + unresolved), int)),
        "approved": approved,
        "ignored": int(_result_number(raw.get("ignored", ignored_default), int)),
        "unresolved": unresolved,
        "elapsed_seconds": float(_result_number(raw.get("elapsed_seconds", 0), float)),
    }


def text_value(value: Any, default: str = "—") -> str:
    """Apresenta vazios sem vazar o texto tecnico 'nan' para a interface."""
    if value is None or pd.isna(value):
        return default
    rendered = str(value).strip()
    return rendered if rendered and rendered.casefold() != "nan" else default


def canonicalize_columns(frame: pd.DataFrame, required: Iterable[str] = REQUIRED_COLUMNS) -> pd.DataFrame:
    """Acrescenta colunas canonicas a uma copia do CSV, preservando as originais."""
    result = frame.copy()
    found = set(frame.columns)
    missing: list[str] = []
    for canonical, aliases in COLUMN_ALIASES.items():
        source = next((name for name in aliases if name in found), None)
        if source is None:
            if canonical in required:
                missing.append(f"{canonical} ({' ou '.join(aliases)})")
            result[canonical] = pd.NA
        else:
            result[canonical] = frame[source]
    if missing:
        available = ", ".join(map(str, frame.columns))
        raise ReviewDataError(f"Colunas obrigatorias ausentes: {', '.join(missing)}. Campos encontrados: {available}")
    for name in BOOL_COLUMNS:
        result[name] = result[name].map(parse_bool)
    for name in NUMERIC_COLUMNS:
        result[name] = pd.to_numeric(result[name], errors="coerce")
    for name in result.columns:
        if result[name].dtype == object or pd.api.types.is_string_dtype(result[name]):
            result[name] = _empty_to_na(result[name])
    result["confianca"] = result["confianca"].fillna("UNRESOLVED").astype(str).str.upper()
    result["review_key"] = result.apply(build_review_key, axis=1)
    return result


def load_audit(path: Path | str = DEFAULT_AUDIT_PATH) -> pd.DataFrame:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Arquivo de auditoria nao encontrado: {path}")
    frame = pd.read_csv(path, encoding="utf-8-sig", dtype=str, keep_default_na=True, na_values=[""])
    return canonicalize_columns(frame)


def _key_piece(value: Any) -> str:
    return "" if value is None or pd.isna(value) else str(value).strip().casefold()


def build_review_key(row: Mapping[str, Any] | pd.Series) -> str:
    """Chave deterministica, independente da ordem do CSV."""
    fields = ("id", "nome_normalizado", "latitude", "longitude", "de_original", "ate_original")
    payload = "|".join(_key_piece(row.get(field, "")) for field in fields)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def load_reviews(path: Path | str = DEFAULT_REVIEW_PATH) -> pd.DataFrame:
    path = Path(path)
    if not path.exists():
        return pd.DataFrame(columns=REVIEW_COLUMNS)
    frame = pd.read_csv(path, encoding="utf-8-sig", dtype=str, keep_default_na=True, na_values=[""])
    for column in REVIEW_COLUMNS:
        if column not in frame:
            frame[column] = pd.NA
    frame["approved_for_alias"] = frame["approved_for_alias"].map(parse_bool)
    return frame[REVIEW_COLUMNS].drop_duplicates("review_key", keep="last")


def merge_reviews(audit: pd.DataFrame, reviews: pd.DataFrame) -> pd.DataFrame:
    right = reviews.drop_duplicates("review_key", keep="last")
    review_fields = [column for column in REVIEW_COLUMNS if column != "review_key"]
    return audit.merge(right[["review_key", *review_fields]], on="review_key", how="left", suffixes=("", "_review"))


def ordered_cases(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    result["_confidence_order"] = result["confianca"].map(CONFIDENCE_ORDER).fillna(4)
    for col in ("score_final", "margem_top2", "distance_m"):
        result[col] = pd.to_numeric(result[col], errors="coerce")
    return result.sort_values(
        ["_confidence_order", "score_final", "margem_top2", "distance_m"],
        ascending=[True, False, False, True], na_position="last", kind="stable",
    ).drop(columns="_confidence_order")


def filter_cases(frame: pd.DataFrame, filters: Mapping[str, Any] | None = None) -> pd.DataFrame:
    """Aplica filtros combinaveis; sem filtro, conserva somente divergencias."""
    filters = dict(filters or {})
    result = frame.copy()
    divergence = filters.get("divergent", True)
    if divergence is not None:
        result = result[result["diverge_resolucao_atual"] == bool(divergence)]
    for column, values in (("confianca", filters.get("confidence")), ("metodo_atual", filters.get("current_method")),
                           ("metodo_recomendado", filters.get("recommended_method"))):
        if values:
            selected = {str(item) for item in values}
            result = result[result[column].astype(str).isin(selected)]
    for column, flag in (("street_requires_review", filters.get("street_review")), ("route_requires_review", filters.get("route_review"))):
        if flag is not None:
            result = result[result[column] == bool(flag)]
    for column, bounds in (("score_final", filters.get("score_range")), ("distance_m", filters.get("distance_range")),
                           ("margem_top2", filters.get("margin_range"))):
        if bounds is not None:
            low, high = bounds
            numeric = pd.to_numeric(result[column], errors="coerce")
            result = result[numeric.between(low, high, inclusive="both")]
    if filters.get("incomplete") is not None:
        values = result["alternativas_json"].map(lambda raw: any(bool(item.get("incomplete")) for item in parse_alternatives(raw)))
        result = result[values == bool(filters["incomplete"])]
    if filters.get("route_context"):
        result = result[result["route_context_status"].astype(str).isin(set(filters["route_context"]))]
    if filters.get("review_reason"):
        needle = str(filters["review_reason"]).casefold()
        reasons = result["street_review_reasons"].fillna("").astype(str) + " " + result["route_review_reasons"].fillna("").astype(str)
        result = result[reasons.str.casefold().str.contains(needle, regex=False)]
    if filters.get("decision"):
        decision = filters["decision"]
        if decision == "PENDENTE":
            result = result[result["decision"].isna() | (result["decision"].astype(str).str.strip() == "")]
        else:
            result = result[result["decision"] == decision]
    for column, needle in (("id", filters.get("id")), ("via_original", filters.get("original")),
                           ("resolucao_atual", filters.get("current")), ("candidato_recomendado", filters.get("recommended")),
                           ("codlog_informado", filters.get("codlog")), ("nome_normalizado", filters.get("free_text"))):
        if needle:
            if column == "nome_normalizado":
                searchable = result.astype(str).apply(" ".join, axis=1)
            else:
                searchable = result[column].fillna("").astype(str)
            result = result[searchable.str.casefold().str.contains(str(needle).casefold(), regex=False)]
    return ordered_cases(result)


def parse_alternatives(raw: Any) -> list[dict[str, Any]]:
    if raw is None or pd.isna(raw) or not str(raw).strip():
        return []
    try:
        payload = json.loads(str(raw))
    except (TypeError, json.JSONDecodeError):
        return []
    if isinstance(payload, dict):
        payload = payload.get("alternatives", [payload])
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def alternatives_table(raw: Any) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for position, item in enumerate(parse_alternatives(raw), start=1):
        rows.append({
            "posicao": position, "nome": item.get("street_name") or item.get("street_norm"),
            "CODLOG": item.get("codlog") or ", ".join(map(str, item.get("codlogs", []))),
            "score_final": item.get("final_score"), "score_lexical": item.get("lexical_score"),
            "distancia_m": item.get("distance_m"), "cobertura_tokens": item.get("token_coverage"),
            "tokens_ausentes": ", ".join(map(str, item.get("missing_tokens", []))),
            "tokens_extras": ", ".join(map(str, item.get("extra_tokens", []))),
            "intersecao_de": item.get("intersects_de"), "intersecao_ate": item.get("intersects_ate"),
            "segmentos": item.get("segment_count"), "evidencias": "; ".join(map(str, item.get("evidence", []))),
        })
    return pd.DataFrame(rows)


def _review_record(case: Mapping[str, Any], decision: Mapping[str, Any]) -> dict[str, Any]:
    record = {column: pd.NA for column in REVIEW_COLUMNS}
    for name in ("review_key", "id", "resolucao_atual", "candidato_recomendado", "codlog_recomendado", "confianca", "score_final", "distance_m"):
        record[name] = case.get(name, pd.NA)
    record["original_norm"] = case.get("nome_normalizado", pd.NA)
    record.update({name: decision.get(name, record.get(name, pd.NA)) for name in REVIEW_COLUMNS})
    record["approved_for_alias"] = parse_bool(record["approved_for_alias"])
    return record


def validate_decision(case: Mapping[str, Any], decision: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(decision)
    kind = result.get("decision")
    if kind not in DECISIONS:
        raise ReviewDataError("Selecione uma decisao humana valida.")
    notes = text_value(result.get("review_notes"), default="")
    if kind == "APROVAR_RECOMENDACAO":
        result["manual_resolved_street"] = case.get("candidato_recomendado")
        result["manual_codlog"] = case.get("codlog_recomendado")
    elif kind == "MANTER_RESOLUCAO_ATUAL":
        result["manual_resolved_street"] = case.get("resolucao_atual")
    elif kind == "ESCOLHER_OUTRO_CANDIDATO" and not notes:
        raise ReviewDataError("Escolher outro candidato exige uma nota de revisao.")
    elif kind == "MARCAR_COMO_NAO_RESOLVIDO":
        if not notes:
            raise ReviewDataError("Marcar como nao resolvido exige uma justificativa.")
        result["manual_resolved_street"] = pd.NA
        result["manual_codlog"] = pd.NA
    result["review_notes"] = notes or pd.NA
    return result


def atomic_write_csv(frame: pd.DataFrame, path: Path | str) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8-sig", newline="", suffix=".tmp", dir=path.parent, delete=False) as handle:
            temp_name = handle.name
            frame.to_csv(handle, index=False)
        os.replace(temp_name, path)
        return path
    except PermissionError as exc:
        fallback = path.with_name(f"{path.stem}.pending.{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')}{path.suffix}")
        if temp_name and Path(temp_name).exists():
            os.replace(temp_name, fallback)
        raise ReviewPersistenceError(f"Nao foi possivel substituir {path.name}; o arquivo original foi preservado. Feche o arquivo se estiver bloqueado por outro processo e use {fallback.name} para recuperar a revisao.") from exc
    finally:
        if temp_name and Path(temp_name).exists():
            Path(temp_name).unlink(missing_ok=True)


def save_decision(case: Mapping[str, Any], decision: Mapping[str, Any], path: Path | str = DEFAULT_REVIEW_PATH) -> pd.DataFrame:
    validated = validate_decision(case, decision)
    record = _review_record(case, validated)
    existing = load_reviews(path)
    previous = existing[existing["review_key"] != record["review_key"]]
    new_record = pd.DataFrame([record])
    combined = new_record if previous.empty else pd.concat([previous, new_record], ignore_index=True)
    combined = combined[REVIEW_COLUMNS].drop_duplicates("review_key", keep="last")
    atomic_write_csv(combined, path)
    return combined


def batch_approval_preview(cases: pd.DataFrame, include_unresolved: bool = False) -> dict[str, Any]:
    """Calcula a operacao sem persistir alteracoes, para a tela de confirmacao."""
    eligible_by_confidence: dict[str, int] = {}
    skipped_missing_candidate = 0
    skipped_missing_codlog = 0
    skipped_unresolved = 0
    approved = 0
    marked_unresolved = 0
    for _, case in cases.iterrows():
        confidence = text_value(case.get("confianca"), "UNRESOLVED").upper()
        if confidence == "UNRESOLVED":
            if include_unresolved:
                marked_unresolved += 1
                eligible_by_confidence[confidence] = eligible_by_confidence.get(confidence, 0) + 1
            else:
                skipped_unresolved += 1
            continue
        if not text_value(case.get("candidato_recomendado"), ""):
            skipped_missing_candidate += 1
            continue
        if not text_value(case.get("codlog_recomendado"), ""):
            skipped_missing_codlog += 1
            continue
        approved += 1
        eligible_by_confidence[confidence] = eligible_by_confidence.get(confidence, 0) + 1
    return {
        "approved": approved,
        "marked_unresolved": marked_unresolved,
        "changed": approved + marked_unresolved,
        "ignored": skipped_missing_candidate + skipped_missing_codlog + skipped_unresolved,
        "skipped_missing_candidate": skipped_missing_candidate,
        "skipped_missing_codlog": skipped_missing_codlog,
        "skipped_unresolved": skipped_unresolved,
        "by_confidence": eligible_by_confidence,
    }


def approve_cases_in_bulk(cases: pd.DataFrame, include_unresolved: bool = False, path: Path | str = DEFAULT_REVIEW_PATH) -> dict[str, int | float]:
    """Salva uma decisao por caso selecionado, em uma unica escrita atomica.

    Revisoes de casos fora do filtro sao preservadas. Casos selecionados recebem a
    decisao do lote mesmo que ja tenham uma decisao humana anterior, pois a
    confirmacao explicita representa uma nova decisao para aquele filtro.
    """
    started = time.perf_counter()
    preview = batch_approval_preview(cases, include_unresolved)
    timestamp = datetime.now(timezone.utc).isoformat()
    records: list[dict[str, Any]] = []
    for _, case in cases.iterrows():
        confidence = text_value(case.get("confianca"), "UNRESOLVED").upper()
        if confidence == "UNRESOLVED":
            if not include_unresolved:
                continue
            payload = {
                "decision": "MARCAR_COMO_NAO_RESOLVIDO", "review_notes": "Aprovado em lote",
                "approved_for_alias": False, "reviewed_at": timestamp, "reviewed_by": "batch",
            }
        else:
            if not text_value(case.get("candidato_recomendado"), "") or not text_value(case.get("codlog_recomendado"), ""):
                continue
            payload = {
                "decision": "APROVAR_RECOMENDACAO", "review_notes": "Aprovado em lote",
                "approved_for_alias": False, "reviewed_at": timestamp, "reviewed_by": "batch",
            }
        records.append(_review_record(case, validate_decision(case, payload)))
    if records:
        existing = load_reviews(path)
        changed_keys = {record["review_key"] for record in records}
        previous = existing[~existing["review_key"].isin(changed_keys)]
        updates = pd.DataFrame(records)[REVIEW_COLUMNS]
        combined = updates if previous.empty else pd.concat([previous, updates], ignore_index=True)
        atomic_write_csv(combined.drop_duplicates("review_key", keep="last"), path)
    return normalize_batch_result({
        "changed": preview["changed"], "approved": preview["approved"],
        "ignored": preview["ignored"], "unresolved": preview["marked_unresolved"],
        "elapsed_seconds": time.perf_counter() - started,
    })


def export_approved(reviews: pd.DataFrame, path: Path | str = DEFAULT_APPROVED_PATH) -> pd.DataFrame:
    approved = reviews[reviews["decision"] == "APROVAR_RECOMENDACAO"].copy()
    atomic_write_csv(approved, path)
    return approved


def export_alias_candidates(reviews: pd.DataFrame, path: Path | str = DEFAULT_ALIAS_PATH) -> pd.DataFrame:
    selected = reviews[(reviews["decision"] == "APROVAR_RECOMENDACAO") & reviews["approved_for_alias"].map(parse_bool)].copy()
    aliases = pd.DataFrame({
        "original_norm": selected.get("original_norm", pd.Series(dtype=str)),
        "resolved_norm": selected.get("manual_resolved_street", pd.Series(dtype=str)),
        "codlog": selected.get("manual_codlog", pd.Series(dtype=str)),
        "source": "HUMAN_REVIEW", "notes": selected.get("review_notes", pd.Series(dtype=str)), "active": True,
    })
    atomic_write_csv(aliases, path)
    return aliases


def review_metrics(cases: pd.DataFrame) -> dict[str, Any]:
    divergence = cases[cases["diverge_resolucao_atual"]].copy()
    decision = divergence["decision"].fillna("").astype(str)
    reviewed = decision.str.strip() != ""
    decisions = {kind: int((decision == kind).sum()) for kind in DECISIONS}
    by_confidence: dict[str, dict[str, int]] = {}
    approval_rates: dict[str, float | None] = {}
    for confidence in ("HIGH", "MEDIUM", "LOW", "UNRESOLVED"):
        subset = divergence[divergence["confianca"] == confidence]
        subset_decision = subset["decision"].fillna("").astype(str)
        count_reviewed = int((subset_decision.str.strip() != "").sum())
        approved = int((subset_decision == "APROVAR_RECOMENDACAO").sum())
        by_confidence[confidence] = {"total": len(subset), "reviewed": count_reviewed, "pending": len(subset) - count_reviewed, "approved": approved}
        approval_rates[confidence] = round(approved / count_reviewed, 4) if count_reviewed else None
    approved_cases = divergence[decision == "APROVAR_RECOMENDACAO"]
    return {
        "total_divergences": len(divergence), "total_reviewed": int(reviewed.sum()), "total_pending": int((~reviewed).sum()),
        "decisions": decisions, "by_confidence": by_confidence, "approval_rate_by_confidence": approval_rates,
        "average_score_approved": _mean_or_none(approved_cases.get("score_final")),
        "average_distance_approved": _mean_or_none(approved_cases.get("distance_m")),
        "aliases_marked": int(((decision == "APROVAR_RECOMENDACAO") & divergence["approved_for_alias"].map(parse_bool)).sum()),
    }


def _mean_or_none(values: Any) -> float | None:
    if values is None:
        return None
    numeric = pd.to_numeric(values, errors="coerce").dropna()
    return round(float(numeric.mean()), 4) if not numeric.empty else None


def write_report(cases: pd.DataFrame, path: Path | str = DEFAULT_REPORT_PATH) -> dict[str, Any]:
    report = review_metrics(cases)
    report["last_updated"] = datetime.now(timezone.utc).isoformat()
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(payload, encoding="utf-8")
    os.replace(temporary, target)
    return report


def stratified_sample(frame: pd.DataFrame, sizes: Mapping[str, int], seed: int = 42) -> pd.DataFrame:
    selected: list[pd.DataFrame] = []
    source = filter_cases(frame, {"divergent": True})
    for offset, confidence in enumerate(("HIGH", "MEDIUM", "LOW", "UNRESOLVED")):
        count = max(0, int(sizes.get(confidence, 0)))
        bucket = source[source["confianca"] == confidence]
        selected.append(bucket.sample(n=min(count, len(bucket)), random_state=seed + offset) if count else bucket.iloc[0:0])
    return ordered_cases(pd.concat(selected, ignore_index=True)) if selected else source.iloc[0:0]


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Utilitarios da revisao humana de resolucao de ruas.")
    parser.add_argument("--audit", type=Path, default=DEFAULT_AUDIT_PATH)
    parser.add_argument("--reviews", type=Path, default=DEFAULT_REVIEW_PATH)
    parser.add_argument("--export-high-divergences", action="store_true")
    parser.add_argument("--export-approved", action="store_true")
    parser.add_argument("--sample-high", type=int, default=0)
    parser.add_argument("--sample-medium", type=int, default=0)
    parser.add_argument("--sample-low", type=int, default=0)
    parser.add_argument("--sample-unresolved", type=int, default=0)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    audit = merge_reviews(load_audit(args.audit), load_reviews(args.reviews))
    if args.export_high_divergences:
        target = args.audit.parent / "street_resolution_high_divergences.csv"
        atomic_write_csv(filter_cases(audit, {"confidence": ["HIGH"]}), target)
        print(target)
    if args.export_approved:
        approved = export_approved(load_reviews(args.reviews))
        aliases = export_alias_candidates(load_reviews(args.reviews))
        print(f"{len(approved)} aprovadas; {len(aliases)} aliases")
    sizes = {"HIGH": args.sample_high, "MEDIUM": args.sample_medium, "LOW": args.sample_low, "UNRESOLVED": args.sample_unresolved}
    if any(sizes.values()):
        target = args.audit.parent / "street_resolution_review_sample.csv"
        atomic_write_csv(stratified_sample(audit, sizes, args.seed), target)
        print(target)


if __name__ == "__main__":
    _cli()
