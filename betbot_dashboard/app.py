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
    render_live_tab,
    render_local_agent_tab,
    render_over_tab,
    render_safe_fast_tab,
    render_scan_tab,
    render_target_parlay_tab,
    render_lottery_parlay_tab,
    render_blind_tab,
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
render_safe_fast_tab = guarded(render_safe_fast_tab)
render_over_tab = guarded(render_over_tab)
render_local_agent_tab = guarded(render_local_agent_tab)
render_ai_agent_tab = guarded(render_ai_agent_tab)
render_target_parlay_tab = guarded(render_target_parlay_tab)
render_lottery_parlay_tab = guarded(render_lottery_parlay_tab)
render_blind_tab = guarded(render_blind_tab)
render_live_tab = guarded(render_live_tab)
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

# What is actually waiting on the user?
#
# Under AUTO_CONFIRM_PICKS the validation queue is always empty by design, so
# "Picks à valider: 0 — rien à faire" was a permanently dead KPI occupying the
# most prominent slot in the UI. What the user genuinely tracks in that mode is
# how many bets are still awaiting a result.
_auto_confirm = bool(health.get("auto_confirm_picks"))
try:
    n_proposed = len(api_get("/predictions/proposed"))
except Exception:
    n_proposed = -1  # silent — not fatal, dashboard still loads
try:
    n_pending = len(api_get("/predictions/pending"))
except Exception:
    n_pending = -1

# Sidebar collects global filters and surfaces KPIs / quick actions
filters = render_sidebar(health, agent_enabled, api_post,
                         n_proposed=n_proposed, n_pending=n_pending)


# ---------------------------------------------------------------------------
# Top-level tabs — organized by daily-use frequency, not by technical layer.
# ---------------------------------------------------------------------------

section_picks, section_perf, section_capital, section_model, section_tools = st.tabs([
    f"🔔 Mes picks{f' ({n_pending})' if _auto_confirm and n_pending > 0 else (f' ({n_proposed})' if n_proposed > 0 else '')}",
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
    if health.get("auto_confirm_picks"):
        st.caption(
            "**Validation automatique.** Chaque pronostic scanné est compté "
            "comme un pari réellement placé et entre directement dans le track "
            "record — pas de file d'attente à traiter."
        )
    else:
        st.caption(
            _intro +
            "à toi de confirmer ceux que tu as réellement placés chez ton bookmaker, "
            "ou de skipper. Le solde est débité uniquement à la confirmation."
        )
    tab_validate, tab_pending = st.tabs([
        ("🔔 Mes picks" if _auto_confirm
         else f"🔔 Picks à valider{f' ({n_proposed})' if n_proposed > 0 else ''}"),
        f"⏳ En attente de résultat{f' ({n_pending})' if n_pending > 0 else ''}",
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
    render_capital_tab(health)

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
    if _auto_confirm:
        st.caption(
            "**Le worker scanne tout seul** à 10:00, 16:00, 19:00 et 21:30 "
            "(Europe/Paris) : tes pronostics arrivent sans rien lancer. "
            "🎯 **Scan manuel** est un scan EN PLUS, à la demande — il appelle "
            "l'Odds API, applique le modèle et **enregistre** les picks, qui "
            "comptent immédiatement dans ton track record. Ne le relance pas "
            "pour essayer des réglages : chaque passage ajoute des paris "
            "comptés. Les autres onglets sont du diagnostic et de "
            "l'exploration."
        )
    else:
        st.caption(
            "**Sandbox.** Scans à la demande, diagnostic infra et historique des "
            "invocations IA. Utilise ces outils pour explorer, pas pour ton "
            "workflow quotidien."
        )
    (tab_blind, tab_scan, tab_safe, tab_over, tab_local, tab_agent, tab_parlay,
     tab_lottery, tab_live, tab_events, tab_sources, tab_agent_runs) = st.tabs([
        "🔮 Pronostics modèle",
        "🎯 Scan manuel",
        "🟢 Sûr & rapide",
        "⚽ Over (buts)",
        "🧠 Agent local",
        "🤖 Agent IA (Claude)",
        "🎯 Combinés favoris",
        "🎰 Loterie",
        "🔴 Live",
        "📅 Matchs disponibles",
        "🔌 Sources",
        "📜 Historique IA",
    ])
    with tab_blind:
        render_blind_tab(filters)
    with tab_scan:
        render_scan_tab(filters, health)
    with tab_safe:
        render_safe_fast_tab(filters, health)
    with tab_over:
        render_over_tab(filters, health)
    with tab_local:
        render_local_agent_tab(filters, health)
    with tab_agent:
        render_ai_agent_tab(filters, agent_enabled, health)
    with tab_parlay:
        render_target_parlay_tab(filters)
    with tab_lottery:
        render_lottery_parlay_tab(filters)
    with tab_live:
        render_live_tab(filters, health)
    with tab_events:
        render_events_tab(filters)
    with tab_sources:
        render_sources_tab()
    with tab_agent_runs:
        render_history_tab()
