"""Decision tabs — manual scan, local agent, AI agent."""
from __future__ import annotations

import streamlit as st

from betbot_dashboard.api_client import api_get, api_post
from betbot_dashboard.components.picks import render_picks_table, render_parlays
from betbot_dashboard.styles import empty_state


def _payload_from_filters(filters: dict, extras: dict | None = None) -> dict:
    base = {
        "sport_key": None if filters["sport"] == "Toutes" else filters["sport"],
        "today_only": filters["today_only"],
        "min_edge": round(filters["min_edge_pct"] / 100, 4),
        "min_prob": filters["min_prob"],
        "min_odds": filters["min_odds"],
        "n_legs": filters["n_legs"],
        "n_combos": filters["n_combos"],
    }
    if extras:
        base.update(extras)
    return base


def render_scan_tab(filters: dict, health: dict) -> None:
    st.subheader("Scan manuel — modèle Dixon-Coles + xG + ELO")
    scan_hours = health.get("scan_hours") or []
    if scan_hours:
        scan_caption = (
            f"Aperçu **read-only** du modèle Poisson. Le worker auto fait la même chose "
            f"à **{' et '.join(scan_hours)}** (Europe/Paris) et **sauvegarde** les picks "
            f"comme « proposés » dans la file de validation. "
        )
    else:
        scan_caption = (
            "Aperçu **read-only** du modèle Poisson. Auto-scan désactivé "
            "(`SCAN_HOURS=` dans `.env`) → c'est **toi** qui contrôles quand "
            "l'Odds API est appelé. "
        )
    st.caption(
        scan_caption +
        "Ce bouton-ci ne sauvegarde rien par défaut — utilise « 💾 Sauvegarder » "
        "sous le tableau si tu veux pousser les picks vers la file de validation "
        "**Mes picks → Picks à valider**."
    )

    if st.button("▶️ Lancer le scan", type="primary", width='stretch'):
        payload = _payload_from_filters(filters)
        with st.spinner("Récupération des cotes + calcul Poisson…"):
            try:
                res = api_post("/recommend/manual", json=payload)
            except Exception as exc:
                st.error(f"Erreur : {exc}")
                res = None

        if res:
            if res.get("odds_quota_exhausted"):
                empty_state(
                    "🚫",
                    f"Quota Odds API épuisé ({res.get('odds_quota_remaining', '?')} req restantes)",
                    f"Notre garde-fou bloque les scans en dessous de {health.get('odds_quota_minimum', 20)} "
                    "requêtes pour éviter d'épuiser le quota mensuel. Le quota se réinitialise "
                    "le 1er du mois. Pour abaisser le seuil, modifie ODDS_QUOTA_MINIMUM dans .env.",
                )
            else:
                cols = st.columns(3)
                cols[0].metric("Matchs scannés", res["n_events_scanned"])
                cols[1].metric("Paris détectés", res["n_picks"])
                cols[2].metric("Combinés générés", res["n_parlays"])

                if res["n_events_scanned"] == 0:
                    empty_state(
                        "🔎",
                        "Aucun match correspondant aux filtres",
                        "Décoche « Aujourd'hui seulement » ou élargis la ligue dans la sidebar.",
                    )
                elif res["n_picks"] == 0:
                    empty_state(
                        "🎯",
                        "Aucun pari ne passe les filtres",
                        "Le modèle n'a rien trouvé qui dépasse l'edge minimum. "
                        "Abaisse « Edge minimum » dans la sidebar et relance.",
                    )
                else:
                    st.markdown("### Paris individuels")
                    render_picks_table(res["picks"])
                    if res["parlays"]:
                        st.markdown("### Combinés")
                        render_parlays(res["parlays"])

                    # Save-to-validation-queue : same effect as a worker scan
                    # but on demand. Useful when the worker had no eligible
                    # matches at 09h/20h (e.g. mid-afternoon scan after the
                    # MIN_BEFORE_KICKOFF window has filtered everything out).
                    st.markdown("---")
                    st.markdown(
                        "**💾 Pousser ces picks vers la file de validation ?** "
                        "Ils apparaîtront dans **Matchs → 🔔 Picks à valider** "
                        "comme s'ils venaient d'un scan worker. Le bankroll "
                        "n'est pas débité — la confirmation reste à faire "
                        "click-by-click sur chaque pick."
                    )
                    if st.button("💾 Sauvegarder ces picks comme proposés",
                                 width='stretch'):
                        saved = 0
                        already = 0
                        errors = 0
                        for pick in res["picks"]:
                            try:
                                api_post("/admin/save-pick-as-proposed", json=pick)
                                saved += 1
                            except Exception as exc:
                                msg = str(exc).lower()
                                if "already" in msg or "duplicate" in msg or "409" in msg:
                                    already += 1
                                else:
                                    errors += 1
                        if saved:
                            st.success(
                                f"✅ {saved} pick(s) sauvegardés en 'proposed'. "
                                f"Va dans **Matchs → 🔔 Picks à valider** pour les confirmer."
                            )
                        if already:
                            st.info(f"ℹ️ {already} pick(s) déjà en DB (skipped).")
                        if errors:
                            st.error(f"❌ {errors} erreur(s) — voir logs API.")


