"""
BetBot Streamlit dashboard.

Run locally:
    streamlit run betbot_dashboard/app.py

Talks to the FastAPI backend (BETBOT_API_URL, default http://localhost:8000).
Two modes:
  - Manual scan (zero AI, free, deterministic) — uses the blended Poisson model
  - AI agent (requires ANTHROPIC_API_KEY) — same data + Claude reasoning
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
    r = httpx.post(f"{API_URL}{path}", params=params, json=json, auth=AUTH, timeout=180)
    r.raise_for_status()
    return r.json()


def render_picks_table(picks: list[dict]) -> None:
    if not picks:
        return
    df = pd.DataFrame(picks)
    cols = [c for c in [
        "home_team", "away_team", "league", "selection_label",
        "best_odds", "model_prob", "value_edge", "kelly_stake",
        "best_book", "model_type",
    ] if c in df.columns]
    st.dataframe(df[cols], width='stretch', hide_index=True)

    # Caveat on suspiciously large edges
    if "value_edge" in df.columns:
        big_edges = (df["value_edge"] > 0.20).sum()
        if big_edges > 0:
            st.warning(
                f"⚠️ **{big_edges} pari(s) ont un edge > 20%.** Le marché des cotes est "
                "généralement bien calibré ; un edge aussi élevé révèle souvent un "
                "**défaut de modèle** (le modèle ne voit pas les blessures, suspensions, "
                "rotations, motivation). Considère ces paris comme suspects et privilégie "
                "ceux à edge **2-10%**, où la valeur est plus fiable."
            )


def render_parlays(parlays: list[dict]) -> None:
    if not parlays:
        return
    for i, parlay in enumerate(parlays, 1):
        ev = parlay.get("combined_ev_pct", 0)
        odds = parlay.get("combined_odds", 0)
        with st.expander(f"Combiné #{i}  —  cote × {odds}  —  EV {ev:+.1f}%", expanded=(i == 1)):
            df = pd.DataFrame(parlay.get("legs", []))
            if not df.empty:
                show = [c for c in [
                    "home_team", "away_team", "selection_label",
                    "best_odds", "model_prob", "value_edge",
                ] if c in df.columns]
                st.dataframe(df[show], width='stretch', hide_index=True)


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

# Health is fetched once — needed by the rest of the page
try:
    health = api_get("/health")
except Exception as exc:
    st.error(f"API injoignable sur {API_URL} : {exc}")
    st.info("Vérifie que les containers Docker tournent : `docker compose ps`")
    st.stop()

agent_enabled = bool(health.get("agent_enabled"))


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

with st.sidebar:
    st.header("État système")
    st.metric("Équipes en DB", health["teams_in_db"])
    # Real bankroll balance (NOT the static BANKROLL env var anymore)
    balance = float(health.get("balance", 0))
    available = float(health.get("available", 0))
    initial = float(health.get("bankroll_initial", 0))
    delta_str = f"{balance - initial:+.0f} $" if initial > 0 else None
    st.metric("Solde courant", f"{balance:.0f} $", delta=delta_str)
    if balance != available:
        st.caption(f"Disponible : **{available:.0f} $** · "
                   f"Engagé : {balance - available:.0f} $")
    st.write("Scans automatiques :", " · ".join(health["scan_hours"]))

    if agent_enabled:
        st.success("Agent IA actif (Claude)")
    else:
        st.info("Agent IA non configuré — utilise le **Scan manuel** ci-contre.")

    st.divider()
    st.header("Filtres de scan")
    st.caption("S'appliquent au Scan manuel ET à l'agent IA.")

    sport = st.selectbox(
        "Ligue",
        options=["Toutes", "soccer_epl", "soccer_spain_la_liga",
                 "soccer_germany_bundesliga", "soccer_italy_serie_a",
                 "soccer_france_ligue1", "soccer_uefa_champs_league"],
        index=0,
    )
    today_only = st.checkbox("Seulement les matchs d'aujourd'hui", value=True)
    min_edge_pct = st.slider("Edge minimum (%)", -10.0, 20.0, 4.0, 0.5)
    min_prob = st.slider("Probabilité modèle minimale", 0.10, 0.90, 0.40, 0.05)
    min_odds = st.slider("Cote minimum", 1.0, 5.0, 1.5, 0.1)
    n_legs = st.slider("Nombre de jambes par combiné", 1, 6, 3)
    n_combos = st.slider("Nombre de combinés à générer", 1, 10, 3)

    st.divider()
    if st.button("🔄 Résoudre les paris terminés", width='stretch'):
        try:
            res = api_post("/predictions/resolve")
            st.success(f"Résolus : {res.get('resolved')} · "
                       f"En attente : {res.get('still_pending')}")
        except Exception as exc:
            st.error(f"Erreur : {exc}")


# ---------------------------------------------------------------------------
# Tabs — order changed: matches first, manual scan, then AI agent
# ---------------------------------------------------------------------------

(
    tab_scan, tab_local, tab_events, tab_pending,
    tab_roi, tab_capital, tab_agent, tab_history, tab_sources,
) = st.tabs([
    "🎯 Scan manuel",
    "🧠 Agent local",
    "📅 Matchs disponibles",
    "⏳ Paris en attente",
    "📊 Performance",
    "💰 Capital",
    "🤖 Agent IA (Claude)",
    "📜 Historique agent",
    "🔌 Sources",
])


# ---------------------------------------------------------------------------
# Tab 1 — Manual scan (no AI)
# ---------------------------------------------------------------------------

with tab_scan:
    st.subheader("Scan manuel — modèle Dixon-Coles + xG + ELO")
    st.caption(
        "Aucune IA. Reproductible. Gratuit (au-delà du quota Odds API). "
        "C'est exactement le calcul que fait le worker au quotidien à 09h, 15h, 20h."
    )

    if st.button("▶️ Lancer le scan", type="primary", width='stretch'):
        payload = {
            "sport_key": None if sport == "Toutes" else sport,
            "today_only": today_only,
            "min_edge": round(min_edge_pct / 100, 4),
            "min_prob": min_prob,
            "min_odds": min_odds,
            "n_legs": n_legs,
            "n_combos": n_combos,
        }
        with st.spinner("Récupération des cotes + calcul Poisson…"):
            try:
                res = api_post("/recommend/manual", json=payload)
            except Exception as exc:
                st.error(f"Erreur : {exc}")
                res = None

        if res:
            cols = st.columns(3)
            cols[0].metric("Matchs scannés", res["n_events_scanned"])
            cols[1].metric("Paris détectés", res["n_picks"])
            cols[2].metric("Combinés générés", res["n_parlays"])

            if res["n_events_scanned"] == 0:
                st.warning(
                    "Aucun match correspondant aux filtres. "
                    "Décoche *Aujourd'hui seulement* ou élargis la ligue."
                )
            elif res["n_picks"] == 0:
                st.info(
                    "Aucun pari ne passe les filtres. Le modèle n'a rien trouvé "
                    "qui dépasse l'edge minimum. Tu peux abaisser **Edge minimum** "
                    "dans la sidebar et relancer."
                )
            else:
                st.markdown("### Paris individuels")
                render_picks_table(res["picks"])
                if res["parlays"]:
                    st.markdown("### Combinés")
                    render_parlays(res["parlays"])


# ---------------------------------------------------------------------------
# Tab 2 — Local deterministic agent (no AI cost, uses Tavily + ELO + weather)
# ---------------------------------------------------------------------------

with tab_local:
    st.subheader("Agent local — règles métier explicites")
    st.caption(
        "Prend les picks du scan, croise avec les news Tavily + blessures + météo + ELO, "
        "applique des règles explicites pour calibrer les edges fictifs. "
        "**Zéro coût** au-delà des quotas Tavily/Odds API. Reproductible et auditable."
    )

    col1, col2 = st.columns(2)
    with col1:
        agent_use_news = st.checkbox(
            "Consulter Tavily (news live)", value=True,
            help="Demande aux nouvelles du jour si une équipe a une blessure/suspension/coach viré.",
        )
    with col2:
        agent_use_weather = st.checkbox(
            "Consulter Open-Meteo (météo stade)", value=True,
            help="Pluie forte ou vent fort réduisent les paris Over.",
        )

    min_final = st.slider(
        "Edge minimum APRÈS calibration (%)",
        -5.0, 15.0, 2.0, 0.5,
        help="Seuil de rejet final. Les picks dont l'edge tombe sous ce seuil après calibration sont écartés.",
    )

    if st.button("🧠 Lancer l'agent local", type="primary", width='stretch'):
        payload = {
            "sport_key": None if sport == "Toutes" else sport,
            "today_only": today_only,
            "min_edge": round(min_edge_pct / 100, 4),
            "min_prob": min_prob,
            "min_odds": min_odds,
            "n_legs": n_legs,
            "n_combos": n_combos,
            "fetch_news": agent_use_news,
            "fetch_weather": agent_use_weather,
            "min_final_edge": round(min_final / 100, 4),
        }
        with st.spinner("Scan + appels Tavily/météo/ELO + règles…"):
            try:
                res = api_post("/recommend/agent-local", json=payload)
            except Exception as exc:
                st.error(f"Erreur : {exc}")
                res = None

        if res:
            cols = st.columns(4)
            cols[0].metric("Picks bruts", res["n_picks_in"])
            cols[1].metric("Acceptés", res["n_accepted"])
            cols[2].metric("Rejetés", res["n_rejected"])
            cols[3].metric("Combinés", res["n_parlays"])

            sub = st.columns(3)
            sub[0].metric("Appels Tavily", res["n_news_calls"])
            sub[1].metric("Appels météo", res["n_weather_calls"])
            sub[2].metric("Tavily actif", "✓" if res["tavily_available"] else "non configuré")

            if not res["tavily_available"]:
                st.info(
                    "Tavily désactivé : pas de prise en compte des news live. "
                    "Active-le en mettant `TAVILY_API_KEY` dans `.env`."
                )

            if res["picks"]:
                st.markdown("### ✅ Paris validés")
                for i, p in enumerate(res["picks"], 1):
                    label = (
                        f"#{i} · {p['home_team']} vs {p['away_team']} — "
                        f"{p['selection_label']} @ {p['best_odds']:.2f} · "
                        f"prob {p['model_prob']*100:.1f}% · edge {p['value_edge']*100:+.1f}%"
                    )
                    if p["status"] == "flagged":
                        label = f"🟡 {label}"
                    with st.expander(label):
                        st.caption(f"Statut : **{p['status']}** · Mise Kelly : {p.get('kelly_stake', 0)}$")
                        for r in p.get("rationale", []):
                            st.write(f"- {r}")
            else:
                st.warning("Aucun pari n'a survécu aux règles de l'agent local.")

            if res["rejected"]:
                with st.expander(f"❌ {res['n_rejected']} paris rejetés (cliquer pour voir)"):
                    for p in res["rejected"]:
                        st.markdown(
                            f"**{p['home_team']} vs {p['away_team']}** — "
                            f"{p['selection_label']} · edge final {p['value_edge']*100:+.1f}%"
                        )
                        for r in p.get("rationale", []):
                            st.write(f"- {r}")
                        st.divider()

            if res["parlays"]:
                st.markdown("### Combinés (paris validés uniquement)")
                render_parlays(res["parlays"])


# ---------------------------------------------------------------------------
# Tab 3 — Available events
# ---------------------------------------------------------------------------

with tab_events:
    st.subheader("Matchs disponibles")
    st.caption("Liste brute des matchs visibles côté Odds API, sans filtre de modèle.")
    if st.button("🔍 Charger les matchs", width='stretch'):
        try:
            params = {"today_only": today_only}
            if sport != "Toutes":
                params["sport_key"] = sport
            ev = api_get("/events", **params)
            st.metric("Total", ev["total"])
            for sk, items in ev["by_sport"].items():
                with st.expander(f"{sk} ({len(items)})", expanded=True):
                    df = pd.DataFrame(items)
                    if not df.empty:
                        st.dataframe(df, width='stretch', hide_index=True)
        except Exception as exc:
            st.error(f"Erreur : {exc}")


# ---------------------------------------------------------------------------
# Tab 3 — Pending predictions
# ---------------------------------------------------------------------------

with tab_pending:
    st.subheader("Paris en attente de résultat")
    try:
        rows = api_get("/predictions/pending")
        if not rows:
            st.info(
                "Aucun pari en attente. Les paris arrivent ici quand : (1) un scan "
                "(manuel ou auto via le worker) sauvegarde des picks, ou (2) tu "
                "valides l'output de l'agent IA."
            )
        else:
            df = pd.DataFrame(rows)
            cols = [c for c in [
                "created_at", "home_team", "away_team", "selection",
                "best_odds", "closing_odds", "model_prob", "value_edge", "kelly_stake",
            ] if c in df.columns]
            st.dataframe(df[cols], width='stretch', hide_index=True)
    except Exception as exc:
        st.error(f"Erreur : {exc}")


# ---------------------------------------------------------------------------
# Tab 4 — Performance (real metrics only, hidden when no data)
# ---------------------------------------------------------------------------

with tab_roi:
    st.subheader("Performance globale")
    period = st.selectbox("Période (jours)", [7, 14, 30, 60, 90, 180, 365], index=2)
    try:
        s = api_get("/stats/roi", days=period)
    except Exception as exc:
        st.error(f"Erreur : {exc}")
        s = None

    if s is None:
        pass
    elif s["n_bets"] == 0:
        st.info(
            f"Aucun pari résolu sur les {period} derniers jours. "
            "Les métriques de ROI / hit rate / CLV s'afficheront automatiquement "
            "dès que les premiers paris auront été résolus (le worker le fait à 04h "
            "chaque jour, ou clique **Résoudre les paris terminés** dans la sidebar)."
        )
    else:
        cols = st.columns(4)
        cols[0].metric("Paris résolus", s["n_bets"])
        cols[1].metric("Victoires", f"{s['n_wins']} ({s['hit_rate']}%)")
        cols[2].metric("ROI", f"{s['roi']:+.1f}%")
        cols[3].metric("Edge moyen", f"{s['avg_edge']:+.2f}%")

        if s.get("n_with_clv", 0) > 0:
            st.divider()
            st.markdown("### Closing Line Value (CLV)")
            st.caption(
                "Métrique de skill #1 des bettors pros : un CLV moyen positif "
                "signifie que tu paries à de meilleures cotes que la fermeture du marché."
            )
            clv_cols = st.columns(3)
            clv_cols[0].metric("Paris avec CLV", s["n_with_clv"])
            clv_cols[1].metric("CLV moyen", f"{s['avg_clv_pct']:+.2f}%")
            clv_cols[2].metric("% paris CLV > 0", f"{s['positive_clv_share']:.1f}%")
        else:
            st.caption(
                "Le CLV s'activera dès que le worker aura snappé les closing odds "
                "(automatique toutes les 10 min pour les matchs qui démarrent dans "
                "moins de 30 min)."
            )


# ---------------------------------------------------------------------------
# Tab — Capital (real bankroll tracking)
# ---------------------------------------------------------------------------

with tab_capital:
    st.subheader("💰 Gestion du capital")
    st.caption(
        "Toutes les mises consomment réellement le solde, tous les gains/pertes "
        "le mettent à jour automatiquement. Source unique de vérité : la table "
        "`bankroll_ledger` en DB."
    )

    try:
        bk_state = api_get("/bankroll/state")
    except Exception as exc:
        st.error(f"Erreur : {exc}")
        bk_state = None

    if bk_state:
        c = st.columns(4)
        c[0].metric("Solde courant", f"{bk_state['balance']:.2f} $")
        c[1].metric("Capital libre", f"{bk_state['available']:.2f} $")
        c[2].metric("Engagé sur paris", f"{bk_state['committed']:.2f} $")
        pnl = bk_state['pnl']
        c[3].metric("P&L cumulé", f"{pnl:+.2f} $",
                    delta=f"{pnl:+.2f} $" if pnl != 0 else None)

        st.divider()
        c2 = st.columns(4)
        c2[0].metric("Dépôts cumulés", f"{bk_state['total_deposits']:.2f} $")
        c2[1].metric("Retraits cumulés", f"{bk_state['total_withdrawals']:.2f} $")
        c2[2].metric("Gains cumulés", f"{bk_state['total_won']:.2f} $")
        c2[3].metric("Mises perdues", f"{bk_state['total_lost_stakes']:.2f} $")

        # Evolution chart
        try:
            evo = api_get("/bankroll/evolution", days=60)
            if evo:
                df = pd.DataFrame(evo)
                df["ts"] = pd.to_datetime(df["ts"])
                df = df.set_index("ts")
                st.markdown("### Évolution du solde (60 derniers jours)")
                st.line_chart(df["balance"], height=260)
            else:
                st.info("Pas encore de mouvements à afficher dans la courbe.")
        except Exception:
            pass

        # Deposit / withdraw
        st.divider()
        st.markdown("### Mouvements manuels")
        c3 = st.columns([1, 1, 2])
        with c3[0]:
            dep_amt = st.number_input("Montant dépôt", min_value=1.0, value=50.0, step=10.0)
            dep_note = st.text_input("Note dépôt", placeholder="ex : recharge mensuelle")
            if st.button("➕ Déposer", width='stretch'):
                try:
                    api_post("/bankroll/deposit",
                             json={"amount": dep_amt, "note": dep_note or None})
                    st.success(f"+{dep_amt:.2f}$ déposés. Recharge la page.")
                except Exception as exc:
                    st.error(f"Erreur : {exc}")
        with c3[1]:
            wd_amt = st.number_input("Montant retrait", min_value=1.0, value=20.0, step=10.0)
            wd_note = st.text_input("Note retrait", placeholder="ex : retrait gains")
            if st.button("➖ Retirer", width='stretch'):
                try:
                    api_post("/bankroll/withdraw",
                             json={"amount": wd_amt, "note": wd_note or None})
                    st.success(f"-{wd_amt:.2f}$ retirés. Recharge la page.")
                except Exception as exc:
                    st.error(f"Erreur : {exc}")

        # Recent ledger
        st.markdown("### Journal récent")
        try:
            history = api_get("/bankroll/history", limit=50)
            if history:
                hdf = pd.DataFrame(history)
                hdf["ts"] = pd.to_datetime(hdf["ts"]).dt.strftime("%Y-%m-%d %H:%M")
                show = ["ts", "kind", "amount", "balance_after", "note"]
                show = [c for c in show if c in hdf.columns]
                st.dataframe(hdf[show], width='stretch', hide_index=True)
        except Exception:
            st.caption("(Pas encore d'entrées dans le journal.)")


# ---------------------------------------------------------------------------
# Tab 5 — AI agent (only enabled when key is set)
# ---------------------------------------------------------------------------

with tab_agent:
    st.subheader("Agent IA — Claude Sonnet 4.6")
    st.caption(
        "L'agent appelle les MÊMES outils que le scan manuel + en plus : recherche "
        "de news live (Tavily), blessures (API-Football), météo (Open-Meteo). "
        "Il raisonne en plusieurs étapes et justifie chaque pick."
    )

    if not agent_enabled:
        st.warning(
            "**Agent désactivé.** Pour l'activer :\n\n"
            "1. Crée une clé sur https://console.anthropic.com (5$ de crédit gratuits)\n"
            "2. Ajoute `ANTHROPIC_API_KEY=sk-ant-...` dans `.env`\n"
            "3. Redémarre l'API : `docker compose restart api`\n\n"
            "En attendant, utilise l'onglet **🎯 Scan manuel** — il fait le même "
            "calcul de probabilités, sans le coût et sans le raisonnement narratif."
        )
    else:
        extra = st.text_area(
            "Instructions additionnelles",
            placeholder="ex : éviter les nuls, prioriser les favoris à domicile, "
                        "ne recommander que des combinés à 2 jambes max…",
        )
        if st.button("🚀 Demander une recommandation à l'agent", type="primary", width='stretch'):
            payload = {
                "sport_key": None if sport == "Toutes" else sport,
                "today_only": today_only,
                "min_edge": round(min_edge_pct / 100, 4),
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
                m = st.columns(4)
                m[0].metric("Tool calls", res.get("n_tool_calls", 0))
                m[1].metric("Durée", f"{res.get('duration_ms', 0)/1000:.1f} s")
                m[2].metric("Coût", f"${res.get('cost_usd') or 0:.4f}")
                m[3].metric("Modèle", res.get("model", "?").replace("claude-", ""))
                st.markdown("### Raisonnement")
                st.info(res.get("rationale") or "(vide)")
                if res.get("picks"):
                    st.markdown("### Paris individuels")
                    render_picks_table(res["picks"])
                if res.get("parlays"):
                    st.markdown("### Combinés")
                    render_parlays(res["parlays"])


# ---------------------------------------------------------------------------
# Tab — Agent runs history
# ---------------------------------------------------------------------------

with tab_history:
    st.subheader("📜 Historique des invocations de l'agent IA")
    st.caption(
        "Chaque appel à l'agent (Claude ou local) crée une entrée auditée : "
        "filtres utilisés, raisonnement complet, picks, durée, coût."
    )
    col_a, col_b = st.columns([1, 3])
    with col_a:
        trigger_filter = st.selectbox(
            "Source",
            options=[None, "api", "scheduled", "dashboard"],
            format_func=lambda x: "Toutes" if x is None else x,
        )
        history_limit = st.slider("Limite", 10, 200, 50)

    try:
        params = {"limit": history_limit}
        if trigger_filter:
            params["trigger"] = trigger_filter
        runs = api_get("/agent/runs", **params)
    except Exception as exc:
        st.error(f"Erreur : {exc}")
        runs = []

    if not runs:
        st.info(
            "Aucune exécution d'agent enregistrée. L'agent IA Claude crée "
            "des entrées dès que tu cliques sur 'Demander une recommandation'. "
            "L'agent local n'enregistre pas dans cette table (à ajouter)."
        )
    else:
        df = pd.DataFrame(runs)
        if "created_at" in df.columns:
            df["created_at"] = pd.to_datetime(df["created_at"]).dt.strftime("%Y-%m-%d %H:%M")
        compact_cols = [c for c in [
            "id", "created_at", "trigger", "model", "status",
            "n_tool_calls", "duration_ms", "cost_usd",
        ] if c in df.columns]
        st.dataframe(df[compact_cols], width='stretch', hide_index=True)

        # Drill-down
        st.markdown("### Détail d'une exécution")
        if "id" in df.columns:
            run_id = st.selectbox("Sélectionne un ID",
                                  options=df["id"].tolist())
            if st.button("📖 Charger le raisonnement complet", width='stretch'):
                try:
                    detail = api_get(f"/agent/runs/{run_id}")
                except Exception as exc:
                    st.error(f"Erreur : {exc}")
                    detail = None
                if detail:
                    cols = st.columns(4)
                    cols[0].metric("Tool calls", detail.get("n_tool_calls", 0))
                    cols[1].metric("Durée", f"{(detail.get('duration_ms') or 0)/1000:.1f}s")
                    cols[2].metric("Coût USD", f"${detail.get('cost_usd') or 0:.4f}")
                    cols[3].metric("Statut", detail.get("status", "?"))
                    st.markdown("**Filtres utilisés :**")
                    st.json(detail.get("filters") or {})
                    st.markdown("**Raisonnement :**")
                    reasoning = detail.get("reasoning") or "(vide)"
                    st.text_area("trace", value=reasoning, height=300, disabled=True,
                                 label_visibility="collapsed")
                    if detail.get("picks"):
                        st.markdown("**Picks recommandés :**")
                        st.dataframe(pd.DataFrame(detail["picks"]),
                                     width='stretch', hide_index=True)
                    if detail.get("error"):
                        st.error(detail["error"])


# ---------------------------------------------------------------------------
# Tab — Data sources health
# ---------------------------------------------------------------------------

with tab_sources:
    st.subheader("🔌 État des sources externes")
    st.caption(
        "Probe en temps réel chaque source. Une source qui passe à `KO` "
        "suggère un changement upstream (API down, scraper cassé, clé révoquée)."
    )
    if st.button("🔄 Tester maintenant", type="primary"):
        try:
            health = api_get("/health/sources")
            for src in health.get("sources", []):
                cols = st.columns([2, 1, 1])
                status_icon = "✅" if src["ok"] else "🔴"
                cols[0].markdown(f"{status_icon} **{src['name']}**")
                cols[1].caption(f"{src.get('latency_ms', 0)} ms")
                if not src["ok"] and src.get("reason"):
                    cols[2].caption(f"_{src['reason'][:80]}_")
        except Exception as exc:
            st.error(f"Erreur : {exc}")
    else:
        st.info("Clique sur 'Tester maintenant' pour lancer une vérification live.")
