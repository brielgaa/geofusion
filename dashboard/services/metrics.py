"""Métricas puras e campos derivados para a auditoria geoespacial."""
from __future__ import annotations

from typing import Any

import pandas as pd

from dashboard.utils.status import (
    EM_ANDAMENTO,
    METODOS_FRACOS,
    REVISAO,
    SEM_COBERTURA,
    banda_confianca,
    normalizar_situacao,
)


def safe_ratio(numerator: int | float, denominator: int | float) -> float:
    return float(numerator) / float(denominator) if denominator else 0.0


def _series(frame: pd.DataFrame, column: str, default: Any = "") -> pd.Series:
    if column in frame.columns:
        return frame[column]
    return pd.Series(default, index=frame.index)


def prepare_audit_records(cruzamento: pd.DataFrame) -> pd.DataFrame:
    """Acrescenta semântica de auditoria sem modificar as regras do ETL.

    A marcação de revisão é uma sinalização visual: matches somente por nome,
    por coordenada isolada ou com score menor que 85 devem ser verificados por
    uma pessoa. O valor original de ``situacao`` continua preservado.
    """
    if cruzamento.empty:
        return cruzamento.copy()

    result = cruzamento.copy().reset_index(drop=True)
    metodo = _series(result, "metodo_match", SEM_COBERTURA).fillna(SEM_COBERTURA).astype(str).str.upper()
    score = pd.to_numeric(_series(result, "score_fuzzy", 0), errors="coerce").fillna(0).clip(0, 100)
    situacao = _series(result, "situacao", EM_ANDAMENTO).map(normalizar_situacao)
    encontrou_match = metodo.ne(SEM_COBERTURA)
    exige_revisao = encontrou_match & (metodo.isin(METODOS_FRACOS) | score.lt(85))
    situacao_auditoria = situacao.copy()
    situacao_auditoria.loc[~encontrou_match] = SEM_COBERTURA
    situacao_auditoria.loc[exige_revisao] = REVISAO

    result["score_confianca"] = score
    result["situacao_codigo"] = situacao
    result["situacao_auditoria"] = situacao_auditoria
    result["match_encontrado"] = encontrou_match
    result["exige_revisao"] = exige_revisao
    result["baixa_confianca"] = encontrou_match & score.lt(85)
    result["cobertura_confirmada"] = encontrou_match & ~exige_revisao
    result["banda_confianca"] = [
        banda_confianca(value, found) for value, found in zip(score, encontrou_match)
    ]
    result["case_id"] = result.index.astype(str) + "-" + _series(result, "numero_os", "sem-os").fillna("sem-os").astype(str)
    return result


def coverage_metrics(records: pd.DataFrame) -> dict[str, float | int]:
    total = len(records)
    found = int(_series(records, "match_encontrado", False).sum())
    confirmed = int(_series(records, "cobertura_confirmada", False).sum())
    review = int(_series(records, "exige_revisao", False).sum())
    low_confidence = int(_series(records, "baixa_confianca", False).sum())
    no_coverage = int(total - found)
    return {
        "total": total,
        "found": found,
        "confirmed": confirmed,
        "review": review,
        "low_confidence": low_confidence,
        "no_coverage": no_coverage,
        "confirmed_pct": safe_ratio(confirmed, total) * 100,
        "found_pct": safe_ratio(found, total) * 100,
        "review_pct": safe_ratio(review, total) * 100,
        "no_coverage_pct": safe_ratio(no_coverage, total) * 100,
    }


def coverage_delta(records: pd.DataFrame) -> float | None:
    """Variação de cobertura confirmada entre os dois últimos meses completos."""
    if "data_recebimento" not in records.columns or records.empty:
        return None
    dates = pd.to_datetime(records["data_recebimento"], errors="coerce")
    monthly = records.assign(_month=dates.dt.to_period("M")).dropna(subset=["_month"])
    if monthly["_month"].nunique() < 2:
        return None
    grouped = monthly.groupby("_month").agg(
        total=("case_id", "size"), confirmed=("cobertura_confirmada", "sum")
    )
    last_two = grouped.tail(2)
    before = safe_ratio(last_two.iloc[0]["confirmed"], last_two.iloc[0]["total"]) * 100
    current = safe_ratio(last_two.iloc[1]["confirmed"], last_two.iloc[1]["total"]) * 100
    return current - before


