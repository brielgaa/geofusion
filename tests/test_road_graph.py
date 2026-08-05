from __future__ import annotations

import pandas as pd
from shapely.geometry import LineString

from road_graph import RoadGraph


def _roads(disconnected: bool = False) -> pd.DataFrame:
    end = 30 if disconnected else 20
    main = [LineString([(0, 0), (10, 0)]), LineString([(20 if disconnected else 10, 0), (end, 0)])]
    return pd.DataFrame([
        {"geometry": main[0], "codlog": "1", "nm_logradouro": "Rua Principal", "cd_numero_ordem_segmento": "1"},
        {"geometry": main[1], "codlog": "1", "nm_logradouro": "Rua Principal", "cd_numero_ordem_segmento": "2"},
        {"geometry": LineString([(0, -5), (0, 0)]), "codlog": "2", "nm_logradouro": "Rua Inicio", "cd_numero_ordem_segmento": "1"},
        {"geometry": LineString([(end, 0), (end, 5)]), "codlog": "3", "nm_logradouro": "Rua Fim", "cd_numero_ordem_segmento": "1"},
    ])


def _normalize(value: str) -> str:
    return str(value).upper().replace("RUA ", "")


def test_road_graph_rota_segmentos_reais() -> None:
    graph = RoadGraph.from_geodataframe(_roads(), _normalize)
    geometry, status, metadata = graph.route("Rua Principal", "Rua Inicio", "Rua Fim", expected_length=20)

    assert geometry is not None
    assert status == "OK"
    assert metadata["segment_count"] == 2


def test_road_graph_detecta_componentes_desconectados() -> None:
    graph = RoadGraph.from_geodataframe(_roads(disconnected=True), _normalize)
    geometry, status, _ = graph.route("Rua Principal", "Rua Inicio", "Rua Fim")

    assert geometry is None
    assert status == "SEM_CAMINHO"


def test_cache_e_invalidado_quando_fonte_muda(tmp_path) -> None:
    source = tmp_path / "segmentos.geojson"
    source.write_text("versao-1", encoding="utf-8")
    path = tmp_path / "grafo.pkl"
    graph = RoadGraph.from_geodataframe(_roads(), _normalize)
    graph.save(str(path), str(source))

    assert RoadGraph.load_cached(str(path), str(source), normalizer=_normalize) is not None
    source.write_text("versao-2-com-assinatura-diferente", encoding="utf-8")
    assert RoadGraph.load_cached(str(path), str(source), normalizer=_normalize) is None
