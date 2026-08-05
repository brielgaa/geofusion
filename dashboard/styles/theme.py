"""Sistema visual discreto para uma interface operacional de alta densidade."""
from __future__ import annotations

import streamlit as st


TOKENS = {
    "ink": "#E7EEF8",
    "muted": "#93A4B8",
    "surface": "#111827",
    "canvas": "#090D14",
    "border": "#253247",
    "border_strong": "#34445D",
    "blue": "#6EA8FE",
    "blue_soft": "#172A46",
    "success": "#16794A",
    "warning": "#A45C00",
    "danger": "#C23A30",
    "review": "#6E3BB7",
}


def inject_theme() -> None:
    """Aplica apenas seletores específicos; não força cor em elementos globais."""
    st.markdown(
        f"""
        <style>
          :root {{
            --obras-ink: {TOKENS['ink']};
            --obras-muted: {TOKENS['muted']};
            --obras-surface: {TOKENS['surface']};
            --obras-canvas: {TOKENS['canvas']};
            --obras-border: {TOKENS['border']};
            --obras-blue: {TOKENS['blue']};
          }}
          .stApp {{ background: var(--obras-canvas); }}
          [data-testid="stHeader"] {{ background: rgba(9,13,20,.92); }}
          .block-container {{ max-width: 1680px; padding-top: 1.35rem; padding-bottom: 3rem; }}
          .obras-topbar {{ display:flex; align-items:center; justify-content:space-between; gap:16px; margin:0 0 22px; }}
          .obras-brand {{ display:flex; align-items:center; gap:10px; font-weight:700; color:var(--obras-ink); letter-spacing:-.02em; }}
          .obras-brand-mark {{ width:26px; height:26px; border-radius:8px; background:var(--obras-blue); display:inline-block; box-shadow:inset 0 0 0 6px #dbeafe; }}
          .obras-nav-note {{ color:var(--obras-muted); font-size:.82rem; white-space:nowrap; }}
          .page-header {{ margin:0 0 18px; }}
          .page-header h1 {{ color:var(--obras-ink); letter-spacing:-.035em; font-size:1.62rem; line-height:1.25; margin:0 0 5px; }}
          .page-header p {{ color:var(--obras-muted); margin:0; font-size:.94rem; }}
          .section-kicker {{ color:var(--obras-muted); font-size:.72rem; font-weight:700; letter-spacing:.08em; text-transform:uppercase; margin-bottom:7px; }}
          .metric-card {{ background:var(--obras-surface); border:1px solid var(--obras-border); border-radius:12px; padding:16px; min-height:118px; }}
          .metric-card--primary {{ min-height:160px; padding:20px; border-color:#294A7A; background:linear-gradient(135deg,#111827 0%,#101D33 100%); }}
          .metric-card__label {{ color:var(--obras-muted); font-size:.78rem; font-weight:650; display:flex; align-items:center; gap:5px; }}
          .metric-card__value {{ color:var(--obras-ink); letter-spacing:-.045em; font-size:2rem; font-weight:720; line-height:1.15; margin:6px 0; }}
          .metric-card--primary .metric-card__value {{ font-size:2.65rem; }}
          .metric-card__context {{ color:var(--obras-muted); font-size:.8rem; line-height:1.4; }}
          .metric-card__delta {{ color:{TOKENS['success']}; font-size:.8rem; font-weight:650; }}
          .status-badge {{ display:inline-flex; align-items:center; gap:6px; border-radius:999px; padding:3px 8px; font-size:.75rem; font-weight:650; white-space:nowrap; }}
          .status-badge__dot {{ height:6px; width:6px; border-radius:50%; background:currentColor; }}
          .filter-shell {{ background:var(--obras-surface); border:1px solid var(--obras-border); border-radius:12px; padding:14px 16px 8px; margin:4px 0 16px; }}
          .filter-summary {{ color:var(--obras-muted); font-size:.8rem; margin:4px 0 10px; }}
          .active-chip {{ display:inline-block; background:#1B2638; border:1px solid #30415B; color:#B8C6D9; border-radius:999px; font-size:.73rem; padding:3px 8px; margin:0 5px 5px 0; }}
          .detail-panel {{ background:var(--obras-surface); border:1px solid var(--obras-border); border-radius:12px; padding:18px; }}
          .detail-title {{ color:var(--obras-ink); margin:0; font-size:1.12rem; letter-spacing:-.02em; }}
          .detail-grid-label {{ color:var(--obras-muted); font-size:.71rem; font-weight:700; letter-spacing:.06em; text-transform:uppercase; margin:0 0 4px; }}
          .detail-grid-value {{ color:var(--obras-ink); font-size:.88rem; margin:0 0 13px; word-break:break-word; }}
          .empty-state {{ background:var(--obras-surface); border:1px dashed var(--obras-border-strong, #D0D5DD); border-radius:12px; color:var(--obras-muted); padding:30px 22px; text-align:center; }}
          .pipeline-step {{ position:relative; border-left:2px solid #273A57; padding:0 0 18px 18px; margin-left:6px; }}
          .pipeline-step:last-child {{ padding-bottom:0; }}
          .pipeline-step::before {{ content:""; position:absolute; left:-6px; top:2px; height:10px; width:10px; border-radius:50%; background:var(--obras-blue); }}
          .pipeline-step h3 {{ color:var(--obras-ink); font-size:.95rem; margin:0 0 4px; }}
          .pipeline-step p {{ color:var(--obras-muted); font-size:.82rem; margin:0; }}
          [data-testid="stDataFrame"] {{ border:1px solid var(--obras-border); border-radius:10px; overflow:hidden; }}
          [data-testid="stExpander"] {{ border:1px solid var(--obras-border); border-radius:10px; background:var(--obras-surface); }}
          .stButton > button {{ border-radius:8px; border-color:var(--obras-border-strong); font-weight:600; }}
          div[data-testid="stDownloadButton"] > button {{ border-radius:8px; font-weight:600; }}
          @media (max-width: 900px) {{
            .block-container {{ padding-left:1rem; padding-right:1rem; }}
            .obras-topbar {{ align-items:flex-start; flex-direction:column; gap:5px; }}
            .obras-nav-note {{ white-space:normal; }}
          }}
        </style>
        """,
        unsafe_allow_html=True,
    )
