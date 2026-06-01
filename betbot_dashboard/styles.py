"""CSS injection and shared visual primitives."""
from __future__ import annotations

import streamlit as st

_CSS = """
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

  /* Typography */
  html, body, [class*="css"], .stApp, .stMarkdown, .stButton button, .stSelectbox, .stTextInput {
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif !important;
  }

  /* Hide Streamlit chrome */
  #MainMenu, header [data-testid="stToolbar"], footer { visibility: hidden; }
  .stDeployButton { display: none !important; }
  header { background: transparent !important; }

  /* Top padding tighter */
  .block-container { padding-top: 1.2rem !important; max-width: 1280px; }

  /* Header row compact */
  h1 { font-size: 1.65rem !important; font-weight: 700 !important; margin-bottom: 0.2rem !important; letter-spacing: -0.02em; }
  h2 { font-size: 1.25rem !important; font-weight: 600 !important; letter-spacing: -0.01em; }
  h3 { font-size: 1.05rem !important; font-weight: 600 !important; }

  /* Tabs — wider, bolder, cleaner separators */
  [data-baseweb="tab-list"] {
    gap: 4px !important;
    border-bottom: 1px solid #e5e7eb !important;
    padding-bottom: 0 !important;
  }
  [data-baseweb="tab"] {
    background: transparent !important;
    padding: 10px 16px !important;
    border-radius: 8px 8px 0 0 !important;
    font-weight: 500 !important;
    color: #475569 !important;
    transition: all 0.15s ease !important;
  }
  [data-baseweb="tab"]:hover { background: #f1f5f9 !important; color: #0f172a !important; }
  [data-baseweb="tab"][aria-selected="true"] {
    color: #10b981 !important; font-weight: 600 !important; background: #ecfdf5 !important;
  }
  [data-baseweb="tab-highlight"] { background: #10b981 !important; height: 3px !important; }

  /* Sidebar — softer with section cards */
  section[data-testid="stSidebar"] { background: #f8fafc !important; border-right: 1px solid #e5e7eb; }
  section[data-testid="stSidebar"] h2 { font-size: 0.78rem !important; text-transform: uppercase; letter-spacing: 0.06em; color: #64748b !important; font-weight: 600 !important; margin-top: 0.5rem; }
  section[data-testid="stSidebar"] [data-testid="stMetricValue"] { font-size: 1.5rem !important; font-weight: 700 !important; }
  section[data-testid="stSidebar"] [data-testid="stMetricDelta"] { font-size: 0.78rem !important; }
  section[data-testid="stSidebar"] hr { margin: 1.2rem 0 !important; border-color: #e2e8f0 !important; }

  /* Metric tiles */
  [data-testid="stMetric"] {
    background: #ffffff;
    padding: 12px 16px;
    border-radius: 10px;
    border: 1px solid #e5e7eb;
    box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
  }
  [data-testid="stMetricLabel"] { font-size: 0.78rem !important; color: #64748b !important; font-weight: 500 !important; }
  [data-testid="stMetricValue"] { font-weight: 700 !important; color: #0f172a !important; }

  /* Buttons — primary punchier */
  .stButton > button[kind="primary"] {
    background: linear-gradient(135deg, #10b981 0%, #059669 100%) !important;
    border: none !important; font-weight: 600 !important; box-shadow: 0 1px 3px rgba(16, 185, 129, 0.3);
  }
  .stButton > button[kind="primary"]:hover { box-shadow: 0 4px 8px rgba(16, 185, 129, 0.35) !important; transform: translateY(-1px); }
  .stButton > button { border-radius: 8px !important; transition: all 0.15s ease !important; }

  /* Alerts (info / warning / success) */
  [data-testid="stAlert"] { border-radius: 10px !important; border-left-width: 4px !important; padding: 12px 16px !important; }

  /* Empty-state utility (used via st.markdown) */
  .empty-state {
    text-align: center; padding: 36px 24px; background: #f8fafc;
    border: 1px dashed #cbd5e1; border-radius: 12px; color: #64748b;
  }
  .empty-state .icon { font-size: 2.4rem; display: block; margin-bottom: 0.4rem; opacity: 0.7; }
  .empty-state .title { font-weight: 600; color: #334155; font-size: 1rem; margin-bottom: 0.3rem; }
  .empty-state .hint { font-size: 0.88rem; }

  /* Dataframe */
  [data-testid="stDataFrame"] { border-radius: 10px; overflow: hidden; border: 1px solid #e5e7eb; }

  /* Mobile / narrow screens — keep tabs and content usable below ~768px */
  @media (max-width: 768px) {
    .block-container { max-width: 100% !important; padding-left: 0.6rem !important; padding-right: 0.6rem !important; }
    [data-baseweb="tab"] { padding: 8px 10px !important; font-size: 0.85rem !important; }
    h1 { font-size: 1.35rem !important; }
    [data-testid="stMetricValue"] { font-size: 1.2rem !important; }
  }
</style>
"""


def inject_css() -> None:
    """Render the global CSS once per page load."""
    st.markdown(_CSS, unsafe_allow_html=True)


def empty_state(icon: str, title: str, hint: str = "") -> None:
    """Render a polished empty state instead of a flat info alert."""
    st.markdown(
        f"""<div class="empty-state">
            <span class="icon">{icon}</span>
            <div class="title">{title}</div>
            <div class="hint">{hint}</div>
        </div>""",
        unsafe_allow_html=True,
    )
