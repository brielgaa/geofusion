import json

import pandas as pd
from shapely.geometry import LineString, Point

from src.road_graph import RoadGraph
from src.street_resolver import (
    AuditInterrupted,
    StreetResolutionContext,
    StreetResolver,
    audit_dataframe,
)


def simple_normalizer(value):
    return str(value or "").upper().strip()


def build_graph():
    rows = [
        ("ACACIO", "100001", LineString([(0, 0), (10, 0)])),
        ("FONTOURA", "100002", LineString([(0, 10), (10, 10)])),
        ("ACACCIO FONTOURA", "100003", LineString([(0, 20), (10, 20)])),
        ("MAIN ALPHA", "100010", LineString([(0, 100), (100, 100)])),
        ("MAIN BETA", "100011", LineString([(0, 200), (100, 200)])),
        ("DE STREET", "100020", LineString([(30, 90), (30, 110)])),
        ("ATE STREET", "100021", LineString([(70, 90), (70, 110)])),
        ("SANTA MARIA", "100022", LineString([(50, 90), (50, 110)])),
        ("BRAGA", "100023", LineString([(0, 600), (100, 600)])),
        ("SILVA BRAGA", "100024", LineString([(0, 700), (100, 700)])),
        ("CAVALHEIRO", "100025", LineString([(0, 900), (100, 900)])),
        ("NEAR ONE", "100030", LineString([(0, 300), (100, 300)])),
        ("NEAR TWO", "100031", LineString([(0, 500), (100, 500)])),
    ]
    frame = pd.DataFrame(
        [
            {
                "codlog": codlog,
                "nm_logradouro": name,
                "cd_numero_ordem_segmento": "1",
                "geometry": geometry,
            }
            for name, codlog, geometry in rows
        ]
    )
    return RoadGraph.from_geodataframe(frame, simple_normalizer)


def make_resolver(tmp_path, graph):
    return StreetResolver(
        graph,
        normalizer=simple_normalizer,
        text_corrector=lambda value: value,
        aliases_path=tmp_path / "data" / "config" / "street_aliases.csv",
        cache_path=tmp_path / "data" / "cache" / "diagnostic.pkl",
        source_path=tmp_path / "geo.json",
    )


def test_complete_candidate_beats_token_set_partial(tmp_path):
    resolver = make_resolver(tmp_path, build_graph())

    result = resolver.resolve("ACACIO FONTOURA")

    assert result.resolved_street == "ACACCIO FONTOURA"
    assert result.candidates[0].final_score > result.candidates[1].final_score
    partial = next(item for item in result.candidates if item.street_norm == "ACACIO")
    assert partial.token_set_score == 100
    assert partial.incomplete is True
    assert "ACACCIO FONTOURA" in [item.street_norm for item in result.candidates]


def test_exact_codlog_and_alias(tmp_path):
    graph = build_graph()
    resolver = make_resolver(tmp_path, graph)

    exact = resolver.resolve("MAIN ALPHA")
    codlog = resolver.resolve("qualquer nome", codlog="100011")
    alias_path = tmp_path / "aliases.csv"
    alias_path.write_text(
        "original_norm,resolved_norm,codlog,scope,source,notes,active\n"
        "VIA ANTIGA,MAIN ALPHA,,GLOBAL,TEST,,true\n",
        encoding="utf-8-sig",
    )
    alias_resolver = StreetResolver(
        graph,
        normalizer=simple_normalizer,
        text_corrector=lambda value: value,
        aliases_path=alias_path,
        cache_path=tmp_path / "alias-cache.pkl",
        source_path=tmp_path / "geo.json",
    )
    alias = alias_resolver.resolve("VIA ANTIGA")

    assert exact.method == "EXATO" and exact.confidence == "HIGH"
    assert codlog.method == "CODLOG" and codlog.resolved_street == "MAIN BETA"
    assert alias.method == "ALIAS" and alias.resolved_street == "MAIN ALPHA"


def test_de_and_ate_intersections_break_fuzzy_tie(tmp_path):
    resolver = make_resolver(tmp_path, build_graph())

    result = resolver.resolve("MAIN", de="DE STREET", ate="ATE STREET")

    assert result.resolved_street == "MAIN ALPHA"
    assert result.candidates[0].intersects_de is True
    assert result.candidates[0].intersects_ate is True
    assert result.candidates[0].component_connected is True


def test_exact_street_keeps_street_high_when_ate_is_unresolved(tmp_path):
    resolver = make_resolver(tmp_path, build_graph())

    result = resolver.resolve("MAIN ALPHA", de="DE STREET", ate="UNKNOWN CROSS")

    assert result.street_confidence == "HIGH"
    assert result.street_requires_review is False
    assert result.route_requires_review is True
    assert result.route_context_status == "DE_CONFIRMED_ATE_NOT_FOUND"
    assert "ATE_NAO_RESOLVIDO" in result.route_review_reasons
    assert "INTERSECCAO_CONTRADITORIA" not in result.street_review_reasons


