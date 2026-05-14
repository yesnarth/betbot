"""
BetBot Streamlit dashboard — thin orchestrator.

Run locally:
    streamlit run betbot_dashboard/app.py

Talks to the FastAPI backend (BETBOT_API_URL, default http://localhost:8000).
Two modes:
  - Manual scan (zero AI, free, deterministic) — uses the blended Poisson model
  - AI agent (requires ANTHROPIC_API_KEY) — same data + Claude reasoning

Each tab section lives in its own module under `sections/`. This file only
wires page chrome (config, CSS, health probe), the sidebar, and the tab
hierarchy together.
"""
from __future__ import annotations

import streamlit as st

from betbot_dashboard.api_client import API_URL, api_get, api_post
from betbot_dashboard.components.sidebar import render_sidebar
from betbot_dashboard.sections.decision import (
    render_ai_agent_tab,
    render_local_agent_tab,
    render_scan_tab,
)
from betbot_dashboard.sections.history import render_history_tab
from betbot_dashboard.sections.matches import (
    render_events_tab,
    render_pending_tab,
    render_validate_tab,
)
from betbot_dashboard.sections.performance import (
    render_capital_tab,
    render_roi_tab,
)
from betbot_dashboard.sections.system import (
    render_basket_tab,
    render_calibrator_tab,
    render_sources_tab,
    render_tennis_tab,
)
from betbot_dashboard.styles import inject_css


# ---------------------------------------------------------------------------
# Page chrome
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="BetBot Dashboard",
    page_icon="⚽",
    layout="wide",
    initial_sidebar_state="expanded",
)
inject_css()

st.title("⚽ BetBot Dashboard")
st.caption("Pronostiqueur quantitatif — modèle Dixon-Coles + xG + ELO + agent local")

# Health is fetched once — needed by the sidebar and several tabs
try:
    health = api_get("/health")
except Exception as exc:
    st.error(f"API injoignable sur {API_URL} : {exc}")
    st.info("Vérifie que les containers Docker tournent : `docker compose ps`")
    st.stop()

agent_enabled = bool(health.get("agent_enabled"))

# Sidebar collects global filters and surfaces KPIs / quick actions
filters = render_sidebar(health, agent_enabled, api_post)


# ---------------------------------------------------------------------------
# Top-level tabs (5 sections, each with sub-tabs where needed)
# ---------------------------------------------------------------------------

section_decision, section_matches, section_perf, section_history, section_system = st.tabs([
    "🎯 Décision",
    "📅 Matchs",
    "📊 Performance",
    "📜 Historique",
    "⚙️ Système",
])

with section_decision:
    st.caption(
        "**Aperçus de modèle** — utilise ces 3 outils pour explorer ce que le bot "
        "trouverait MAINTENANT. Ces previews **ne sauvegardent rien par défaut**. "
        "Pour la file de validation officielle alimentée par le worker auto, va "
        "dans **📅 Matchs → 🔔 Picks à valider**."
    )
    tab_scan, tab_local, tab_agent = st.tabs([
        "🎯 Scan manuel",
        "🧠 Agent local",
        "🤖 Agent IA (Claude)",
    ])
    with tab_scan:
        render_scan_tab(filters, health)
    with tab_local:
        render_local_agent_tab(filters, health)
    with tab_agent:
        render_ai_agent_tab(filters, agent_enabled)

with section_matches:
    st.caption("Matchs disponibles, picks à valider, et bets en attente de résolution.")
    tab_events, tab_validate, tab_pending = st.tabs([
        "📅 Matchs disponibles",
        "🔔 Picks à valider",
        "⏳ Paris en attente",
    ])
    with tab_events:
        render_events_tab(filters)
    with tab_validate:
        render_validate_tab(health)
    with tab_pending:
        render_pending_tab()

with section_perf:
    st.caption("ROI, hit rate, CLV et gestion du bankroll.")
    tab_roi, tab_capital = st.tabs([
        "📊 ROI / Performance",
        "💰 Capital",
    ])
    with tab_roi:
        render_roi_tab()
    with tab_capital:
        render_capital_tab()

with section_history:
    render_history_tab()

with section_system:
    st.caption("État des sources, calibrateur ML et modèles dédiés (tennis ELO, basket pace+rating).")
    tab_sources, tab_calibrator, tab_tennis, tab_basket = st.tabs([
        "🔌 Sources",
        "🎚️ Calibrateur ML",
        "🎾 Modèle tennis",
        "🏀 Modèle basket",
    ])
    with tab_sources:
        render_sources_tab()
    with tab_calibrator:
        render_calibrator_tab()
    with tab_tennis:
        render_tennis_tab(health)
    with tab_basket:
        render_basket_tab(health)
