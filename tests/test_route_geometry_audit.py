from __future__ import annotations

import json
from types import SimpleNamespace

import pandas as pd
from shapely.geometry import LineString

from road_graph import RoadGraph
from route_geometry_audit import GeometryQualityShadowEngine, GeometryRecoveryEngine, _result_row, _source_signature


def _normalize(value: str) -> str:
    return " ".join(str(value or "").upper().replace("RUA ", "").split())


def _graph(split_main: bool = False, de_y: float = 0.0) -> RoadGraph:
    if split_main:
        main = [
            LineString([(0, 0), (20, 0)]),
            LineString([(20, 0), (80, 0)]),
            LineString([(80, 0), (100, 0)]),
        ]
    else:
        main = [LineString([(0, 0), (100, 0)])]
    roads = [
        *({"geometry": geometry, "codlog": "1", "nm_logradouro": "Principal", "cd_numero_ordem_segmento": str(index)} for index, geometry in enumerate(main)),
        {"geometry": LineString([(20, de_y), (20, 20)]), "codlog": "2", "nm_logradouro": "Inicio", "cd_numero_ordem_segmento": "1"},
        {"geometry": LineString([(80, -20), (80, 0)]), "codlog": "3", "nm_logradouro": "Fim", "cd_numero_ordem_segmento": "1"},
    ]
    return RoadGraph.from_geodataframe(pd.DataFrame(roads), _normalize)


def _row(**values):
    result = {
        "id": "1", "via": "Principal", "de": "Inicio", "ate": "Fim",
        "latitude": "0", "longitude": "-46.6", "extensao_m": "60",
    }
    result.update(values)
    return result


def test_interseccoes_geometricas_sem_no_topologico_geram_candidato_valido():
    result = GeometryRecoveryEngine(_graph()).recover(_row(), {"status_path": "SEM_INTERSECAO_DE"})

    assert result.recovered
    assert result.selected_candidate is not None
    assert result.selected_candidate.strategy == "GEOMETRIC_INTERSECTIONS"
    assert result.selected_candidate.topology_status == "GEOMETRIC_INTERSECTION"
    assert result.selected_candidate.snap_used is False
    assert result.selected_candidate.geometry_wkt.startswith("LINESTRING")


def test_interseccao_topologica_exata_continua_separada_de_snap():
    result = GeometryRecoveryEngine(_graph(split_main=True)).recover(_row(), {"status_path": "SEM_CAMINHO"})

    assert result.recovered
    assert result.selected_candidate is not None
    assert result.selected_candidate.strategy == "PROJECTED_INTERSECTIONS"
    assert result.selected_candidate.snap_used is False


def test_gap_pequeno_e_classificado_como_snap_virtual():
    result = GeometryRecoveryEngine(_graph(de_y=0.5)).recover(_row(), {"status_path": "SEM_INTERSECAO_DE"})

    snap_candidates = [candidate for candidate in result.alternatives + ([result.selected_candidate] if result.selected_candidate else []) if candidate.strategy == "GEOMETRIC_GAP_SNAP"]
    assert snap_candidates
    assert snap_candidates[0].snap_used
    assert snap_candidates[0].max_gap_m is not None and snap_candidates[0].max_gap_m <= 0.5


def test_gap_acima_de_cinco_metres_nao_e_snap_automatico():
    result = GeometryRecoveryEngine(_graph(de_y=6.0)).recover(_row(), {"status_path": "SEM_INTERSECAO_DE"})

    assert not any(candidate.strategy == "GEOMETRIC_GAP_SNAP" for candidate in result.alternatives)
    assert result.selected_candidate is None or not result.selected_candidate.snap_used


def test_decisao_humana_bloqueada_permanece_unresolved():
    override = SimpleNamespace(valid=True, applicable=True, block_fuzzy=True, resolved_street="PRINCIPAL", resolved_codlog="1")
    overrides = SimpleNamespace(for_record=lambda row: override)

    result = GeometryRecoveryEngine(_graph(), overrides=overrides).recover(_row(), {"status_path": "SEM_CAMINHO"})

    assert result.recovered is False
    assert result.confidence == "UNRESOLVED"
    assert result.requires_review is True


def test_saida_de_auditoria_tem_wkt_geojson_e_contagem_de_candidatos():
    result = GeometryRecoveryEngine(_graph()).recover(_row(), {"status_path": "SEM_CAMINHO", "categoria_falha": "SEM_CAMINHO"})
    output = _result_row(_row(), {"status_path": "SEM_CAMINHO", "categoria_falha": "SEM_CAMINHO"}, result)

    assert output["candidate_count"] == result.candidate_count
    assert output["geometry_wkt"].startswith("LINESTRING")
    assert '"type": "LineString"' in output["geometry_geojson"]


def test_assinatura_do_checkpoint_sobrevive_ao_roundtrip_json(tmp_path):
    source = tmp_path / "fonte.geojson"
    source.write_text("fonte", encoding="utf-8")
    signature = _source_signature([source])

    assert json.loads(json.dumps(signature)) == signature


def test_texto_toda_extensao_seleciona_componente_sem_unir_componentes():
    row = _row(de="TODA EXTENSAO", ate="TODA EXTENSAO", extensao_m="100")
    result = GeometryRecoveryEngine(_graph()).recover(row, {"status_path": "SEM_CAMINHO"})

    assert result.recovered
    strategies = {candidate.strategy for candidate in [result.selected_candidate, *result.alternatives] if candidate}
    assert "SPECIAL_WHOLE_COMPONENT" in strategies


def test_shadow_resolve_nome_principal_incompleto_e_preserva_rankings():
    row = _row(via="Principal Trecho", logradouro_geosampa="Principal Trecho", latitude=None, longitude=None)
    result = GeometryQualityShadowEngine(_graph()).recover(row, {"status_path": "SEM_CAMINHO"})

    assert result.recovered
    assert result.selected_candidate is not None
    assert result.selected_candidate.main_street == "PRINCIPAL"
    assert "via_principal_shadow" in result.selected_candidate.evidence or result.selected_candidate.strategy.startswith("SHADOW_MAIN_")
    assert result.candidate_count >= 2
    assert result.requires_review is True