def test_special_route_context_does_not_create_review(tmp_path):
    resolver = make_resolver(tmp_path, build_graph())

    result = resolver.resolve("MAIN ALPHA", de="DE STREET", ate="ATE O FIM DA VIA")

    assert result.street_confidence == "HIGH"
    assert result.route_context_status == "SPECIAL_ROUTE_CONTEXT"
    assert result.route_requires_review is False


def test_transversal_semantic_substitution_is_not_accepted(tmp_path):
    resolver = make_resolver(tmp_path, build_graph())

    result = resolver.resolve("MAIN ALPHA", de="SANTA MARINA")
    candidate = result.candidates[0]

    assert candidate.de_resolution_status == "NAO_RESOLVIDA"
    assert candidate.de_candidate is None
    assert result.street_confidence == "HIGH"
    assert result.route_requires_review is True


def test_fuzzy_transversal_requires_real_intersection(tmp_path):
    resolver = make_resolver(tmp_path, build_graph())

    confirmed = resolver.resolve("MAIN ALPHA", de="DE STREETS")
    distant = resolver.resolve("MAIN BETA", de="DE STREETS")

    assert confirmed.candidates[0].de_resolution_status == "FUZZY_CONFIRMADA_POR_INTERSECAO"
    assert confirmed.candidates[0].de_candidate == "DE STREET"
    assert distant.candidates[0].de_resolution_status == "NAO_RESOLVIDA"
    assert distant.candidates[0].de_candidate is None


def test_partial_false_positive_cases_remain_conservative(tmp_path):
    resolver = make_resolver(tmp_path, build_graph())

    general = resolver.resolve("GENERAL SILVA BRAGA", reference=Point(50, 700))
    cavalheiro = resolver.resolve("CAVALHEIRO FRONTINI", reference=Point(50, 1000))

    assert general.resolved_street == "SILVA BRAGA"
    assert general.resolved_street != "BRAGA"
    assert cavalheiro.resolved_street is None
    assert cavalheiro.confidence == "UNRESOLVED"


def test_distance_and_missing_coordinate_are_contextual_evidence(tmp_path):
    graph = build_graph()
    resolver = make_resolver(tmp_path, graph)

    near = resolver.resolve("NEAR", reference=Point(50, 300))
    without_coordinate = resolver.resolve("NEAR")

    assert near.resolved_street == "NEAR ONE"
    assert near.candidates[0].distance_m == 0
    assert without_coordinate.candidates[0].distance_m is None


def test_special_reference_and_invalid_alias_are_reviewable(tmp_path):
    graph = build_graph()
    alias_path = tmp_path / "aliases.csv"
    alias_path.write_text(
        "original_norm,resolved_norm,codlog,scope,source,notes,active\n"
        "VIA INVALIDA,DESTINO AUSENTE,,GLOBAL,TEST,,true\n",
        encoding="utf-8-sig",
    )
    resolver = StreetResolver(
        graph,
        normalizer=simple_normalizer,
        text_corrector=lambda value: value,
        aliases_path=alias_path,
        cache_path=tmp_path / "cache.pkl",
        source_path=tmp_path / "geo.json",
    )

    result = resolver.resolve("VIA INVALIDA", de="TODA EXTENSAO", ate="")

    assert result.invalid_alias is True
    assert "ALIAS_INVALIDO" in result.review_reasons
    assert result.candidates
    assert result.candidates[0].intersects_de is None


def test_audit_reports_are_serializable_and_cache_is_reused(tmp_path):
    graph = build_graph()
    df = pd.DataFrame(
        [
            {
                "id": 1,
                "numero_processo": "P1",
                "via": "MAIN ALPHA",
                "logradouro_geosampa": "MAIN ALPHA",
                "de": "DE STREET",
                "ate": "ATE STREET",
                "latitude": None,
                "longitude": None,
            }
        ]
    )
    resolver = make_resolver(tmp_path, graph)
    audit, review, report = audit_dataframe(df, graph, resolver=resolver, progress=False)
    resolver.save_cache()
    second = make_resolver(tmp_path, graph)
    second_result = second.resolve_context(StreetResolutionContext(via_original="MAIN ALPHA", de_original="DE STREET", ate_original="ATE STREET"))

    assert audit.iloc[0]["candidato_recomendado"] == "MAIN ALPHA"
    assert audit.iloc[0]["street_confidence"] == "HIGH"
    assert audit.iloc[0]["street_requires_review"] == False
    assert audit.iloc[0]["route_requires_review"] == False
    assert "decision" not in review.columns or "decision" in review.columns
    assert report["total"] == 1
    assert report["street_reviews"] == 0
    assert report["route_reviews"] == 0
    json.dumps(report, ensure_ascii=False)
    assert second.cache_hits == 1
    assert second_result.resolved_street == "MAIN ALPHA"


