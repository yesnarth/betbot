"""Decision tabs — manual scan, local agent, AI agent."""
from __future__ import annotations

import streamlit as st

from betbot_dashboard.api_client import api_get, api_post
from betbot_dashboard.components.picks import (
    render_picks_table, render_parlays, render_safe_picks, is_early_resolving,
    render_over_picks, is_over_goals,
)
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


def render_safe_fast_tab(filters: dict) -> None:
    st.subheader("🟢 Sûr & rapide — forte probabilité, validation précoce")
    st.caption(
        "Ne retient que les paris à **forte probabilité** (≥ 72 %) et **+EV**, "
        "petites cotes autorisées. Priorité aux marchés **⚡ précoces** (Plus de "
        "0.5 / 1.5 but) — gagnés dès qu'assez de buts tombent, **avant la fin**."
    )
    st.info(
        "⚠️ **Forte proba ≠ profit garanti.** Ces paris sont bien pricés par le "
        "marché : on ne les garde que quand le modèle y trouve une **vraie valeur** "
        "(+EV). Une petite cote gagnée souvent rapporte peu ; la défaite efface "
        "plusieurs gains. **Mise petit, vise la valeur — pas juste la fréquence.**",
        icon="🎯",
    )
    if st.button("🟢 Lancer le scan sûr & rapide", type="primary", width='stretch'):
        payload = _payload_from_filters(
            filters, {"min_edge": 0.01, "min_prob": 0.72, "min_odds": 1.05})
        with st.spinner("Récupération des cotes + calcul…"):
            try:
                st.session_state["safe_res"] = api_post("/recommend/manual", json=payload)
            except Exception as exc:
                st.error(f"Erreur : {exc}")
                st.session_state["safe_res"] = None

    res = st.session_state.get("safe_res")
    if not res:
        return
    if res.get("odds_quota_exhausted"):
        empty_state("🚫", f"Quota Odds API épuisé ({res.get('odds_quota_remaining', '?')} req)",
                    "Recharge une clé dans **🔌 Sources → 🔑 Clé Odds API**.")
        return
    picks = res.get("picks", [])
    early = sum(1 for p in picks if is_early_resolving(p.get("selection_code")))
    cols = st.columns(3)
    cols[0].metric("Matchs scannés", res.get("n_events_scanned", 0))
    cols[1].metric("Paris sûrs (+EV)", res.get("n_picks", 0))
    cols[2].metric("⚡ Validables avant la fin", early)
    if not picks:
        empty_state(
            "🎯", "Aucun pari sûr ne passe le filtre",
            "Aucun marché forte-proba **et** +EV pour l'instant — normal hors-saison, "
            "ou le marché price déjà bien ces lignes. Reviens à la reprise.",
        )
        return
    st.markdown("### 🟢 Paris sûrs (triés par probabilité)")
    render_safe_picks(picks)


def render_over_tab(filters: dict) -> None:
    st.subheader("⚽ Over — spécial buts (jamais Under)")
    st.caption(
        "Scanne **uniquement** les paris **Plus de X buts** (total du match). Le "
        "signal clé est les **buts attendus** (λ) du modèle : plus il est haut, "
        "plus l'Over est probable. On ne garde que les Over à **valeur réelle** "
        "(+EV) — pas juste les probables. Meilleure ligne par match, triée par valeur."
    )
    choice = st.radio("Ligne minimale", ["Toutes", "≥ 1.5", "≥ 2.5", "≥ 3.5"],
                      horizontal=True, index=0, key="over_line_choice")
    min_line = {"Toutes": 0.0, "≥ 1.5": 1.5, "≥ 2.5": 2.5, "≥ 3.5": 3.5}[choice]

    if st.button("⚽ Lancer le scan Over", type="primary", width='stretch'):
        payload = _payload_from_filters(
            filters, {"min_edge": 0.02, "min_prob": 0.40, "min_odds": 1.05})
        with st.spinner("Récupération des cotes + calcul des buts attendus…"):
            try:
                st.session_state["over_res"] = api_post("/recommend/manual", json=payload)
            except Exception as exc:
                st.error(f"Erreur : {exc}")
                st.session_state["over_res"] = None

    res = st.session_state.get("over_res")
    if not res:
        return
    if res.get("odds_quota_exhausted"):
        empty_state("🚫", f"Quota Odds API épuisé ({res.get('odds_quota_remaining', '?')} req)",
                    "Recharge une clé dans **🔌 Sources → 🔑 Clé Odds API**.")
        return
    picks = res.get("picks", [])
    overs = [p for p in picks if is_over_goals(p.get("selection_code"))]
    lambdas = [
        float(p.get("lambda_home") or 0) + float(p.get("lambda_away") or 0)
        for p in overs
        if (float(p.get("lambda_home") or 0) + float(p.get("lambda_away") or 0)) > 0
    ]
    cols = st.columns(3)
    cols[0].metric("Matchs scannés", res.get("n_events_scanned", 0))
    cols[1].metric("Paris Over (+EV)", len(overs))
    cols[2].metric("⌀ Buts attendus", round(sum(lambdas) / len(lambdas), 2) if lambdas else "—")
    if not overs:
        empty_state(
            "⚽", "Aucun pari Over à valeur pour l'instant",
            "Aucun Over +EV — normal hors-saison, ou le marché price déjà bien les "
            "totaux. Reviens à la reprise, ou baisse « Edge minimum » dans la sidebar.",
        )
        return
    st.markdown("### ⚽ Meilleurs Over (triés par valeur)")
    render_over_picks(picks, min_line)


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
        with st.spinner(
            "L'agent raisonne… (de ~30 s à plusieurs minutes selon la complexité). "
            "Ne ferme pas l'onglet — même si la réponse expire côté navigateur, le "
            "run reste consultable dans 🛠️ Outils → 📜 Historique IA."
        ):
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


