"""GeoFusion visual system: dark-first, compact and information-led."""
from __future__ import annotations

import streamlit as st


TOKENS = {
    "ink": "#E8EEF7",
    "muted": "#8FA1B7",
    "surface": "#101827",
    "surface_2": "#151F30",
    "canvas": "#080D14",
    "border": "#223149",
    "border_strong": "#334766",
    "blue": "#77A8FF",
    "success": "#69D3A2",
    "warning": "#F0C56C",
    "danger": "#F17C78",
}


def inject_theme() -> None:
    st.markdown(
        f"""
        <style>
          :root {{
            --gf-ink: {TOKENS['ink']}; --gf-muted: {TOKENS['muted']};
            --gf-surface: {TOKENS['surface']}; --gf-surface-2: {TOKENS['surface_2']};
            --gf-canvas: {TOKENS['canvas']}; --gf-border: {TOKENS['border']};
            --gf-border-strong: {TOKENS['border_strong']}; --gf-blue: {TOKENS['blue']};
            --gf-success: {TOKENS['success']}; --gf-warning: {TOKENS['warning']}; --gf-danger: {TOKENS['danger']};
            --gf-radius: 8px; --gf-gap: 1.25rem;
            --gf-content-max: 1740px; --gf-map-height: clamp(520px, 68vh, 720px);
          }}
          .stApp {{ background: var(--gf-canvas); color: var(--gf-ink); }}
          [data-testid="stHeader"] {{ background: rgba(8,13,20,.96); }}
          .block-container {{ width:100%; max-width:var(--gf-content-max); padding:1rem 2rem 3.5rem; }}
          [data-testid="stSidebar"] {{ background: #0B121D; border-right: 1px solid var(--gf-border); }}
          [data-testid="stSidebar"] > div:first-child {{ padding-top: 1rem; }}
          [data-testid="stSidebar"] h3 {{ color:var(--gf-ink); letter-spacing:-.02em; margin-bottom:.1rem; }}
          [data-testid="stSidebar"] [data-testid="stCaptionContainer"] {{ color:var(--gf-muted); }}
          [data-testid="stSidebar"] [role="radiogroup"] {{ gap: 2px; }}
          [data-testid="stSidebar"] [role="radiogroup"] label {{ border-left:2px solid transparent; border-radius:5px; padding:5px 8px; margin:0; color:var(--gf-muted); transition:background .15s ease,color .15s ease; }}
          [data-testid="stSidebar"] [role="radiogroup"] label:hover {{ background:#111D2D; color:var(--gf-ink); }}
          [data-testid="stSidebar"] [role="radiogroup"] label:has(input:checked) {{ background:#12233A; border-left-color:var(--gf-blue); color:var(--gf-ink); }}
          .gf-topbar {{ display:flex; align-items:center; justify-content:space-between; gap:20px; margin:0 0 18px; padding-bottom:11px; border-bottom:1px solid rgba(51,71,102,.6); }}
          .gf-brand {{ display:flex; align-items:center; gap:10px; color:var(--gf-ink); font-weight:760; letter-spacing:-.025em; font-size:1rem; }}
          .gf-brand-mark {{ width:24px; height:24px; border-radius:6px; background:var(--gf-blue); display:inline-block; box-shadow:inset 0 0 0 6px #DCE9FF; }}
          .gf-topbar-note {{ color:var(--gf-muted); font-size:.75rem; white-space:nowrap; }}
          .page-header {{ margin:0 0 18px; max-width:1280px; }}
          .page-header h1 {{ color:var(--gf-ink); letter-spacing:-.045em; font-size:1.9rem; line-height:1.12; margin:0 0 7px; }}
          .page-header p {{ color:var(--gf-muted); margin:0; max-width:1180px; font-size:.88rem; line-height:1.5; }}
          .section-kicker {{ color:var(--gf-muted); font-size:.64rem; font-weight:760; letter-spacing:.13em; text-transform:uppercase; margin:0 0 7px; }}
          .metric-card {{ background:var(--gf-surface); border:1px solid var(--gf-border); border-radius:var(--gf-radius); padding:14px 15px; min-height:102px; box-shadow:0 4px 18px rgba(0,0,0,.08); }}
          .metric-card--primary {{ border-color:#345B98; border-left:3px solid var(--gf-blue); background:#101D31; min-height:102px; }}
          .metric-card__label {{ color:var(--gf-muted); font-size:.72rem; font-weight:650; letter-spacing:.01em; }}
          .metric-card__value {{ color:var(--gf-ink); letter-spacing:-.045em; font-size:1.9rem; font-weight:760; line-height:1.1; margin:9px 0 5px; }}
          .metric-card--primary .metric-card__value {{ font-size:2.2rem; }}
          .metric-card__context {{ color:var(--gf-muted); font-size:.72rem; line-height:1.4; }}
          .metric-card__delta {{ color:var(--gf-success); font-size:.72rem; font-weight:650; margin-top:7px; }}
          .gf-section-title {{ display:flex; align-items:baseline; justify-content:space-between; gap:16px; color:var(--gf-ink); font-size:1rem; font-weight:720; margin:24px 0 10px; }}
          .gf-section-context {{ color:var(--gf-muted); font-size:.7rem; font-weight:500; }}
          .gf-badge {{ display:inline-flex; align-items:center; gap:6px; border-radius:999px; padding:3px 9px; font-size:.69rem; font-weight:680; white-space:nowrap; border:1px solid transparent; }}
          .gf-badge__dot {{ width:6px; height:6px; border-radius:50%; background:currentColor; }}
          .gf-badge--success {{ color:#7AE0AF; background:#123126; border-color:#245640; }}
          .gf-badge--info {{ color:#9BC1FF; background:#152A4A; border-color:#2A4D82; }}
          .gf-badge--warning {{ color:#F4D58D; background:#352B18; border-color:#665126; }}
          .gf-badge--danger {{ color:#FF9A95; background:#3A1D21; border-color:#6D3034; }}
          .gf-badge--muted {{ color:#B1BECE; background:#202A39; border-color:#37465A; }}
          .gf-detail-label {{ color:var(--gf-muted); font-size:.63rem; text-transform:uppercase; letter-spacing:.09em; font-weight:700; margin:7px 0 4px; }}
          .gf-detail-value {{ color:var(--gf-ink); font-size:.82rem; line-height:1.35; overflow-wrap:anywhere; margin-bottom:8px; }}
          .gf-warning {{ background:#33271B; border:1px solid #69512B; border-left:3px solid var(--gf-warning); border-radius:var(--gf-radius); padding:12px 14px; color:#F5D994; font-size:.8rem; line-height:1.5; margin:10px 0; }}
          .gf-warning strong {{ color:#FFE4A9; }}
          .gf-warning code {{ color:#FFD785; }}
          .filter-shell, .detail-panel {{ background:var(--gf-surface); border:1px solid var(--gf-border); border-radius:var(--gf-radius); padding:14px 16px; box-shadow:0 4px 18px rgba(0,0,0,.06); }}
          .filter-shell {{ background:#0D1624; }}
          .empty-state {{ background:var(--gf-surface); border:1px dashed var(--gf-border-strong); border-radius:var(--gf-radius); color:var(--gf-muted); padding:30px 22px; text-align:center; line-height:1.55; }}
          .empty-state strong {{ color:var(--gf-ink); }}
          .gf-table-shell {{ background:var(--gf-surface); border:1px solid var(--gf-border); border-radius:var(--gf-radius); padding:4px; overflow:hidden; }}
          [data-testid="stDataFrame"] {{ border:1px solid var(--gf-border); border-radius:var(--gf-radius); overflow:hidden; }}
          .gf-html-table {{ width:100%; border-collapse:collapse; background:var(--gf-surface); border:1px solid var(--gf-border); border-radius:var(--gf-radius); overflow:hidden; font-size:.76rem; }}
          .gf-html-table th {{ color:var(--gf-muted); text-align:left; font-size:.63rem; letter-spacing:.08em; text-transform:uppercase; padding:9px 11px; border-bottom:1px solid var(--gf-border); background:#0D1624; }}
          .gf-html-table td {{ color:var(--gf-ink); padding:9px 11px; border-bottom:1px solid #1B2739; vertical-align:top; }}
          .gf-html-table tr:last-child td {{ border-bottom:0; }}
          .gf-table-caption {{ color:var(--gf-muted); font-size:.7rem; margin:6px 0 10px; }}
          [data-testid="stDeckGlJsonChart"],
          [data-testid="stFullScreenFrame"]:has(> [data-testid="stDeckGlJsonChart"]),
          [data-testid="element-container"]:has(> [data-testid="stFullScreenFrame"] > [data-testid="stDeckGlJsonChart"]) {{
            height:var(--gf-map-height) !important;
            min-height:var(--gf-map-height) !important;
          }}
          [data-testid="stDeckGlJsonChart"] canvas {{ height:100% !important; min-height:100% !important; }}
          body:has(.page-header--query) {{ --gf-map-height:clamp(340px, 45vh, 520px); }}
          body:has(.page-header--pipeline) .page-header,
          body:has(.page-header--about) .page-header {{ max-width:980px; }}
          body:has(.page-header--pipeline) .page-header p,
          body:has(.page-header--about) .page-header p {{ max-width:900px; }}
          .gf-summary {{ background:#0D1624; border-left:3px solid var(--gf-blue); border-radius:var(--gf-radius); padding:13px 15px; }}
          .gf-summary__title {{ color:var(--gf-ink); font-size:.94rem; font-weight:720; }}
          .gf-summary__meta {{ color:var(--gf-muted); font-size:.75rem; margin-top:4px; line-height:1.45; }}
          .gf-bar-list {{ display:grid; gap:9px; margin:5px 0 2px; }}
          .gf-bar-row {{ display:grid; grid-template-columns:132px minmax(80px,1fr) 72px; gap:10px; align-items:center; font-size:.74rem; }}
          .gf-bar-label {{ color:var(--gf-ink); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
          .gf-bar-track {{ height:7px; background:#202B3A; border-radius:999px; overflow:hidden; }}
          .gf-bar-fill {{ height:100%; background:var(--gf-blue); border-radius:999px; }}
          .gf-bar-value {{ color:var(--gf-muted); text-align:right; font-variant-numeric:tabular-nums; }}
          .gf-legend {{ display:flex; flex-wrap:wrap; gap:8px 12px; color:var(--gf-muted); font-size:.7rem; }}
          .gf-legend-item {{ display:inline-flex; align-items:center; gap:5px; }}
          .gf-legend-swatch {{ width:8px; height:8px; border-radius:50%; display:inline-block; }}
          .gf-action-card {{ height:100%; display:flex; flex-direction:column; justify-content:space-between; }}
          .gf-principles {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px 14px; }}
          .gf-principle {{ padding:12px 14px; border-left:2px solid var(--gf-border-strong); background:#0D1624; border-radius:5px; }}
          .gf-principle strong {{ color:var(--gf-ink); font-size:.83rem; }}
          .gf-principle span {{ color:var(--gf-muted); font-size:.77rem; line-height:1.45; display:block; margin-top:4px; }}
          .pipeline-step {{ position:relative; background:#0D1624; border:1px solid var(--gf-border); border-left:3px solid var(--gf-border-strong); border-radius:6px; padding:11px 14px; margin:0 0 8px; }}
          .pipeline-step:not(:last-of-type)::after {{ content:""; position:absolute; left:16px; bottom:-9px; width:1px; height:8px; background:var(--gf-border-strong); }}
          .pipeline-step h3 {{ color:var(--gf-ink); font-size:.84rem; margin:0 0 4px; letter-spacing:-.01em; }}
          .pipeline-step p {{ color:var(--gf-muted); font-size:.75rem; line-height:1.45; margin:0; }}
          [data-testid="stExpander"] {{ border:1px solid var(--gf-border); border-radius:var(--gf-radius); background:var(--gf-surface); }}
          .stButton > button, div[data-testid="stDownloadButton"] > button {{ border-radius:6px; border-color:var(--gf-border-strong); font-weight:650; min-height:2.25rem; white-space:nowrap; line-height:1.15; overflow-wrap:normal; }}
          .stTextInput input, .stNumberInput input, .stSelectbox div[data-baseweb="select"] > div {{ background:#0D1521; border-color:var(--gf-border); }}
          [data-testid="stCaptionContainer"] {{ color:var(--gf-muted); font-size:.72rem; }}
          label, [data-testid="stWidgetLabel"] {{ color:var(--gf-muted) !important; font-size:.72rem !important; }}
          @media (min-width: 1500px) {{
            .page-header h1 {{ font-size:2.08rem; }}
            .page-header p {{ font-size:.95rem; line-height:1.55; }}
            .gf-section-title {{ font-size:1.08rem; }}
            .metric-card__label, .metric-card__context {{ font-size:.78rem; }}
            .metric-card__value {{ font-size:2.08rem; }}
            .metric-card--primary .metric-card__value {{ font-size:2.38rem; }}
            .gf-detail-value {{ font-size:.88rem; }}
            .gf-html-table {{ font-size:.82rem; }}
            .gf-html-table th {{ font-size:.67rem; }}
            .gf-html-table th, .gf-html-table td {{ padding:10px 13px; }}
            [data-testid="stDataFrame"] [role="gridcell"], [data-testid="stDataFrame"] [role="columnheader"] {{ font-size:.82rem !important; }}
          }}
          @media (max-width: 1440px) {{ [data-testid="stSidebar"][aria-expanded="true"] {{ width:300px !important; }} }}
          @media (max-width: 1100px) {{ [data-testid="stSidebar"][aria-expanded="true"] {{ width:260px !important; }} .block-container {{ padding-left:1.25rem; padding-right:1.25rem; }} .page-header {{ max-width:none; }} .gf-bar-row {{ grid-template-columns:116px minmax(60px,1fr) 64px; }} }}
          @media (max-width: 900px) {{ .gf-topbar {{ align-items:flex-start; flex-direction:column; gap:5px; }} .gf-topbar-note {{ white-space:normal; }} .gf-principles {{ grid-template-columns:1fr; }} .metric-card {{ min-height:94px; }} }}
          @media (max-width: 700px) {{ [data-testid="stSidebar"][aria-expanded="true"] {{ width:220px !important; }} .block-container {{ padding-left:.85rem; padding-right:.85rem; }} .page-header h1 {{ font-size:1.6rem; }} .gf-section-title {{ align-items:flex-start; flex-direction:column; gap:3px; }} .gf-bar-row {{ grid-template-columns:104px minmax(50px,1fr) 60px; gap:7px; }} .gf-html-table {{ font-size:.7rem; }} .gf-html-table th, .gf-html-table td {{ padding:7px 8px; }} :root {{ --gf-map-height:480px; }} }}
        </style>
        """,
        unsafe_allow_html=True,
    )
