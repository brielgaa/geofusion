"""Formatações seguras e reutilizáveis para a interface."""
from __future__ import annotations

import html
import math
from typing import Any

import pandas as pd


def texto(valor: Any, fallback: str = "—") -> str:
    if valor is None or (isinstance(valor, float) and math.isnan(valor)):
        return fallback
    resultado = str(valor).strip()
    return resultado if resultado and resultado.lower() != "nan" else fallback


def escapar(valor: Any, fallback: str = "—") -> str:
    return html.escape(texto(valor, fallback))


def numero(valor: Any, casas: int = 0, fallback: str = "—") -> str:
    valor_num = pd.to_numeric(pd.Series([valor]), errors="coerce").iloc[0]
    if pd.isna(valor_num):
        return fallback
    return f"{float(valor_num):,.{casas}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def percentual(valor: Any, casas: int = 1, fallback: str = "—") -> str:
    valor_num = pd.to_numeric(pd.Series([valor]), errors="coerce").iloc[0]
    if pd.isna(valor_num):
        return fallback
    return f"{float(valor_num):.{casas}f}%".replace(".", ",")


def data(valor: Any, fallback: str = "—") -> str:
    convertido = pd.to_datetime(valor, errors="coerce")
    return convertido.strftime("%d/%m/%Y") if pd.notna(convertido) else fallback


def distancia_km(valor: Any) -> str:
    valor_num = pd.to_numeric(pd.Series([valor]), errors="coerce").iloc[0]
    if pd.isna(valor_num):
        return "—"
    if valor_num < 1:
        return f"{valor_num * 1000:.0f} m"
    return f"{valor_num:.2f} km".replace(".", ",")


def mascarar(valor: Any, manter: int = 4) -> str:
    original = texto(valor, "")
    if not original:
        return "—"
    if len(original) <= manter:
        return "•" * len(original)
    return f"{original[:manter]}••••"
