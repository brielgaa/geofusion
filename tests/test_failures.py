from __future__ import annotations

from transform import _failure_diagnosis


def test_falha_de_intersecao_tem_categoria_explicita() -> None:
    category, detail = _failure_diagnosis(
        "SEM_INTERSECAO_DE",
        {"rua_via_resolvida": "PAULISTA", "method_via": "EXATO", "segment_count_via": 12},
        {},
    )

    assert category == "SEM_INTERSECAO_DE"
    assert "interseção" in detail.lower()


def test_codlog_inexistente_eh_reportado() -> None:
    category, _ = _failure_diagnosis(
        "SEM_RUA_GEOM",
        {"codlog_status": "INEXISTENTE", "method_via": "SEM_GEOMETRIA"},
        {},
    )

    assert category == "CODLOG_INEXISTENTE"
