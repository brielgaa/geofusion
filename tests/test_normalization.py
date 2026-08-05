from __future__ import annotations

from transform import corrigir_texto, eh_toda_extensao, normalizar_cep, normalizar_rua


def test_normalizar_rua_remove_tipo_e_expande_abreviacao() -> None:
    assert normalizar_rua("Av. Dr. João, 120") == "DOUTOR JOAO"


def test_corrigir_texto_recupera_mojibake() -> None:
    assert corrigir_texto("SÃ£o Paulo") == "São Paulo"


def test_normalizar_cep_e_toda_extensao() -> None:
    assert normalizar_cep("01234-567") == "01234567"
    assert eh_toda_extensao("em toda a extensão")
