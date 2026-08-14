from __future__ import annotations

import pandas as pd
import streamlit as st

from dashboard.components.cards import page_header
from dashboard.components.operational_ui import horizontal_bars, metric_row, section_title, status_badge, warning_panel
from dashboard.services.operational_dashboard import OperationalContext
from dashboard.utils.formatting import numero, percentual


def _percent(value: int, total: int) -> str:
    return percentual(value / total * 100) if total else "0,0%"


def render(context: OperationalContext) -> None:
    page_header("Qualidade", "Métricas agregadas do acervo operacional e causas observáveis nas famílias shadow, validator e consensus.", "Qualidade de dados")
    frame = context.recapes.copy()
    total = len(frame)
    official = int(frame["quality_code"].eq("OFFICIAL").sum())
    high = int(frame["quality_code"].eq("SHADOW_HIGH").sum())
    medium = int(frame["quality_code"].eq("SHADOW_MEDIUM").sum())
    estimated = int(frame["quality_code"].eq("ESTIMATED").sum())
    unresolved = int(frame["quality_code"].eq("UNRESOLVED").sum())
    metric_row(
        [
            ("Oficial", _percent(official, total), f"{numero(official)} registros"),
            ("Shadow alta", _percent(high, total), f"{numero(high)} registros"),
            ("Shadow média", _percent(medium, total), f"{numero(medium)} registros"),
            ("Estimada / não resolvida", _percent(estimated + unresolved, total), f"{numero(estimated + unresolved)} registros"),
        ],
        primary_index=0,
    )
    st.caption(f"Base: {numero(total)} recapes do recape_clean.csv. Geometria oficial nunca é substituída por shadow nesta leitura.")

    left, right = st.columns([1, 1], gap="large")
    with left:
        section_title("Distribuição de geometria", "status de apresentação")
        quality = frame["quality_label"].value_counts().rename_axis("camada").reset_index(name="recapes")
        quality["%"] = quality["recapes"].map(lambda value: _percent(int(value), total))
        horizontal_bars(list(zip(quality["camada"], quality["recapes"])), total)
        st.dataframe(quality, hide_index=True, use_container_width=True)
    with right:
        section_title("Proteção temporal", "mesma referência explícita")
        protection = frame["protection_status"].value_counts().rename_axis("status").reset_index(name="recapes")
        protection["Status"] = protection["status"].map(status_badge)
        protection["%"] = protection["recapes"].map(lambda value: _percent(int(value), total))
        st.markdown(protection[["Status", "recapes", "%"]].to_html(index=False, escape=False, classes="gf-html-table"), unsafe_allow_html=True)

    section_title("Causas e sinais técnicos", "campos disponíveis nos artefatos shadow")
    root_causes = frame["root_causes"].astype(str).str.strip().replace({"": "Não informado", "nan": "Não informado"}).value_counts().rename_axis("causa").reset_index(name="recapes")
    if len(root_causes) > 20:
        root_causes = pd.concat([root_causes.head(19), pd.DataFrame([{ "causa": "Outras", "recapes": int(root_causes.iloc[19:]["recapes"].sum()) }])], ignore_index=True)
    horizontal_bars(list(zip(root_causes.head(10)["causa"], root_causes.head(10)["recapes"])), total, accent="#F0C56C")
    st.dataframe(root_causes, hide_index=True, use_container_width=True)

    causes = pd.DataFrame(
        [
            {"Família": "Validator", "registros com classe": int(frame["validation_class"].astype(str).str.strip().ne("").sum()), "cobertura": _percent(int(frame["validation_class"].astype(str).str.strip().ne("").sum()), total)},
            {"Família": "Consensus", "registros com classe": int(frame["consensus_class"].astype(str).str.strip().ne("").sum()), "cobertura": _percent(int(frame["consensus_class"].astype(str).str.strip().ne("").sum()), total)},
            {"Família": "Root causes shadow", "registros com causa": int(frame["root_causes"].astype(str).str.strip().replace("nan", "").ne("").sum()), "cobertura": _percent(int(frame["root_causes"].astype(str).str.strip().replace("nan", "").ne("").sum()), total)},
        ]
    )
    st.dataframe(causes, hide_index=True, use_container_width=True)
    if unresolved:
        warning_panel("Há geometrias não resolvidas", f"{numero(unresolved)} registros permanecem sem geometria oficial ou shadow exibível.", code="UNRESOLVED_GEOMETRY")
