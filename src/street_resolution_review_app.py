"""Interface Streamlit local para revisao humana de divergencias de rua."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

from street_resolution_review import (
    DECISIONS, DEFAULT_ALIAS_PATH, DEFAULT_APPROVED_PATH, DEFAULT_AUDIT_PATH,
    DEFAULT_REPORT_PATH, DEFAULT_REVIEW_PATH, ReviewDataError, ReviewPersistenceError,
    alternatives_table, approve_cases_in_bulk, batch_approval_preview,
    export_alias_candidates, export_approved, filter_cases,
    load_audit, load_reviews, merge_reviews, review_metrics, save_decision,
    normalize_batch_result, stratified_sample, text_value, write_report,
)


st.set_page_config(page_title="Revisao de divergencias", page_icon="🧭", layout="wide")
st.title("Revisao operacional de divergencias")
st.caption("Camada local de decisao humana. O CSV de auditoria, o resolvedor e o ETL nao sao alterados.")


@st.cache_data(show_spinner="Lendo auditoria...")
def cached_audit(path: str, modified_ns: int, size: int) -> pd.DataFrame:
    del modified_ns, size
    return load_audit(path)


def values(frame: pd.DataFrame, column: str) -> list[str]:
    return sorted(frame[column].dropna().astype(str).unique().tolist())


def number_bounds(frame: pd.DataFrame, column: str) -> tuple[float, float] | None:
    numeric = pd.to_numeric(frame[column], errors="coerce").dropna()
    if numeric.empty:
        return None
    return float(numeric.min()), float(numeric.max())


def current_decision(case: pd.Series) -> dict[str, Any]:
    return {
        "decision": text_value(case.get("decision"), "ADIAR_REVISAO"),
        "manual_resolved_street": text_value(case.get("manual_resolved_street"), ""),
        "manual_codlog": text_value(case.get("manual_codlog"), ""),
        "review_notes": text_value(case.get("review_notes"), ""),
        "approved_for_alias": bool(case.get("approved_for_alias", False)) if pd.notna(case.get("approved_for_alias", pd.NA)) else False,
        "reviewed_at": text_value(case.get("reviewed_at"), ""),
        "reviewed_by": text_value(case.get("reviewed_by"), ""),
    }


def show_value(label: str, value: Any) -> None:
    st.markdown(f"**{label}**  ")
    st.write(text_value(value))


def show_case(case: pd.Series) -> None:
    st.subheader(f"Caso {text_value(case['id'])}")
    score, margin, distance, confidence = st.columns(4)
    score.metric("Score final", text_value(case.get("score_final")))
    margin.metric("Margem top2", text_value(case.get("margem_top2")))
    distance.metric("Distancia (m)", text_value(case.get("distance_m")))
    confidence.metric("Confianca", text_value(case.get("confianca")))

    original, current, recommended = st.columns(3)
    with original:
        st.markdown("### Dado original")
        for label, name in (("ID", "id"), ("Processo", "numero_processo"), ("Via original", "via_original"),
                            ("GeoSampa original", "logradouro_geosampa_original"), ("Nome normalizado", "nome_normalizado"),
                            ("CODLOG informado", "codlog_informado"), ("De", "de_original"), ("Ate", "ate_original"),
                            ("Latitude", "latitude"), ("Longitude", "longitude")):
            show_value(label, case.get(name))
    with current:
        st.markdown("### Resolucao atual")
        for label, name in (("Rua atual", "resolucao_atual"), ("Metodo atual", "metodo_atual"), ("Score atual", "score_atual")):
            show_value(label, case.get(name))
    with recommended:
        st.markdown("### Recomendacao")
        for label, name in (("Candidato recomendado", "candidato_recomendado"), ("CODLOG recomendado", "codlog_recomendado"),
                            ("Metodo recomendado", "metodo_recomendado"), ("Confianca", "confianca"), ("Score final", "score_final"),
                            ("Margem top2", "margem_top2"), ("Distancia (m)", "distance_m"), ("Cobertura de tokens", "token_coverage"),
                            ("Motivo", "motivo_recomendacao")):
            show_value(label, case.get(name))

    st.markdown("### Contexto e alertas")
    context_columns = st.columns(3)
    context_items = (("Status de De", "de_resolution_status"), ("Candidato De", "de_candidate"),
                     ("Intersecao De", "de_intersection_status"), ("Status de Ate", "ate_resolution_status"),
                     ("Candidato Ate", "ate_candidate"), ("Intersecao Ate", "ate_intersection_status"),
                     ("Contexto da rota", "route_context_status"), ("Revisao de rua", "street_review_reasons"),
                     ("Revisao de rota", "route_review_reasons"))
    for index, (label, name) in enumerate(context_items):
        with context_columns[index % 3]:
            show_value(label, case.get(name))

    if text_value(case.get("resolucao_atual"), "") != text_value(case.get("candidato_recomendado"), ""):
        st.warning("A resolucao atual diverge da recomendacao. A decisao humana e obrigatoria para validar a troca.")
    if bool(case.get("street_requires_review")) or bool(case.get("route_requires_review")):
        st.info("Existem sinais de revisao no endereco ou no contexto de rota; eles nao aprovam nem rejeitam a recomendacao por si so.")
    latitude, longitude = case.get("latitude"), case.get("longitude")
    if pd.notna(latitude) and pd.notna(longitude):
        st.link_button("Abrir no Google Maps", f"https://www.google.com/maps?q={latitude},{longitude}")

    st.markdown("### Alternativas")
    raw_alternatives = case.get("alternativas_json")
    alternatives = alternatives_table(raw_alternatives)
    if alternatives.empty:
        if pd.notna(raw_alternatives) and str(raw_alternatives).strip():
            st.warning("As alternativas deste caso possuem JSON invalido ou um formato nao suportado.")
        else:
            st.info("Nenhuma alternativa registrada para este caso.")
    else:
        st.dataframe(alternatives, use_container_width=True, hide_index=True)


def sidebar_filters(frame: pd.DataFrame) -> dict[str, Any]:
    st.sidebar.header("Filtros combinaveis")
    divergence_label = st.sidebar.selectbox("Resolucao divergente", ["Somente divergentes", "Todas", "Somente nao divergentes"])
    divergent = {"Somente divergentes": True, "Todas": None, "Somente nao divergentes": False}[divergence_label]
    confidence = st.sidebar.multiselect("Confianca", values(frame, "confianca"), default=[])
    street_review = st.sidebar.selectbox("Requer revisao de rua", ["Todos", "Sim", "Nao"])
    route_review = st.sidebar.selectbox("Requer revisao de rota", ["Todos", "Sim", "Nao"])
    current_method = st.sidebar.multiselect("Metodo atual", values(frame, "metodo_atual"))
    recommended_method = st.sidebar.multiselect("Metodo recomendado", values(frame, "metodo_recomendado"))
    decision = st.sidebar.selectbox("Status humano", ["Todos", "PENDENTE", *DECISIONS])
    filters: dict[str, Any] = {
        "divergent": divergent, "confidence": confidence, "current_method": current_method,
        "recommended_method": recommended_method, "street_review": {"Todos": None, "Sim": True, "Nao": False}[street_review],
        "route_review": {"Todos": None, "Sim": True, "Nao": False}[route_review],
        "decision": None if decision == "Todos" else decision,
    }
    with st.sidebar.expander("Faixas e contexto"):
        for label, source, target in (("Score final", "score_final", "score_range"), ("Distancia (m)", "distance_m", "distance_range"),
                                      ("Margem top2", "margem_top2", "margin_range")):
            bounds = number_bounds(frame, source)
            if bounds and bounds[0] != bounds[1]:
                filters[target] = st.slider(label, min_value=bounds[0], max_value=bounds[1], value=bounds, key=f"filter_{target}")
            elif bounds:
                filters[target] = bounds
        filters["incomplete"] = {"Todos": None, "Sim": True, "Nao": False}[st.selectbox("Candidato incompleto", ["Todos", "Sim", "Nao"])]
        filters["route_context"] = st.multiselect("Contexto de rota", values(frame, "route_context_status"))
        filters["review_reason"] = st.text_input("Motivo de revisao contem")
    with st.sidebar.expander("Busca"):
        filters["id"] = st.text_input("ID")
        filters["original"] = st.text_input("Nome original")
        filters["current"] = st.text_input("Resolucao atual")
        filters["recommended"] = st.text_input("Candidato recomendado")
        filters["codlog"] = st.text_input("CODLOG")
        filters["free_text"] = st.text_input("Texto livre")
    with st.sidebar.expander("Amostragem estratificada"):
        filters["_sample_active"] = st.checkbox("Revisar somente uma amostra")
        filters["_sample_sizes"] = {
            "HIGH": st.number_input("HIGH", min_value=0, value=50, step=1),
            "MEDIUM": st.number_input("MEDIUM", min_value=0, value=30, step=1),
            "LOW": st.number_input("LOW", min_value=0, value=10, step=1),
            "UNRESOLVED": st.number_input("UNRESOLVED", min_value=0, value=10, step=1),
        }
        filters["_sample_seed"] = st.number_input("Seed", min_value=0, value=42, step=1)
    return filters


def review_editor(case: pd.Series) -> str | None:
    saved = current_decision(case)
    alternatives = alternatives_table(case.get("alternativas_json"))
    options = [text_value(case.get("candidato_recomendado"), "")] + alternatives.get("nome", pd.Series(dtype=str)).dropna().astype(str).tolist()
    options = [item for index, item in enumerate(options) if item and item not in options[:index]]
    with st.form(f"review_form_{case['review_key']}", clear_on_submit=False):
        st.markdown("### Decisao humana")
        default_decision = saved["decision"] if saved["decision"] in DECISIONS else "ADIAR_REVISAO"
        decision = st.selectbox("Decisao", DECISIONS, index=DECISIONS.index(default_decision))
        choice = st.selectbox("Alternativa (para escolher outro candidato)", ["", *options])
        manual_street = st.text_input("Rua resolvida manualmente", value=saved["manual_resolved_street"])
        manual_codlog = st.text_input("CODLOG manual", value=saved["manual_codlog"])
        notes = st.text_area("Notas de revisao", value=saved["review_notes"], height=100)
        approved_for_alias = st.checkbox("Marcar para analise manual como alias", value=saved["approved_for_alias"])
        reviewed_by = st.text_input("Revisado por", value=saved["reviewed_by"])
        reviewed_at = st.text_input("Revisado em (UTC)", value=saved["reviewed_at"] or datetime.now(timezone.utc).isoformat())
        save_button, next_button = st.columns(2)
        submitted = save_button.form_submit_button("Salvar decisao", type="primary")
        save_and_advance = next_button.form_submit_button("Salvar e avancar")
    if not submitted and not save_and_advance:
        return None
    if decision == "ESCOLHER_OUTRO_CANDIDATO" and choice:
        manual_street = choice
        if not manual_codlog and "nome" in alternatives:
            candidate = alternatives[alternatives["nome"].astype(str) == choice]
            if not candidate.empty:
                manual_codlog = text_value(candidate.iloc[0]["CODLOG"], "")
    payload = {
        "decision": decision, "manual_resolved_street": manual_street or pd.NA,
        "manual_codlog": manual_codlog or pd.NA, "review_notes": notes or pd.NA,
        "approved_for_alias": approved_for_alias, "reviewed_by": reviewed_by or pd.NA,
        "reviewed_at": reviewed_at or datetime.now(timezone.utc).isoformat(),
    }
    try:
        save_decision(case, payload, DEFAULT_REVIEW_PATH)
        st.success("Decisao salva sem alterar a auditoria original.")
        return "next" if save_and_advance else "saved"
    except (ReviewDataError, ReviewPersistenceError) as error:
        st.error(str(error))
        return None


def batch_controls(cases: pd.DataFrame, filtered: pd.DataFrame) -> None:
    """Exibe confirmacao explicita antes de gravar decisoes humanas em lote."""
    previous_raw = st.session_state.pop("batch_approval_result", None)
    if previous_raw is not None:
        previous_result = normalize_batch_result(previous_raw)
        changed = previous_result.get("changed", 0)
        approved = previous_result.get("approved", 0)
        ignored = previous_result.get("ignored", 0)
        unresolved = previous_result.get("unresolved", 0)
        elapsed = previous_result.get("elapsed_seconds", 0.0)
        st.success(
            f"{changed} registros alterados: {approved} aprovados "
            f"e {unresolved} marcados como nao resolvidos. "
            f"{ignored} ignorados. Operacao concluida em {elapsed:.2f} s."
        )
    st.markdown("### Aprovacao em lote")
    include_unresolved = st.checkbox("Incluir UNRESOLVED", value=False, key="batch_include_unresolved")
    if st.button("Aprovar todos do filtro", type="secondary"):
        st.session_state.batch_confirmation = {
            "review_keys": filtered["review_key"].tolist(),
            "include_unresolved": include_unresolved,
        }

    confirmation = st.session_state.get("batch_confirmation")
    if not confirmation:
        return
    selected = cases[cases["review_key"].isin(confirmation["review_keys"])].copy()
    preview = batch_approval_preview(selected, confirmation["include_unresolved"])
    full_distribution = selected["confianca"].value_counts().to_dict()
    distribution = " · ".join(f"{name}: {full_distribution.get(name, 0)}" for name in ("HIGH", "MEDIUM", "LOW", "UNRESOLVED"))
    st.warning("Tem certeza que deseja aprovar automaticamente todos os registros atualmente filtrados?")
    st.write(f"**{len(selected)} registros no filtro** — {distribution}")
    if confirmation["include_unresolved"]:
        st.caption(f"A operacao aprovara {preview['approved']} recomendacoes e marcara {preview['marked_unresolved']} casos UNRESOLVED como nao resolvidos.")
    else:
        st.caption(f"A operacao aprovara {preview['approved']} recomendacoes. UNRESOLVED permanecem fora do lote.")
    if preview["ignored"]:
        st.caption(
            f"Ignorados: {preview['skipped_missing_candidate']} sem candidato, "
            f"{preview['skipped_missing_codlog']} sem CODLOG e {preview['skipped_unresolved']} UNRESOLVED nao incluidos."
        )
    cancel, confirm = st.columns(2)
    if cancel.button("Cancelar", key="cancel_batch"):
        del st.session_state.batch_confirmation
        st.rerun()
    if confirm.button("Confirmar", type="primary", key="confirm_batch", disabled=preview["changed"] == 0):
        try:
            result = approve_cases_in_bulk(selected, confirmation["include_unresolved"], DEFAULT_REVIEW_PATH)
            st.session_state.batch_approval_result = normalize_batch_result(result)
            del st.session_state.batch_confirmation
            st.rerun()
        except ReviewPersistenceError as error:
            st.error(str(error))


def main() -> None:
    # Mantem resultados criados por versoes anteriores da aplicacao compativeis
    # antes de qualquer componente Streamlit tentar renderiza-los.
    if "batch_result" in st.session_state and "batch_approval_result" not in st.session_state:
        st.session_state.batch_approval_result = normalize_batch_result(st.session_state.pop("batch_result"))
    elif "batch_approval_result" in st.session_state:
        st.session_state.batch_approval_result = normalize_batch_result(st.session_state.batch_approval_result)
    try:
        stat = DEFAULT_AUDIT_PATH.stat()
        audit = cached_audit(str(DEFAULT_AUDIT_PATH), stat.st_mtime_ns, stat.st_size)
    except (FileNotFoundError, ReviewDataError) as error:
        st.error(str(error))
        st.stop()
    cases = merge_reviews(audit, load_reviews(DEFAULT_REVIEW_PATH))
    metrics = review_metrics(cases)
    metric_columns = st.columns(6)
    metric_columns[0].metric("Divergencias", metrics["total_divergences"])
    metric_columns[1].metric("Pendentes", metrics["total_pending"])
    metric_columns[2].metric("Revisados", metrics["total_reviewed"])
    metric_columns[3].metric("Aprovadas", metrics["decisions"]["APROVAR_RECOMENDACAO"])
    metric_columns[4].metric("Mantidas", metrics["decisions"]["MANTER_RESOLUCAO_ATUAL"])
    metric_columns[5].metric("Nao resolvidas", metrics["decisions"]["MARCAR_COMO_NAO_RESOLVIDO"])
    st.caption("Pendentes por confianca: " + " · ".join(f"{name} {data['pending']}" for name, data in metrics["by_confidence"].items()))

    filters = sidebar_filters(cases)
    filtered = filter_cases(cases, filters)
    if filters["_sample_active"]:
        filtered = stratified_sample(filtered, filters["_sample_sizes"], int(filters["_sample_seed"]))
    st.sidebar.caption(f"{len(filtered):,} casos no filtro")
    if filtered.empty:
        st.warning("Nenhum caso atende aos filtros atuais.")
        return
    batch_controls(cases, filtered)
    keys = filtered["review_key"].tolist()
    if st.session_state.get("selected_review_key") not in keys:
        st.session_state.selected_review_key = keys[0]
    selected_index = keys.index(st.session_state.selected_review_key)

    mode = st.radio("Modo", ["Revisao detalhada", "Tabela geral"], horizontal=True)
    if mode == "Tabela geral":
        table_columns = ["id", "via_original", "resolucao_atual", "candidato_recomendado", "confianca", "score_final", "margem_top2", "distance_m", "street_review_reasons", "decision"]
        st.dataframe(filtered[table_columns], use_container_width=True, hide_index=True)
        labels = [f"{text_value(row.id)} — {text_value(row.via_original)}" for row in filtered.itertuples()]
        picked = st.selectbox("Abrir caso por ID", range(len(labels)), format_func=lambda index: labels[index])
        if st.button("Abrir revisao detalhada"):
            st.session_state.selected_review_key = keys[picked]
            st.rerun()
        return

    st.caption(f"Caso {selected_index + 1} de {len(filtered):,} no filtro atual")
    navigation = st.columns(7)
    if navigation[0].button("Inicio", disabled=selected_index == 0):
        st.session_state.selected_review_key = keys[0]; st.rerun()
    if navigation[1].button("← Anterior", disabled=selected_index == 0):
        st.session_state.selected_review_key = keys[selected_index - 1]; st.rerun()
    if navigation[2].button("Proximo →", disabled=selected_index == len(keys) - 1):
        st.session_state.selected_review_key = keys[selected_index + 1]; st.rerun()
    pending_indexes = [index for index, key in enumerate(keys) if pd.isna(filtered.iloc[index].get("decision")) or not str(filtered.iloc[index].get("decision")).strip()]
    if navigation[3].button("Proximo pendente", disabled=not pending_indexes):
        target = next((index for index in pending_indexes if index > selected_index), pending_indexes[0]); st.session_state.selected_review_key = keys[target]; st.rerun()
    high_pending = [index for index in pending_indexes if filtered.iloc[index]["confianca"] == "HIGH"]
    if navigation[4].button("Proximo HIGH pendente", disabled=not high_pending):
        target = next((index for index in high_pending if index > selected_index), high_pending[0]); st.session_state.selected_review_key = keys[target]; st.rerun()
    jump = navigation[5].number_input("Ir para caso", min_value=1, max_value=len(keys), value=selected_index + 1, step=1)
    if navigation[6].button("Ir"):
        st.session_state.selected_review_key = keys[int(jump) - 1]; st.rerun()

    case = filtered.iloc[selected_index]
    show_case(case)
    saved = review_editor(case)
    if saved == "next":
        st.session_state.selected_review_key = keys[min(selected_index + 1, len(keys) - 1)]
        st.rerun()
    if saved == "saved":
        st.rerun()
    st.divider()
    exports = st.columns(3)
    if exports[0].button("Exportar aprovadas"):
        count = len(export_approved(load_reviews(DEFAULT_REVIEW_PATH), DEFAULT_APPROVED_PATH))
        exports[0].success(f"{count} registros em {DEFAULT_APPROVED_PATH.name}")
    if exports[1].button("Exportar aliases candidatos"):
        count = len(export_alias_candidates(load_reviews(DEFAULT_REVIEW_PATH), DEFAULT_ALIAS_PATH))
        exports[1].success(f"{count} registros em {DEFAULT_ALIAS_PATH.name}")
    if exports[2].button("Gerar relatorio JSON"):
        write_report(cases, DEFAULT_REPORT_PATH)
        exports[2].success(f"Relatorio salvo em {DEFAULT_REPORT_PATH.name}")


if __name__ == "__main__":
    main()
