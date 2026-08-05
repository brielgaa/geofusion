from __future__ import annotations

import streamlit as st

from dashboard.utils.formatting import escapar
from dashboard.utils.status import status_meta


def status_badge(code: str) -> None:
    meta = status_meta(code)
    st.markdown(
        f'<span class="status-badge" style="color:{meta.color};background:{meta.background};">'
        f'<span class="status-badge__dot"></span>{escapar(meta.label)}</span>',
        unsafe_allow_html=True,
    )
