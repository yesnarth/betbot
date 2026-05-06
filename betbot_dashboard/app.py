"""
BetBot Streamlit dashboard.

Run locally:
    streamlit run betbot_dashboard/app.py

Talks to the FastAPI backend (BETBOT_API_URL, default http://localhost:8000).
"""
from __future__ import annotations

import os
from typing import Any

import httpx
import pandas as pd
import streamlit as st

API_URL = os.getenv("BETBOT_API_URL", "http://localhost:8000").rstrip("/")
BASIC_USER = os.getenv("API_BASIC_USER", "betbot")
BASIC_PASSWORD = os.getenv("API_BASIC_PASSWORD", "")

AUTH = (BASIC_USER, BASIC_PASSWORD) if BASIC_PASSWORD else None


def api_get(path: str, **params: Any) -> Any:
    r = httpx.get(f"{API_URL}{path}", params=params, auth=AUTH, timeout=30)
    r.raise_for_status()
    return r.json()


def api_post(path: str, json: dict | None = None, **params: Any) -> Any:
    # Long timeout — the agent endpoint can take 30-60s
    r = httpx.post(f"{API_URL}{path}", params=params, json=json, auth=AUTH, timeout=180)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="BetBot Dashboard",
    page_icon="⚽",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.title("⚽ BetBot Dashboard")
st.caption(f"Backend : {API_URL}")


# ---------------------------------------------------------------------------
# Sidebar — health + filters
# ---------------------------------------------------------------------------

with st.sidebar:
    st.header("État système")
    try:
        h = api_get("/health")
        st.metric("Équipes en DB", h["teams_in_db"])
        st.metric("Capital", f"{h['bankroll']:.0f} $")
        st.write("Scans/jour :", " · ".join(h["scan_hours"]))
        if h["agent_enabled"]:
            st.success("✅ Agent IA actif")
        else:
            st.warning("⚠️ Agent IA désactivé (ANTHROPIC_API_KEY manquant)")
    except Exception as exc:
        st.error(f"API injoignable : {exc}")
        st.stop()

    st.divider()
    st.header("Filtres agent IA")
    sport = st.selectbox(
        "Ligue",
        options=["Toutes", "soccer_epl", "soccer_spain_la_liga",
                 "soccer_germany_bundesliga", "soccer_italy_serie_a",
                 "soccer_france_ligue1", "soccer_uefa_champs_league"],
        index=0,
    )
    today_only = st.checkbox("Seulement les matchs d'aujourd'hui", value=True)
    min_edge = st.slider("Edge minimum (%)", min_value=-10.0, max_value=20.0, value=4.0, step=0.5)
    min_prob = st.slider("Probabilité modèle minimale", min_value=0.10, max_value=0.90, value=0.40, step=0.05)
    min_odds = st.slider("Cote minimum", min_value=1.0, max_value=5.0, value=1.5, step=0.1)
    n_legs = st.slider("Nombre de jambes par combiné", min_value=1, max_value=6, value=3)
    n_combos = st.slider("Nombre de combinés à générer", min_value=1, max_value=10, value=3)
    extra = st.text_area("Instructions additionnelles", placeholder="ex : éviter les nuls, prioriser les favoris à domicile…")

    st.divider()
    if st.button("🔄 Résoudre les paris terminés", use_container_width=True):
        try:
            res = api_post("/predictions/resolve")
            st.success(f"Résolus : {res.get('resolved')} · En attente : {res.get('still_pending')}")
        except Exception as exc:
            st.error(f"Erreur : {exc}")


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------

tab_agent, tab_events, tab_pending, tab_roi = st.tabs([
    "🤖 Agent IA", "📅 Matchs du jour", "⏳ Paris en attente", "📊 ROI"
])

# ---------------------------------------------------------------------------
# Tab — Agent
# ---------------------------------------------------------------------------

