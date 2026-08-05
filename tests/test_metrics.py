from __future__ import annotations

import pandas as pd

from dashboard.services.metrics import coverage_metrics, prepare_audit_records
from transform import calcular_cobertura


def test_calcular_cobertura_trata_dataframe_vazio() -> None:
    assert calcular_cobertura(pd.DataFrame()) == {
        "total": 0,
        "com_cobertura": 0,
        "sem_cobertura": 0,
        "cobertura_pct": 0.0,
    }


def test_metricas_separam_revisao_e_cobertura_confirmada() -> None:
    base = pd.DataFrame([
        {"numero_os": "1", "metodo_match": "NOME+COORD", "score_fuzzy": 98, "situacao": "CONCLUIDO"},
        {"numero_os": "2", "metodo_match": "NOME", "score_fuzzy": 97, "situacao": "CONCLUIDO"},
        {"numero_os": "3", "metodo_match": "SEM_COBERTURA", "score_fuzzy": 0, "situacao": "SEM_COBERTURA"},
    ])

    metrics = coverage_metrics(prepare_audit_records(base))

    assert metrics["confirmed"] == 1
    assert metrics["review"] == 1
    assert metrics["no_coverage"] == 1