def monthly_coverage(records: pd.DataFrame) -> pd.DataFrame:
    if "data_recebimento" not in records.columns or records.empty:
        return pd.DataFrame(columns=["mes", "total", "confirmada", "sem_cobertura", "baixa_confianca"])
    dated = records.copy()
    dated["mes"] = pd.to_datetime(dated["data_recebimento"], errors="coerce").dt.to_period("M")
    dated = dated.dropna(subset=["mes"])
    if dated.empty:
        return pd.DataFrame(columns=["mes", "total", "confirmada", "sem_cobertura", "baixa_confianca"])
    grouped = dated.groupby("mes").agg(
        total=("case_id", "size"),
        confirmada=("cobertura_confirmada", "sum"),
        sem_cobertura=("match_encontrado", lambda value: int((~value).sum())),
        baixa_confianca=("baixa_confianca", "sum"),
    ).reset_index()
    grouped["mes"] = grouped["mes"].astype(str)
    grouped["cobertura_pct"] = grouped["confirmada"] / grouped["total"] * 100
    return grouped


def regional_criticality(records: pd.DataFrame) -> pd.DataFrame:
    if "prefeitura_regional" not in records.columns or records.empty:
        return pd.DataFrame(columns=["regional", "total", "sem_cobertura_pct", "baixa_confianca_pct", "criticidade"])
    regional = records.copy()
    regional["regional"] = regional["prefeitura_regional"].fillna("Não informado").replace("", "Não informado")
    grouped = regional.groupby("regional").agg(
        total=("case_id", "size"),
        sem_cobertura=("match_encontrado", lambda value: int((~value).sum())),
        baixa_confianca=("baixa_confianca", "sum"),
    ).reset_index()
    grouped["sem_cobertura_pct"] = grouped["sem_cobertura"] / grouped["total"] * 100
    grouped["baixa_confianca_pct"] = grouped["baixa_confianca"] / grouped["total"] * 100
    grouped["criticidade"] = grouped["sem_cobertura_pct"] * 0.7 + grouped["baixa_confianca_pct"] * 0.3
    grouped["status"] = pd.cut(
        grouped["criticidade"],
        bins=[-1, 20, 45, float("inf")],
        labels=["Estável", "Atenção", "Crítica"],
    ).astype(str)
    return grouped.sort_values(["criticidade", "total"], ascending=[False, False])


def match_flow(records: pd.DataFrame) -> pd.DataFrame:
    if records.empty:
        return pd.DataFrame(columns=["metodo", "destino", "quantidade"])
    flow = records.copy()
    flow["metodo"] = _series(flow, "metodo_match", SEM_COBERTURA).fillna(SEM_COBERTURA).astype(str)
    flow["destino"] = flow["situacao_auditoria"].where(flow["match_encontrado"], SEM_COBERTURA)
    return flow.groupby(["metodo", "destino"]).size().reset_index(name="quantidade")


def coverage_waterfall(coverage_report: dict[str, Any], recapes: pd.DataFrame) -> pd.DataFrame:
    """Monta perdas por etapa usando somente o relatório técnico disponível."""
    report_total = int(coverage_report.get("total_recapes", 0) or 0)
    total = report_total or len(recapes)
    if not total:
        return pd.DataFrame(columns=["etapa", "quantidade", "tipo"])
    failures = coverage_report.get("falhas_por_motivo", {}) or {}
    rows = [
        ("Recapes processados", total, "base"),
        ("Via não resolvida", int(failures.get("SEM_RUA", 0)) + int(failures.get("FUZZY_NAO_RESOLVEU", 0)) + int(failures.get("CODLOG_INEXISTENTE", 0)), "perda"),
        ("Sem interseção em De", int(failures.get("SEM_INTERSECAO_DE", 0)), "perda"),
        ("Sem interseção em Até", int(failures.get("SEM_INTERSECAO_ATE", 0)), "perda"),
        ("Sem caminho topológico", int(failures.get("SEM_CAMINHO", 0)), "perda"),
        ("Geometria inválida", int(failures.get("GEOMETRIA_INVALIDA", 0)), "perda"),
        ("Cobertura geométrica final", int(coverage_report.get("com_geometria", 0) or 0), "final"),
    ]
    return pd.DataFrame(rows, columns=["etapa", "quantidade", "tipo"])


def confidence_distribution(records: pd.DataFrame) -> pd.DataFrame:
    labels = ["Crítica", "Baixa", "Aceitável", "Alta"]
    if records.empty:
        return pd.DataFrame({"faixa": labels, "quantidade": [0] * len(labels)})
    matched = records[records["match_encontrado"]].copy()
    bands = pd.cut(
        matched["score_confianca"],
        bins=[-0.01, 69.99, 84.99, 94.99, 100],
        labels=labels,
    )
    count = bands.value_counts().reindex(labels, fill_value=0).rename_axis("faixa").reset_index(name="quantidade")
    return count
