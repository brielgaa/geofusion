"""Códigos internos e metadados visuais para status operacionais.

Os códigos são a fonte de verdade. Rótulos, cores e indicadores visuais são
responsabilidade exclusiva da camada de interface.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd


CONCLUIDO = "CONCLUIDO"
PLANEJADO = "PLANEJADO"
EM_ANDAMENTO = "EM_ANDAMENTO"
SEM_COBERTURA = "SEM_COBERTURA"
REVISAO = "REVISAO"

SITUACOES = (CONCLUIDO, PLANEJADO, EM_ANDAMENTO, SEM_COBERTURA, REVISAO)
METODOS_FRACOS = {
    "NOME",
    "COORD_PROXIMA",
    "COORD_REGIONAL",
    "COORD_REGIONAL_LONGA",
}


@dataclass(frozen=True)
class StatusMeta:
    label: str
    color: str
    background: str
    description: str


STATUS_META: dict[str, StatusMeta] = {
    CONCLUIDO: StatusMeta(
        "Concluído", "#66D399", "#153225", "Recape concluído associado à notificação."
    ),
    PLANEJADO: StatusMeta(
        "Planejado", "#F6C767", "#3B2B10", "Recape planejado ou contratado associado à notificação."
    ),
    EM_ANDAMENTO: StatusMeta(
        "Em andamento", "#8FB9FF", "#152A4A", "Recape com execução em curso ou outro estado operacional."
    ),
    SEM_COBERTURA: StatusMeta(
        "Sem cobertura", "#FF8A80", "#401C1C", "Nenhum recape foi associado pela estratégia atual."
    ),
    REVISAO: StatusMeta(
        "Revisão", "#C9A8FF", "#2D1E4B", "Correspondência encontrada, mas requer validação humana."
    ),
}

LEGACY_SITUACOES = {
    "✅ Recape concluído": CONCLUIDO,
    "⚠️ Recape planejado": PLANEJADO,
    "🟡 Em andamento": EM_ANDAMENTO,
    "🔴 Sem cobertura": SEM_COBERTURA,
    "RECAPE CONCLUIDO": CONCLUIDO,
    "RECAPE PLANEJADO": PLANEJADO,
}

RECAPE_STATUS_LABELS = {
    "CONCLUIDO": "Concluído",
    "CONCLUIDO_RATIFICAR": "Concluído — ratificar",
    "PLANEJADO": "Planejado",
    "CONTRATADO": "Contratado",
    "EM_EXECUCAO": "Em execução",
    "SUSPENSO": "Suspenso",
    "APENAS_INFRA": "Apenas infraestrutura",
    "EXCLUIDO": "Excluído",
}


def normalizar_situacao(valor: Any) -> str:
    """Converte valores atuais e legados para um código semântico estável."""
    texto = str(valor or "").strip()
    if texto in SITUACOES:
        return texto
    if texto in LEGACY_SITUACOES:
        return LEGACY_SITUACOES[texto]
    texto_maiusculo = texto.upper()
    if "SEM COBERTURA" in texto_maiusculo:
        return SEM_COBERTURA
    if "CONCLUID" in texto_maiusculo:
        return CONCLUIDO
    if "PLANEJ" in texto_maiusculo:
        return PLANEJADO
    if "ANDAMENTO" in texto_maiusculo or "EXECU" in texto_maiusculo:
        return EM_ANDAMENTO
    return EM_ANDAMENTO


def status_meta(codigo: str) -> StatusMeta:
    return STATUS_META.get(codigo, STATUS_META[EM_ANDAMENTO])


def status_label(codigo: str) -> str:
    return status_meta(codigo).label


def status_recape_label(valor: Any) -> str:
    texto = str(valor or "").strip().upper()
    return RECAPE_STATUS_LABELS.get(texto, texto.replace("_", " ").title() if texto else "Não informado")


def banda_confianca(valor: Any, encontrou_match: bool = True) -> str:
    if not encontrou_match:
        return "Sem match"
    score = pd.to_numeric(pd.Series([valor]), errors="coerce").iloc[0]
    if pd.isna(score):
        return "Não informado"
    if score < 70:
        return "Crítica"
    if score < 85:
        return "Baixa"
    if score < 95:
        return "Aceitável"
    return "Alta"