def test_separate_cache_layers_are_reused(tmp_path):
    graph = build_graph()
    resolver = make_resolver(tmp_path, graph)
    resolver.resolve("NEAR", reference=Point(50, 300))
    resolver.resolve("MAIN ALPHA", reference=Point(50, 100), de="DE STREET", ate="ATE STREET")
    resolver.save_cache()

    second = make_resolver(tmp_path, graph)
    result = second.resolve("NEAR", reference=Point(50, 300))
    contextual = second.resolve("MAIN ALPHA", reference=Point(50, 100), de="DE STREET", ate="ATE STREET")

    assert result.resolved_street == "NEAR ONE"
    assert contextual.resolved_street == "MAIN ALPHA"
    assert second.cache_stats["lexical_cache_hits"] >= 1
    assert second.cache_stats["geographic_cache_hits"] >= 1
    assert second.cache_stats["transversal_cache_hits"] >= 2
    assert second.cache_stats["intersection_cache_hits"] >= 2


def test_checkpoint_resume_does_not_duplicate_rows(tmp_path):
    graph = build_graph()
    df = pd.DataFrame([
        {"id": 1, "via": "MAIN ALPHA", "logradouro_geosampa": "MAIN ALPHA", "de": "", "ate": ""},
        {"id": 2, "via": "MAIN BETA", "logradouro_geosampa": "MAIN BETA", "de": "", "ate": ""},
    ])
    checkpoint = tmp_path / "audit.checkpoint.pkl"
    resolver = make_resolver(tmp_path, graph)
    try:
        audit_dataframe(
            df,
            graph,
            resolver=resolver,
            progress=False,
            checkpoint_path=checkpoint,
            checkpoint_every=1,
            interrupt_after=1,
        )
    except AuditInterrupted:
        pass
    else:
        raise AssertionError("a interrupção simulada deveria criar checkpoint")

    resumed = make_resolver(tmp_path, graph)
    audit, _, report = audit_dataframe(
        df,
        graph,
        resolver=resumed,
        progress=False,
        checkpoint_path=checkpoint,
        resume=True,
    )
    assert len(audit) == 2
    assert audit["id"].tolist() == [1, 2]
    assert report["cache"]["checkpoint_reused_records"] == 1
    assert not checkpoint.exists()


def test_street_only_skips_route_context_and_is_deterministic(tmp_path):
    graph = build_graph()
    df = pd.DataFrame([
        {
            "id": 1,
            "via": "MAIN ALPHA",
            "logradouro_geosampa": "MAIN ALPHA",
            "de": "DE STREET",
            "ate": "ATE STREET",
        }
    ])
    first = make_resolver(tmp_path / "first", graph)
    audit_a, _, report_a = audit_dataframe(df, graph, resolver=first, progress=False, street_only=True)
    second = make_resolver(tmp_path / "second", graph)
    audit_b, _, report_b = audit_dataframe(df, graph, resolver=second, progress=False, street_only=True)

    assert audit_a.iloc[0]["route_context_status"] == "SKIPPED_ROUTE_CONTEXT"
    assert audit_a.iloc[0]["de_resolution_status"] == "NAO_AVALIADO"
    assert report_a["cache"]["intersection_queries"] == 0
    assert audit_a[["candidato_recomendado", "confianca", "street_review_reasons"]].to_dict("records") == audit_b[["candidato_recomendado", "confianca", "street_review_reasons"]].to_dict("records")
    assert report_a["recommended_high"] == report_b["recommended_high"]


def test_geojson_signature_invalidates_diagnostic_cache(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    graph = build_graph()
    source = tmp_path / "geo.json"
    source.write_text("one", encoding="utf-8")
    resolver = StreetResolver(
        graph,
        normalizer=simple_normalizer,
        text_corrector=lambda value: value,
        aliases_path=tmp_path / "aliases.csv",
        cache_path=tmp_path / "cache.pkl",
        source_path=source,
    )
    resolver.resolve("NEAR", reference=Point(50, 300))
    resolver.save_cache()
    source.write_text("two-different", encoding="utf-8")
    invalidated = StreetResolver(
        graph,
        normalizer=simple_normalizer,
        text_corrector=lambda value: value,
        aliases_path=tmp_path / "aliases.csv",
        cache_path=tmp_path / "cache.pkl",
        source_path=source,
    )
    invalidated.resolve("NEAR", reference=Point(50, 300))

    assert invalidated.cache_stats["lexical_cache_hits"] == 0
    assert invalidated.cache_stats["geographic_cache_misses"] >= 1
