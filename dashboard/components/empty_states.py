from __future__ import annotations

import streamlit as st

from dashboard.utils.formatting import escapar


def empty_state(title: str, detail: str) -> None:
    st.markdown(
        f'<div class="empty-state"><strong>{escapar(title)}</strong><br/>{escapar(detail)}</div>',
        unsafe_allow_html=True,
    )
