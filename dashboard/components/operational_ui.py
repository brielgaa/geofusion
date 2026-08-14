"""Small visual primitives shared by the operational dashboard pages."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Iterable

import pandas as pd
import streamlit as st

from dashboard.components.cards import metric_card
from dashboard.utils.formatting import escapar, numero


BADGE_TONES = {
    "ACTIVE": ("Ativa", "success"),
    "EXPIRING_SOON": ("Expira em breve", "warning"),
    "EXPIRED": ("Expirada", "muted"),
    "UNKNOWN_DATE": ("Data desconhecida", "danger"),
    "OFFICIAL": ("Oficial", "success"),
    "SHADOW_HIGH": ("Shadow · alta", "info"),
    "SHADOW_MEDIUM": ("Shadow · média", "warning"),
    "ESTIMATED": ("Estimada", "warning"),
    "UNRESOLVED": ("Não resolvida", "danger"),
    "COMPLETE": ("Completo", "success"),
    "PARTIAL": ("Parcial", "warning"),
    "LIMITED": ("Limitado", "warning"),
    "UNAVAILABLE": ("Indisponível", "danger"),
    "NEEDS_ATTENTION": ("NEEDS_ATTENTION", "danger"),
}


def _value(value: Any, fallback: str = "—") -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return fallback
    if isinstance(value, (date, datetime, pd.Timestamp)):
        return value.strftime("%d/%m/%Y")
    text = str(value).strip()
    return text if text else fallback


def badge(value: Any, *, label: str | None = None, tone: str | None = None) -> None:
    code = str(value or "").upper()
    default_label, default_tone = BADGE_TONES.get(code, (str(value or "—"), "muted"))
    text = label or default_label
    css_tone = tone or default_tone
    st.markdown(
        f'<span class="gf-badge gf-badge--{css_tone}"><span class="gf-badge__dot"></span>{escapar(text)}</span>',
        unsafe_allow_html=True,
    )


def status_badge(value: Any, *, label: str | None = None) -> str:
    code = str(value or "").upper()
    text = label or BADGE_TONES.get(code, (str(value or "—"), "muted"))[0]
    tone = BADGE_TONES.get(code, (text, "muted"))[1]
    return f'<span class="gf-badge gf-badge--{tone}"><span class="gf-badge__dot"></span>{escapar(text)}</span>'


def info_grid(items: Iterable[tuple[str, Any]], *, columns: int = 3) -> None:
    values = list(items)
    for start in range(0, len(values), columns):
        row = values[start : start + columns]
        cols = st.columns(len(row))
        for col, (label, value) in zip(cols, row):
            with col:
                st.markdown(f'<div class="gf-detail-label">{escapar(label)}</div><div class="gf-detail-value">{escapar(_value(value))}</div>', unsafe_allow_html=True)


def section_title(title: str, context: str | None = None) -> None:
    suffix = f'<span class="gf-section-context">{escapar(context)}</span>' if context else ""
    st.markdown(f'<div class="gf-section-title"><span>{escapar(title)}</span>{suffix}</div>', unsafe_allow_html=True)


def empty_panel(title: str, message: str) -> None:
    st.markdown(f'<div class="empty-state"><strong>{escapar(title)}</strong><br/><span>{escapar(message)}</span></div>', unsafe_allow_html=True)


def warning_panel(title: str, message: str, *, code: str | None = None) -> None:
    detail = f'<code>{escapar(code)}</code>' if code else ""
    st.markdown(f'<div class="gf-warning"><strong>{escapar(title)}</strong><br/><span>{escapar(message)}</span> {detail}</div>', unsafe_allow_html=True)


def provenance_panel(items: Iterable[Any]) -> None:
    rows = list(items)
    if not rows:
        empty_panel("Proveniência não disponível", "Nenhuma fonte foi retornada para este resultado.")
        return
    table = []
    for item in rows:
        table.append(
            {
                "Campo / valor": _value(getattr(item, "value", "—")),
                "Fonte": _value(getattr(item, "source", "—")),
                "Método": _value(getattr(item, "method", "—")),
                "Confiança": _value(getattr(item, "confidence", "—")),
            }
        )
    st.dataframe(pd.DataFrame(table), hide_index=True, use_container_width=True)


def geometry_quality_panel(geometry: Any) -> None:
    status = getattr(geometry, "status", "UNRESOLVED")
    section_title("Qualidade geométrica")
    cols = st.columns(3)
    with cols[0]:
        st.markdown(status_badge(status), unsafe_allow_html=True)
    with cols[1]:
        st.caption(f"Oficial: {'sim' if getattr(geometry, 'official_wkt', None) else 'não'}")
    with cols[2]:
        st.caption(f"Shadow: {'disponível' if getattr(geometry, 'shadow_wkt', None) else 'não disponível'}")
    if getattr(geometry, "consensus_class", None) or getattr(geometry, "validation_class", None):
        info_grid(
            [
                ("Consensus", getattr(geometry, "consensus_class", None)),
                ("Validação", getattr(geometry, "validation_class", None)),
                ("Fonte oficial", getattr(geometry, "official_source", None)),
            ],
            columns=3,
        )
    for warning in getattr(geometry, "warnings", ()):
        st.caption(f"Aviso: {warning}")


def metric_row(items: list[tuple[str, str, str]], *, primary_index: int | None = None) -> None:
    cols = st.columns(len(items), gap="medium")
    for index, (label, value, context) in enumerate(items):
        with cols[index]:
            metric_card(label, value, context, primary=primary_index == index)


def horizontal_bars(items: Iterable[tuple[str, int]], total: int, *, accent: str = "#77A8FF") -> None:
    """Render a compact comparison bar using already aggregated UI values."""
    values = [(str(label), int(value)) for label, value in items]
    maximum = max((value for _, value in values), default=0)
    rows = []
    for label, value in values:
        width = (value / maximum * 100) if maximum else 0
        share = (value / total * 100) if total else 0
        rows.append(
            f'<div class="gf-bar-row"><span class="gf-bar-label">{escapar(label)}</span>'
            f'<span class="gf-bar-track"><span class="gf-bar-fill" style="width:{width:.2f}%;background:{accent}"></span></span>'
            f'<span class="gf-bar-value">{numero(value)} · {share:.1f}%</span></div>'
        )
    st.markdown(f'<div class="gf-bar-list">{"".join(rows)}</div>', unsafe_allow_html=True)


def compact_count(value: Any) -> str:
    try:
        return numero(int(value))
    except (TypeError, ValueError):
        return "0"