def render_target_parlay_tab(filters: dict) -> None:
    st.subheader("🎯 Combiné gros multiplicateur — favoris empilés")
    st.info(
        "**La cote cible est un PLAFOND, pas un objectif obligatoire.** Le bot "
        "génère le nombre de combinés demandé, chacun **aussi gros que possible "
        "SANS dépasser** ce plafond — donc tu obtiens des combinés même s'ils "
        "n'atteignent pas le plafond (ex. ×60 un jour creux). Plus le plafond est "
        "**bas**, plus le combiné a de chances de tomber. Il les construit en "
        "**empilant des FAVORIS** (chaque jambe garde la garde no-vig + un edge "
        "réel), **pas** des longshots voués à l'échec → tickets à **EV positive**. "
        "Forte variance quand même. À placer toi-même — **non suivi au bankroll**.",
        icon="🎯",
    )

    c1, c2, c3 = st.columns(3)
    target = c1.number_input("Cote combinée MAX (plafond)", min_value=2.0, max_value=100000.0,
                             value=100.0, step=50.0,
                             help="Plafond à ne pas dépasser (défaut 100). Le bot vise le plus gros combiné possible ≤ ce nombre, sans jamais le dépasser ni être obligé de l'atteindre. Plus bas = plus de chances de gagner ; plus haut (ex. 1000) = plus gros gain, plus rare.")
    max_legs = c2.slider("Jambes max", 2, 20, 14)
    n_combos = c3.slider("Combinés à générer", 1, 10, 3)
    c4, c5 = st.columns(2)
    max_leg_odds = c4.slider(
        "Cote max par jambe (favoris)", 1.3, 5.0, 2.5, 0.1,
        help="Plafonne la cote de chaque jambe → on atteint la cible en empilant "
             "des FAVORIS, pas des longshots. Plus c'est bas, plus il faut de jambes.")
    min_prob = c5.slider(
        "Proba min par jambe", 0.30, 0.80, 0.50, 0.05,
        help="Chaque jambe doit être un favori (plus de chances de gagner que de "
             "perdre). Relâche un peu si trop peu de combinés sont trouvés.")
    today_only = st.checkbox("Aujourd'hui seulement",
                             value=bool(filters.get("today_only", False)), key="tp_today")
    sport = None if filters.get("sport") in (None, "Toutes") else filters.get("sport")

    if st.button(f"🎯 Générer {n_combos} combiné(s) ≤ ×{target:.0f}", type="primary", width='stretch'):
        payload = {
            "sport_key": sport,
            "today_only": today_only,
            "target_odds": float(target),
            "max_legs": int(max_legs),
            "n_combos": int(n_combos),
            "max_leg_odds": float(max_leg_odds),
            "min_prob": float(min_prob),
        }
        with st.spinner("Scan large de toutes les ligues + assemblage glouton…"):
            res = api_post("/recommend/parlay-target", json=payload)

        if res.get("odds_quota_exhausted"):
            empty_state("🚫", "Quota Odds API épuisé", "Réessaie plus tard.")
            return

        cols = st.columns(3)
        cols[0].metric("Jambes candidates", res.get("n_candidates", 0))
        cols[1].metric("Matchs scannés", res.get("n_events_scanned", 0))
        cols[2].metric("Combinés générés", len(res.get("parlays", [])))

        parlays = res.get("parlays", [])
        if parlays:
            st.caption(
                f"Plafond ×{target:.0f} — les combinés ci-dessous sont les plus gros "
                "atteignables **sans le dépasser** (ils n'ont pas à l'atteindre)."
            )
            render_parlays(parlays)
        else:
            empty_state(
                "🎯",
                "Pas assez de jambes-favoris aujourd'hui",
                f"Il faut au moins 2 favoris éligibles (≤ ×{max_leg_odds:.1f}/jambe, "
                f"proba ≥ {min_prob:.0%}, edge réel vs marché) pour former un combiné. "
                "Hors-saison il y a peu de matchs : relâche « Cote max par jambe » ou "
                "« Proba min », décoche « Aujourd'hui seulement » (inclut les jours "
                "suivants), ou active `SCAN_ALL_SOCCER=1` pour couvrir plus de ligues.",
            )