def render_local_agent_tab(filters: dict, health: dict) -> None:
    import os
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
        payload = _payload_from_filters(filters, {
            "fetch_news": agent_use_news,
            "fetch_weather": agent_use_weather,
            "min_final_edge": round(min_final / 100, 4),
        })
        with st.spinner("Scan + appels Tavily/météo/ELO + règles…"):
            try:
                res = api_post("/recommend/agent-local", json=payload)
            except Exception as exc:
                st.error(f"Erreur : {exc}")
                res = None

        if res:
            if res.get("odds_quota_exhausted"):
                empty_state(
                    "🚫",
                    f"Quota Odds API épuisé ({res.get('odds_quota_remaining', '?')} req restantes)",
                    f"Notre garde-fou bloque les scans en dessous de {health.get('odds_quota_minimum', 20)} "
                    "requêtes. Le quota se réinitialise le 1er du mois. "
                    "Pour abaisser le seuil, modifie ODDS_QUOTA_MINIMUM dans .env.",
                )
                res = None  # skip the rest of the rendering
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
                st.caption(
                    "🟢 **accepted** = pick à parier sereinement · "
                    "🟡 **flagged** = pick qui a déclenché une règle de prudence (edge anormalement élevé, "
                    "blessure du favori, etc.). À vérifier manuellement avant de placer la mise."
                )
                for i, p in enumerate(res["picks"], 1):
                    status = p.get("status", "accepted")
                    icon = "🟡" if status == "flagged" else "🟢"
                    label = (
                        f"{icon} #{i} · {p['home_team']} vs {p['away_team']} — "
                        f"{p['selection_label']} @ {p['best_odds']:.2f} · "
                        f"prob {p['model_prob']*100:.1f}% · edge {p['value_edge']*100:+.1f}%"
                    )
                    with st.expander(label):
                        kelly = p.get('kelly_stake', 0)
                        status_color = "orange" if status == "flagged" else "green"
                        st.markdown(
                            f"Statut : :{status_color}[**{status}**] · "
                            f"Mise Kelly : **${kelly:.2f}**"
                        )
                        for r in p.get("rationale", []):
                            st.write(f"- {r}")
            else:
                empty_state(
                    "🛑",
                    "Aucun pari n'a survécu aux règles de l'agent local",
                    "Les picks bruts ont tous été rejetés après calibration. "
                    "Cela peut être normal si le modèle vient d'être recalibré ou si les "
                    "ligues actives n'ont pas de matchs avec un edge fiable aujourd'hui.",
                )

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


def render_ai_agent_tab(filters: dict, agent_enabled: bool) -> None:
    st.subheader("Agent IA — Claude Sonnet 4.6")
    st.caption(
        "L'agent appelle les MÊMES outils que le scan manuel + en plus : recherche "
        "de news live (Tavily), blessures (API-Football), météo (Open-Meteo). "
        "Il raisonne en plusieurs étapes et justifie chaque pick."
    )

    if not agent_enabled:
        empty_state(
            "🤖",
            "Agent IA désactivé",
            "Configure-le en 3 étapes ci-dessous, puis recharge la page.",
        )
        st.markdown(
            """
            **Étapes pour activer Claude :**

            1. Crée une clé sur [console.anthropic.com](https://console.anthropic.com) (5$ de crédit gratuits)
            2. Ajoute `ANTHROPIC_API_KEY=sk-ant-...` dans le fichier `.env` à la racine du projet
            3. Recrée le container API pour charger la nouvelle clé :
               ```bash
               docker compose up -d --force-recreate api
               ```
               *Note : `docker compose restart api` ne suffit pas — il ne recharge pas `.env`.*

            En attendant, utilise l'onglet **🎯 Scan manuel** ou **🧠 Agent local** — ils calculent les
            mêmes probabilités, sans coût et sans raisonnement narratif.
            """
        )
        return

    extra = st.text_area(
        "Instructions additionnelles",
        placeholder="ex : éviter les nuls, prioriser les favoris à domicile, "
                    "ne recommander que des combinés à 2 jambes max…",
    )
    if st.button("🚀 Demander une recommandation à l'agent", type="primary", width='stretch'):
        payload = _payload_from_filters(filters, {"extra_instructions": extra or None})
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
