"""Sidebar — KPIs, quick actions, and scan filters.

`render_sidebar(health, agent_enabled, api_post, n_proposed)` returns the
filter dict that the **Outils** sandbox tabs consume. The first KPI surfaces
the actionable count of picks waiting on the user — clicking the validation
queue tab is the natural next step.
"""
from __future__ import annotations

import streamlit as st


def render_sidebar(
    health: dict,
    agent_enabled: bool,
    api_post,
    n_proposed: int = 0,
) -> dict:
    """Render the left-hand sidebar and return the scan filter selections."""
    with st.sidebar:
        # ─── Section 1 : Action — what to do NOW ─────────────────────────
        st.header("Action")
        if n_proposed > 0:
            st.metric(
                "Picks à valider",
                str(n_proposed),
                delta="action requise",
                delta_color="inverse",
            )
            st.caption("Onglet **🔔 Mes picks → Picks à valider**.")
        elif n_proposed == 0:
            st.metric("Picks à valider", "0", delta="rien à faire")
        else:
            # n_proposed == -1 → API probe failed
            st.metric("Picks à valider", "—", delta="API ?",
                      delta_color="off")

        st.divider()

        # ─── Section 2 : Bankroll snapshot ───────────────────────────────
        st.header("Bankroll")
        balance = float(health.get("balance", 0))
        available = float(health.get("available", 0))
        initial = float(health.get("bankroll_initial", 0))
        delta_str = f"{balance - initial:+.0f} $" if initial > 0 else None
        st.metric("Solde courant", f"{balance:.0f} $", delta=delta_str)
        if balance != available:
            st.caption(f"Disponible : **{available:.0f} $** · "
                       f"Engagé : {balance - available:.0f} $")

        quota = int(health.get("odds_quota_remaining", -1))
        quota_min = int(health.get("odds_quota_minimum", 20))
        if quota < 0:
            # Unknown — probe failed or no header observed yet. Surface explicitly
            # rather than rendering a misleading "9999 req · OK".
            st.metric("Quota Odds API", "—", delta="probe inconnu", delta_color="off")
        elif health.get("odds_quota_exhausted"):
            st.metric("Quota Odds API", f"{quota} req",
                      delta=f"⚠ < {quota_min}", delta_color="inverse")
        else:
            delta_q = "OK" if quota >= 100 else f"min {quota_min}"
            st.metric("Quota Odds API", f"{quota} req", delta=delta_q,
                      delta_color="normal" if quota >= 100 else "off")

        st.metric("Équipes en DB", health["teams_in_db"])
        st.caption(f"Scans auto : {' · '.join(health['scan_hours'])}")

        active = health.get("active_sports", []) or []
        if active:
            sport_icons = []
            for k in active:
                if k.startswith("soccer_"):
                    sport_icons.append("⚽")
                elif k.startswith("tennis_"):
                    sport_icons.append("🎾")
                elif k.startswith("basketball_"):
                    sport_icons.append("🏀")
                else:
                    sport_icons.append("•")
            unique = " ".join(sorted(set(sport_icons)))
            st.caption(f"Sports actifs : {unique} ({len(active)} compétitions)")

        if agent_enabled:
            st.success("Agent IA Claude actif", icon="🤖")
        else:
            st.info("Agent IA Claude non configuré.", icon="ℹ️")

        st.divider()

        # ─── Section 2 : Actions rapides ──────────────────────────────────
        st.header("Actions")
        if st.button("🔄 Résoudre les paris terminés", width='stretch'):
            try:
                res = api_post("/predictions/resolve")
                st.success(f"Résolus : {res.get('resolved')} · "
                           f"En attente : {res.get('still_pending')}")
            except Exception as exc:
                st.error(f"Erreur : {exc}")

        st.divider()

        # ─── Section 3 : Filtres (collapsible) ──────────────────────────────
        with st.expander("🎚️ Filtres", expanded=False):
            st.caption(
                "S'appliquent **uniquement** aux outils sandbox de l'onglet "
                "**🛠️ Outils** (Scan manuel, Agent local, Agent IA, "
                "Matchs disponibles). Sans effet sur tes picks proposés par le worker."
            )
            sport = st.selectbox(
                "Ligue / Compétition",
                options=[
                    "Toutes",
                    # Football — D1 grandes ligues européennes + CL
                    "soccer_epl", "soccer_spain_la_liga",
                    "soccer_germany_bundesliga", "soccer_italy_serie_a",
                    "soccer_france_ligue1", "soccer_uefa_champs_league",
                    # Football — couverture étendue
                    "soccer_efl_champ",
                    "soccer_netherlands_eredivisie",
                    "soccer_portugal_primeira_liga",
                    # Tennis (auto-skipped si Grand Slam pas en cours)
                    "tennis_atp_aus_open", "tennis_atp_french_open",
                    "tennis_atp_wimbledon", "tennis_atp_us_open",
                    # Basketball (auto-skipped hors saison)
                    "basketball_nba", "basketball_euroleague",
                ],
                index=0,
                help="Les compétitions hors saison sont automatiquement ignorées au scan.",
            )
            today_only = st.checkbox("Seulement matchs d'aujourd'hui", value=False,
                                     help="Décoché par défaut : permet de scanner les 24-72h à venir.")
            min_edge_pct = st.slider("Edge minimum (%)", -10.0, 20.0, 4.0, 0.5)
            min_prob = st.slider("Probabilité modèle minimale", 0.10, 0.90, 0.40, 0.05)
            min_odds = st.slider("Cote minimum", 1.0, 5.0, 1.5, 0.1)
            n_legs = st.slider("Jambes par combiné", 1, 6, 3)
            n_combos = st.slider("Combinés à générer", 1, 10, 3)

    return {
        "sport": sport,
        "today_only": today_only,
        "min_edge_pct": min_edge_pct,
        "min_prob": min_prob,
        "min_odds": min_odds,
        "n_legs": n_legs,
        "n_combos": n_combos,
    }
