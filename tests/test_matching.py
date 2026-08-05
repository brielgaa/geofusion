from __future__ import annotations

import pandas as pd

from transform import SITUACAO_CONCLUIDO, cruzar


def test_cruzar_prioriza_nome_e_coordenada() -> None:
    notifications = pd.DataFrame([
        {
            "numero_os": "100", "fonte": "SGZ_156", "rua_raw": "Rua das Flores", "rua_norm": "DAS FLORES",
            "cep": "01001000", "latitude": -23.55, "longitude": -46.63, "prefeitura_regional": "SE",
        }
    ])
    recapes = pd.DataFrame([
        {
            "id": "R1", "rua_raw": "Rua das Flores", "rua_norm": "DAS FLORES", "cep": "01001000",
            "latitude": -23.5505, "longitude": -46.6305, "status": "CONCLUIDO", "subprefeitura": "SE",
        }
    ])

    result = cruzar(notifications, recapes)

    assert result.loc[0, "metodo_match"] == "NOME+CEP"
    assert result.loc[0, "situacao"] == SITUACAO_CONCLUIDO


def test_cruzar_sem_recape_gera_codigo_sem_cobertura() -> None:
    notifications = pd.DataFrame([{"numero_os": "101", "fonte": "SGZ_156", "rua_raw": "Rua Sem Match", "rua_norm": "SEM MATCH"}])
    recapes = pd.DataFrame([{"rua_raw": "Rua Existente", "rua_norm": "EXISTENTE"}])

    result = cruzar(notifications, recapes)

    assert result.loc[0, "metodo_match"] == "SEM_COBERTURA"
    assert result.loc[0, "situacao"] == "SEM_COBERTURA"