with tab_agent:
    st.subheader("Recommandations de l'agent IA")
    st.caption("L'agent utilise le modèle Dixon-Coles + cotes multi-bookmakers, "
               "raisonne en plusieurs étapes via le serveur MCP, et te propose des combinés.")

    if st.button("🚀 Demander une recommandation", type="primary", use_container_width=True):
        if not h["agent_enabled"]:
            st.error("Agent IA désactivé — configure ANTHROPIC_API_KEY dans .env.")
        else:
            payload = {
                "sport_key": None if sport == "Toutes" else sport,
                "today_only": today_only,
                "min_edge": round(min_edge / 100, 4),
                "min_prob": min_prob,
                "min_odds": min_odds,
                "n_legs": n_legs,
                "n_combos": n_combos,
                "extra_instructions": extra or None,
            }
            with st.spinner("L'agent raisonne… (~30-60 s)"):
                try:
                    res = api_post("/agent/recommend", json=payload)
                except Exception as exc:
                    st.error(f"Erreur : {exc}")
                    res = None

            if res:
                if res.get("error"):
                    st.error(f"Agent en échec : {res['error']}")
                cols = st.columns(4)
                cols[0].metric("Tool calls", res.get("n_tool_calls", 0))
                cols[1].metric("Durée", f"{res.get('duration_ms', 0)/1000:.1f} s")
                cols[2].metric("Coût", f"${res.get('cost_usd') or 0:.4f}")
                cols[3].metric("Modèle", res.get("model", "?").replace("claude-", ""))

                st.markdown("### Raisonnement")
                st.info(res.get("rationale", "—"))

                if picks := res.get("picks"):
                    st.markdown("### Paris individuels")
                    df = pd.DataFrame(picks)
                    if not df.empty:
                        # Pretty columns when present
                        cols_to_show = [c for c in [
                            "home_team", "away_team", "league", "selection_label",
                            "best_odds", "model_prob", "value_edge", "kelly_stake",
                            "best_book", "model_type",
                        ] if c in df.columns]
                        st.dataframe(df[cols_to_show], use_container_width=True, hide_index=True)
                else:
                    st.warning("Aucun pari individuel.")

                if parlays := res.get("parlays"):
                    st.markdown("### Combinés")
                    for i, parlay in enumerate(parlays, 1):
                        with st.expander(f"Combiné #{i} — cote × {parlay.get('combined_odds')} · EV {parlay.get('combined_ev_pct')}%"):
                            df = pd.DataFrame(parlay.get("legs", []))
                            if not df.empty:
                                show = [c for c in [
                                    "home_team", "away_team", "selection_label",
                                    "best_odds", "model_prob", "value_edge",
                                ] if c in df.columns]
                                st.dataframe(df[show], use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# Tab — Events
# ---------------------------------------------------------------------------

with tab_events:
    st.subheader("Matchs disponibles")
    if st.button("🔍 Rafraîchir les matchs", use_container_width=True):
        try:
            ev = api_get("/events", today_only=today_only,
                         **({"sport_key": sport} if sport != "Toutes" else {}))
            st.metric("Total", ev["total"])
            for sk, items in ev["by_sport"].items():
                with st.expander(f"{sk} ({len(items)})", expanded=True):
                    df = pd.DataFrame(items)
                    if not df.empty:
                        st.dataframe(df, use_container_width=True, hide_index=True)
        except Exception as exc:
            st.error(f"Erreur : {exc}")


# ---------------------------------------------------------------------------
# Tab — Pending predictions
# ---------------------------------------------------------------------------

with tab_pending:
    st.subheader("Paris en attente de résultat")
    try:
        rows = api_get("/predictions/pending")
        if not rows:
            st.info("Aucun pari en attente.")
        else:
            df = pd.DataFrame(rows)
            cols_to_show = [c for c in [
                "created_at", "home_team", "away_team", "selection",
                "best_odds", "model_prob", "value_edge", "kelly_stake",
            ] if c in df.columns]
            st.dataframe(df[cols_to_show], use_container_width=True, hide_index=True)
    except Exception as exc:
        st.error(f"Erreur : {exc}")


# ---------------------------------------------------------------------------
# Tab — ROI
# ---------------------------------------------------------------------------

with tab_roi:
    st.subheader("Performance globale")
    period = st.selectbox("Période", options=[7, 14, 30, 60, 90, 180, 365], index=2)
    try:
        s = api_get("/stats/roi", days=period)
        cols = st.columns(4)
        cols[0].metric("Paris", s["n_bets"])
        cols[1].metric("Victoires", s["n_wins"], f"{s['hit_rate']}%")
        cols[2].metric("ROI", f"{s['roi']:.1f}%")
        cols[3].metric("Edge moyen", f"{s['avg_edge']:.1f}%")

        st.markdown("### Closing Line Value (CLV)")
        st.caption(
            "Métrique #1 des bettors pros : si tu paries à de meilleures cotes "
            "que la fermeture du marché, tu bats le marché — peu importe les "
            "résultats à court terme."
        )
        clv_cols = st.columns(3)
        clv_cols[0].metric("Paris avec CLV", s.get("n_with_clv", 0))
        clv_cols[1].metric("CLV moyen", f"{s.get('avg_clv_pct', 0):+.2f}%")
        clv_cols[2].metric("% paris CLV > 0", f"{s.get('positive_clv_share', 0):.1f}%")

        if s["n_bets"] == 0:
            st.info(
                "Aucune statistique pour le moment. Les ROI apparaissent après "
                "la résolution des premiers paris (utilise le bouton de la sidebar)."
            )
    except Exception as exc:
        st.error(f"Erreur : {exc}")
