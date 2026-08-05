"""Carregamento tolerante aos CSVs gerados pelo ETL."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd


DATE_COLUMNS = {
    "cruzamento.csv": ("data_recebimento", "data_termino_recape"),
    "recape_clean.csv": ("data_criacao", "data_termino", "data_atualizacao"),
    "notificacoes.csv": ("data_recebimento",),
}


@dataclass
class AppData:
    cruzamento: pd.DataFrame
    recapes: pd.DataFrame
    notificacoes: pd.DataFrame
    falhas: pd.DataFrame
    coverage_report: dict[str, Any]
    pipeline_run: dict[str, Any]
    errors: list[str]
    processed_dir: Path
    updated_at: pd.Timestamp | None


def _empty() -> pd.DataFrame:
    return pd.DataFrame()


def _read_csv(path: Path, date_columns: tuple[str, ...] = ()) -> tuple[pd.DataFrame, str | None]:
    if not path.exists():
        return _empty(), f"Arquivo ausente: {path.name}"
    try:
        dataframe = pd.read_csv(path, low_memory=False, encoding="utf-8-sig")
    except UnicodeDecodeError:
        try:
            dataframe = pd.read_csv(path, low_memory=False, encoding="latin-1")
        except Exception as exc:  # pragma: no cover - proteção de interface
            return _empty(), f"Não foi possível ler {path.name}: {exc}"
    except (OSError, pd.errors.ParserError) as exc:
        return _empty(), f"Não foi possível ler {path.name}: {exc}"

    for column in date_columns:
        if column in dataframe.columns:
            dataframe[column] = pd.to_datetime(dataframe[column], format="mixed", dayfirst=True, errors="coerce")
    return dataframe, None


def _read_json(path: Path) -> tuple[dict[str, Any], str | None]:
    if not path.exists():
        return {}, None
    try:
        with path.open(encoding="utf-8") as stream:
            content = json.load(stream)
        return content if isinstance(content, dict) else {}, None
    except (OSError, ValueError) as exc:
        return {}, f"Não foi possível ler {path.name}: {exc}"


def _latest_mtime(directory: Path) -> pd.Timestamp | None:
    files = [path for path in directory.glob("*") if path.is_file()]
    if not files:
        return None
    return pd.Timestamp(max(path.stat().st_mtime for path in files), unit="s")


def load_app_data(project_dir: Path) -> AppData:
    """Lê somente artefatos disponíveis e acumula erros recuperáveis."""
    processed_dir = project_dir / "data" / "processed"
    errors: list[str] = []

    cruzamento, error = _read_csv(
        processed_dir / "cruzamento.csv", DATE_COLUMNS["cruzamento.csv"]
    )
    if error:
        errors.append(error)
    recapes, error = _read_csv(
        processed_dir / "recape_clean.csv", DATE_COLUMNS["recape_clean.csv"]
    )
    if error:
        errors.append(error)
    notificacoes, error = _read_csv(
        processed_dir / "notificacoes.csv", DATE_COLUMNS["notificacoes.csv"]
    )
    if error:
        errors.append(error)
    falhas, error = _read_csv(processed_dir / "recapes_sem_cobertura.csv")
    if error:
        errors.append(error)
    coverage_report, error = _read_json(processed_dir / "geosampa_coverage_report.json")
    if error:
        errors.append(error)
    pipeline_run, error = _read_json(processed_dir / "pipeline_run.json")
    if error:
        errors.append(error)

    return AppData(
        cruzamento=cruzamento,
        recapes=recapes,
        notificacoes=notificacoes,
        falhas=falhas,
        coverage_report=coverage_report,
        pipeline_run=pipeline_run,
        errors=errors,
        processed_dir=processed_dir,
        updated_at=_latest_mtime(processed_dir),
    )
