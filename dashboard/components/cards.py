from __future__ import annotations

import streamlit as st

from dashboard.utils.formatting import escapar


def page_header(title: str, description: str, eyebrow: str | None = None) -> None:
    kicker = f'<div class="section-kicker">{escapar(eyebrow)}</div>' if eyebrow else ""
    page_key = {
        "Home": "home",
        "Consulta de Via": "query",
        "Proteção de Recapes": "protection",
        "Mapa": "map",
        "Auditoria": "audit",
        "Qualidade": "quality",
        "Pipeline": "pipeline",
        "Sobre": "about",
    }.get(title, "page")
    st.markdown(
        f'<div class="page-header page-header--{page_key}">{kicker}<h1>{escapar(title)}</h1><p>{escapar(description)}</p></div>',
        unsafe_allow_html=True,
    )


def metric_card(
    label: str,
    value: str,
    context: str,
    *,
    primary: bool = False,
    help_text: str | None = None,
    delta: str | None = None,
) -> None:
    hint = f' title="{escapar(help_text)}"' if help_text else ""
    delta_html = f'<div class="metric-card__delta">{escapar(delta)}</div>' if delta else ""
    kind = " metric-card--primary" if primary else ""
    st.markdown(
        f'<div class="metric-card{kind}"><div class="metric-card__label"{hint}>{escapar(label)} <span aria-label="Ajuda">ⓘ</span></div>'
        f'<div class="metric-card__value">{escapar(value)}</div><div class="metric-card__context">{escapar(context)}</div>{delta_html}</div>',
        unsafe_allow_html=True,
    )
