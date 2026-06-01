"""
BetBot Streamlit dashboard — thin orchestrator.

Run locally:
    streamlit run betbot_dashboard/app.py

Talks to the FastAPI backend (BETBOT_API_URL, default http://localhost:8000).

Sections are organized around the user's actual workflow, in order of
expected daily frequency:

    🔔 Mes picks   — the validation queue (daily action)
    📊 Performance — ROI + CLV on resolved bets (look back)
    💰 Capital     — bankroll, deposits, withdrawals (operational)
    🔬 Modèle      — backtest, calibrator, tennis ELO, basket stats (science)
    🛠️ Outils      — manual scans, agent IA, sources health, agent runs (sandbox)

Each tab's render function lives under `sections/`. This file only wires
page chrome, the sidebar, and the tab hierarchy.
"""
from __future__ import annotations

import streamlit as st

from betbot_dashboard.api_client import API_URL, api_get, api_post
from betbot_dashboard.components.sidebar import render_sidebar
from betbot_dashboard.sections.backtest import render_backtest_tab
from betbot_dashboard.sections.decision import (
    render_ai_agent_tab,
    render_local_agent_tab,
    render_scan_tab,
    render_target_parlay_tab,
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
from betbot_dashboard.ui import guarded

# Wrap each section renderer so a backend failure (ApiError) shows a friendly
# message inside that tab instead of crashing the whole page with a traceback.
render_validate_tab = guarded(render_validate_tab)
render_pending_tab = guarded(render_pending_tab)
render_roi_tab = guarded(render_roi_tab)
render_capital_tab = guarded(render_capital_tab)
render_backtest_tab = guarded(render_backtest_tab)
render_calibrator_tab = guarded(render_calibrator_tab)
render_tennis_tab = guarded(render_tennis_tab)
render_basket_tab = guarded(render_basket_tab)
render_scan_tab = guarded(render_scan_tab)
render_local_agent_tab = guarded(render_local_agent_tab)
render_ai_agent_tab = guarded(render_ai_agent_tab)
render_target_parlay_tab = guarded(render_target_parlay_tab)
render_events_tab = guarded(render_events_tab)
render_history_tab = guarded(render_history_tab)


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

# How many picks are currently waiting on user action? Surfaced both in the
# sidebar KPI and in the "Mes picks" landing — drives the user to the
# right tab when there's something to do.
try:
    n_proposed = len(api_get("/predictions/proposed"))
except Exception:
    n_proposed = -1  # silent — not fatal, dashboard still loads

# Sidebar collects global filters and surfaces KPIs / quick actions
filters = render_sidebar(health, agent_enabled, api_post,
                         n_proposed=n_proposed)


# ---------------------------------------------------------------------------
# Top-level tabs — organized by daily-use frequency, not by technical layer.
# ---------------------------------------------------------------------------

section_picks, section_perf, section_capital, section_model, section_tools = st.tabs([
    f"🔔 Mes picks{f' ({n_proposed})' if n_proposed > 0 else ''}",
    "📊 Performance",
    "💰 Capital",
    "🔬 Modèle",
    "🛠️ Outils",
])

with section_picks:
    _scan_hours = health.get("scan_hours") or []
    if _scan_hours:
        _intro = (
            f"**Ton action quotidienne.** Le worker propose ces picks à "
            f"**{' et '.join(_scan_hours)}** (Europe/Paris) ; "
        )
    else:
        _intro = (
            "**Ton action.** Auto-scan désactivé — les picks ici viennent "
            "des scans manuels que tu sauvegardes depuis 🛠️ Outils. "
        )
    st.caption(
        _intro +
        "à toi de confirmer ceux que tu as réellement placés chez ton bookmaker, "
        "ou de skipper. Le solde est débité uniquement à la confirmation."
    )
    tab_validate, tab_pending = st.tabs([
        f"🔔 Picks à valider{f' ({n_proposed})' if n_proposed > 0 else ''}",
        "⏳ Paris en attente",
    ])
    with tab_validate:
        render_validate_tab(health)
    with tab_pending:
        render_pending_tab()

with section_perf:
    st.caption(
        "ROI réel + CLV sur tes paris **résolus**. Pour mesurer la qualité du "
        "modèle indépendamment de la chance, va dans 🔬 Modèle → Backtest."
    )
    render_roi_tab()

with section_capital:
    st.caption(
        "Bankroll, dépôts/retraits, comptes bookmakers. Les mutations sont "
        "protégées contre le double-clic par une clé d'idempotency dérivée du formulaire."
    )
    render_capital_tab()

with section_model:
    st.caption(
        "Qualité et tuning du modèle — sans toucher à tes vrais paris. "
        "**Backtest** = simulation walk-forward sur l'historique. "
        "**Calibrateur** = correction isotonique des probas. "
        "**Tennis/Basket** = preview des modèles dédiés."
    )
    tab_backtest, tab_calibrator, tab_tennis, tab_basket = st.tabs([
        "🧪 Backtest",
        "🎚️ Calibrateur ML",
        "🎾 Tennis ELO",
        "🏀 Basketball",
    ])
    with tab_backtest:
        render_backtest_tab()
    with tab_calibrator:
        render_calibrator_tab()
    with tab_tennis:
        render_tennis_tab(health)
    with tab_basket:
        render_basket_tab(health)

with section_tools:
    st.caption(
        "**Sandbox.** Scans à la demande (preview read-only — n'enregistre rien "
        "par défaut), diagnostic infra et historique des invocations IA. Utilise "
        "ces outils pour explorer, pas pour ton workflow quotidien."
    )
    tab_scan, tab_local, tab_agent, tab_parlay, tab_events, tab_sources, tab_agent_runs = st.tabs([
        "🎯 Scan manuel",
        "🧠 Agent local",
        "🤖 Agent IA (Claude)",
        "🎰 Combiné ×1000",
        "📅 Matchs disponibles",
        "🔌 Sources",
        "📜 Historique IA",
    ])
    with tab_scan:
        render_scan_tab(filters, health)
    with tab_local:
        render_local_agent_tab(filters, health)
    with tab_agent:
        render_ai_agent_tab(filters, agent_enabled)
    with tab_parlay:
        render_target_parlay_tab(filters)
    with tab_events:
        render_events_tab(filters)
    with tab_sources:
        render_sources_tab()
    with tab_agent_runs:
        render_history_tab()