def render_live_tab(filters: dict) -> None:
    import pandas as pd

    st.subheader("🔴 Scanner live (in-play)")
    st.warning(
        "**Données à ~30 s + tu places à la main → place vite.** Le scanner compare "
        "les cotes **live** au modèle in-play (score courant + temps restant). La minute "
        "foot/basket est **estimée** (le flux ne donne pas le chrono) ; le tennis suit "
        "l'état des sets. **Paris simples uniquement** (pas de combinés en live).",
        icon="🔴",
    )
    sport = None if filters.get("sport") in (None, "Toutes") else filters.get("sport")
    c1, c2 = st.columns(2)
    min_edge = c1.slider("Edge minimum (%)", 0.0, 20.0, 4.0, 0.5, key="live_edge")
    min_odds = c2.slider("Cote minimum", 1.0, 5.0, 1.3, 0.1, key="live_odds")

    if st.button("🔴 Scanner le live maintenant", type="primary", width='stretch'):
        payload = {"sport_key": sport, "min_edge": round(min_edge / 100, 4), "min_odds": min_odds}
        with st.spinner("Récupération des cotes + scores live…"):
            st.session_state["live_res"] = api_post("/recommend/live", json=payload)

    res = st.session_state.get("live_res")
    if not res:
        return

    cols = st.columns(2)
    cols[0].metric("Matchs en cours", res.get("n_live_events", 0))
    cols[1].metric("Value bets live", len(res.get("picks", [])))
    ca = (res.get("checked_at") or "")[:19].replace("T", " ")
    st.caption(f"Dernier scan : {ca} UTC — **relance** pour rafraîchir (données ~30 s).")

    picks = res.get("picks", [])
    if not picks:
        empty_state("🔴", "Aucune value bet live pour l'instant",
                    "Soit aucun match en cours, soit aucune valeur détectée. Relance dans quelques minutes.")
        return

    df = pd.DataFrame(picks)
    show = [c for c in ["home_team", "away_team", "live_score", "league", "selection_label",
                        "best_odds", "model_prob", "value_edge", "kelly_stake",
                        "reliability", "best_book", "model_type"] if c in df.columns]
    disp = df[show].rename(columns={
        "home_team": "Domicile", "away_team": "Extérieur", "live_score": "Score",
        "league": "Ligue", "selection_label": "Pari", "best_odds": "Cote",
        "model_prob": "Proba", "value_edge": "Edge", "kelly_stake": "Mise",
        "reliability": "Fiab.", "best_book": "Book", "model_type": "Modèle",
    })
    cfg = {}
    if "Cote" in disp.columns:
        cfg["Cote"] = st.column_config.NumberColumn(format="%.2f")
    if "Proba" in disp.columns:
        disp["Proba"] = disp["Proba"] * 100
        cfg["Proba"] = st.column_config.NumberColumn(format="%.1f%%")
    if "Edge" in disp.columns:
        disp["Edge"] = disp["Edge"] * 100
        cfg["Edge"] = st.column_config.NumberColumn(format="%+.1f%%")
    if "Mise" in disp.columns:
        cfg["Mise"] = st.column_config.NumberColumn(format="$%.2f")
    if "Fiab." in disp.columns:
        cfg["Fiab."] = st.column_config.NumberColumn(format="%.2f")
    st.dataframe(disp, width='stretch', hide_index=True, column_config=cfg)
