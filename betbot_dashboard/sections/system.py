"""System tabs — sources health, ML calibrator, tennis ELO, basketball model."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pandas as pd
import streamlit as st

from betbot_dashboard.api_client import api_get, api_post
from betbot_dashboard.styles import empty_state


def render_odds_key_config() -> None:
    """Self-service Odds API key rotation — no SSH, no restart."""
    with st.expander("🔑 Clé Odds API — changer sans redémarrage", expanded=False):
        try:
            status = api_get("/settings/odds-key")
        except Exception as exc:
            status = None
            st.warning(f"Statut indisponible : {exc}")
        if status:
            src = {"dashboard": "dashboard", "env": ".env", "none": "aucune"}.get(
                status.get("source"), status.get("source"))
            rem = status.get("remaining")
            st.caption(
                f"Clé actuelle : **{status.get('masked')}** (source : {src}) — "
                f"quota restant : **{rem if rem is not None else '?'}**"
            )
        st.caption(
            "Colle ta nouvelle clé gratuite (tous les ~4 jours). Elle est "
            "**vérifiée contre l'API puis appliquée immédiatement** — le prochain "
            "scan l'utilise, sans redémarrage ni SSH."
        )
        new_key = st.text_input("Nouvelle clé Odds API", type="password",
                                key="odds_key_input", placeholder="ex : c67bf38b…")
        if st.button("💾 Enregistrer & vérifier", type="primary", disabled=not new_key):
            with st.spinner("Vérification de la clé contre The Odds API…"):
                try:
                    res = api_post("/settings/odds-key", json={"key": new_key.strip()})
                except Exception as exc:
                    res = None
                    st.error(f"Erreur : {exc}")
            if res and res.get("saved"):
                st.success(
                    f"✅ Clé enregistrée et **active immédiatement** — quota restant : "
                    f"**{res.get('remaining')}**. Aucun redémarrage nécessaire."
                )
            elif res:
                st.error(f"❌ Non enregistrée : {res.get('reason')}")
    st.divider()


def render_sources_tab() -> None:
    render_odds_key_config()
    st.subheader("🔌 État des sources externes")
    st.caption(
        "Probe en temps réel. Les sources sont groupées par criticité : "
        "**critical** = sans elles le système ne peut pas fonctionner, "
        "**important** = dégrade la qualité du modèle, "
        "**optional** = ne touche qu'une fonctionnalité (agent IA, news, etc.)."
    )

    if "sources_health" not in st.session_state:
        st.session_state.sources_health = None

    btn_label = "🔄 Re-tester" if st.session_state.sources_health else "🩺 Tester maintenant"
    if st.button(btn_label, type="primary"):
        try:
            st.session_state.sources_health = api_get("/health/sources")
        except Exception as exc:
            st.error(f"Erreur : {exc}")
            st.session_state.sources_health = None

    health_data = st.session_state.sources_health
    if not health_data:
        empty_state(
            "🩺",
            "Aucune vérification effectuée",
            "Clique sur « Tester maintenant » pour sonder chaque source en direct.",
        )
        return

    # Summary KPIs at top
    sources = health_data.get("sources", [])
    n_ok = sum(1 for s in sources if s["status"] == "ok")
    n_ko = sum(1 for s in sources if s["status"] == "ko")
    n_unconf = sum(1 for s in sources if s["status"] == "not_configured")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Sources OK", f"{n_ok}/{len(sources)}")
    c2.metric("En panne", n_ko, delta="critique" if n_ko else None,
              delta_color="inverse" if n_ko else "off")
    c3.metric("Non configurées", n_unconf)
    # Show relative time since the probe ran — easier to parse at a
    # glance than the raw ISO timestamp ("il y a 3 min" vs "12:34:56").
    checked_iso = health_data.get("checked_at", "")
    relative_age = "—"
    if checked_iso:
        try:
            checked_dt = datetime.fromisoformat(checked_iso.replace("Z", "+00:00"))
            age_sec = int((datetime.now(timezone.utc) - checked_dt).total_seconds())
            if age_sec < 60:
                relative_age = f"il y a {age_sec}s"
            elif age_sec < 3600:
                relative_age = f"il y a {age_sec // 60}min"
            elif age_sec < 86400:
                relative_age = f"il y a {age_sec // 3600}h"
            else:
                relative_age = f"il y a {age_sec // 86400}j"
        except Exception:
            relative_age = checked_iso[:19].replace("T", " ")
    c4.metric("Dernière vérif.", relative_age)

    st.markdown("")

    # Group by criticality
    groups = {"critical": [], "important": [], "optional": []}
    for s in sources:
        groups.setdefault(s.get("criticality", "optional"), []).append(s)

    labels = {
        "critical":  ("🚨 Critique", "Indispensable au fonctionnement du système."),
        "important": ("⚠️ Important", "Dégrade la qualité du modèle si indisponible."),
        "optional":  ("ℹ️ Optionnel", "N'affecte qu'une fonctionnalité spécifique."),
    }
    status_icons = {"ok": "🟢", "ko": "🔴", "not_configured": "⚪"}
    status_text = {"ok": "OK", "ko": "DOWN", "not_configured": "non configuré"}

    for crit_key in ("critical", "important", "optional"):
        items = groups.get(crit_key, [])
        if not items:
            continue
        title, hint = labels[crit_key]
        st.markdown(f"#### {title}")
        st.caption(hint)
        for s in items:
            status = s["status"]
            cols = st.columns([0.4, 2.0, 1.3, 2.7])
            cols[0].markdown(status_icons.get(status, "⚪"))
            cols[1].markdown(f"**{s['name']}**")
            # Always show the WORD status (never colour-only — accessibility),
            # with latency appended for healthy sources.
            if status == "ok":
                cols[2].caption(f":green[OK] · {s.get('latency_ms', 0)} ms")
            elif status == "not_configured":
                cols[2].caption(f":orange[{status_text[status]}]")
            else:
                cols[2].caption(f":red[{status_text[status]}]")
            reason = s.get("reason") or ""
            cols[3].caption(reason[:140])


def render_calibrator_tab() -> None:
    st.subheader("🎚️ Calibrateur de probabilités")
    st.caption(
        "Apprend automatiquement la correction entre les **probabilités du modèle** "
        "et les **vrais taux de victoire** observés sur tes paris résolus, "
        "via une **régression isotonique** (Niculescu-Mizil & Caruana, 2005). "
        "Le retraînage est planifié chaque dimanche 03h30 UTC."
    )

    try:
        cal_status = api_get("/ml/calibrator/status")
    except Exception as exc:
        st.error(f"Erreur : {exc}")
        cal_status = None

    if not cal_status:
        return

    kpi = st.columns(4)
    if cal_status.get("available"):
        kpi[0].metric("Statut", "✓ actif", delta="opérationnel",
                      delta_color="normal")
        trained_at = cal_status.get("trained_at", "?")[:19].replace("T", " ")
        kpi[1].metric("Entraîné le", trained_at or "—")
    else:
        kpi[0].metric("Statut", "✗ inactif", delta="pas encore entraîné",
                      delta_color="off")
        kpi[1].metric("Entraîné le", "—")
    n_resolved = cal_status.get("n_resolved_bets", 0)
    min_samples = cal_status.get("min_samples_to_trust", 50)
    kpi[2].metric(
        "Paris résolus disponibles",
        f"{n_resolved}",
        delta=f"min {min_samples}" if n_resolved < min_samples else "OK",
        delta_color="off" if n_resolved < min_samples else "normal",
    )
    kpi[3].metric(
        "Prêt à entraîner",
        "Oui" if cal_status.get("ready_to_train") else "Non",
    )

    st.markdown("")

    # Manual retrain button
    if cal_status.get("ready_to_train"):
        st.success(
            f"Tu as {n_resolved} paris résolus — c'est suffisant pour calibrer.",
            icon="🎯",
        )
        if st.button("🔁 Re-entraîner maintenant", type="primary"):
            with st.spinner("Entraînement en cours…"):
                try:
                    result = api_post("/ml/calibrator/train")
                except Exception as exc:
                    st.error(f"Erreur : {exc}")
                    result = None
            if result:
                if result.get("trained"):
                    st.success(
                        f"✅ Calibrateur ré-entraîné sur {result['n_samples']} paris. "
                        f"Brier score : **{result['brier_before']} → {result['brier_after']}** "
                        f"(plus bas = mieux calibré)."
                    )
                else:
                    st.warning(f"Entraînement annulé : {result.get('reason')}")
    else:
        empty_state(
            "📊",
            f"Pas assez de paris résolus ({n_resolved}/{min_samples})",
            "Le calibrateur a besoin d'au moins {} paris résolus pour fitter "
            "une isotonic regression robuste. Tes scans génèrent des paris ; "
            "le worker les résout automatiquement à 04h chaque jour. "
            "En attendant, le système utilise les probabilités brutes du modèle.".format(min_samples),
        )

    # Cold-start bootstrap — always available, useful on fresh installs
    st.divider()
    st.markdown("### 🚀 Démarrage à froid")
    st.caption(
        "Sur une installation fraîche tu n'as aucun pari résolu, donc le "
        "calibrateur reste inactif pendant des semaines. Cette action lance "
        "un backtest walk-forward sur les 5 grandes ligues européennes et "
        "entraîne le calibrateur sur les prédictions synthétiques produites. "
        "Le calibrateur sera **opérationnel immédiatement** ; le job hebdo "
        "écrasera plus tard cette version avec un fit basé sur tes vrais paris."
    )
    if cal_status.get("available") and cal_status.get("source", "") != "cold_start_backtest":
        st.info(
            "✓ Un calibrateur est déjà fitté sur tes vrais paris — le cold-start "
            "n'est pas recommandé (il remplacerait le fit réel par un fit synthétique).",
            icon="ℹ️",
        )
    if st.button("🚀 Initialiser depuis l'historique (5 ligues, ~30-60 s)"):
        with st.spinner("Backtests EPL/Liga/Bundesliga/Serie A/Ligue 1 + fit…"):
            try:
                result = api_post("/ml/calibrator/cold-start")
            except Exception as exc:
                st.error(f"Erreur : {exc}")
                result = None
        if result:
            if result.get("trained"):
                st.success(
                    f"✅ Calibrateur initialisé sur {result['n_samples']} "
                    f"prédictions synthétiques. "
                    f"Brier : **{result['brier_before']} → {result['brier_after']}**"
                )
                per_league = result.get("per_league", {})
                if per_league:
                    st.markdown("**Échantillons par ligue :**")
                    for sport, n in per_league.items():
                        st.markdown(f"- `{sport}` : {n} samples")
            else:
                st.warning(f"Cold-start annulé : {result.get('reason')}")
                if result.get("notes"):
                    with st.expander("Détails par ligue"):
                        for note in result["notes"]:
                            st.text(note)


def render_tennis_tab(health: dict) -> None:
    st.subheader("🎾 Modèle tennis — ELO surface-aware")
    st.caption(
        "Ratings ELO ATP construits depuis l'historique [Sackmann](https://github.com/JeffSackmann/tennis_atp). "
        "**ELO global** + **3 ratings par surface** (Hard / Clay / Grass), pondéré 50/50. "
        "Refresh hebdomadaire automatique (lundi 05h00 UTC)."
    )

    try:
        tn_status = api_get("/tennis/status")
    except Exception as exc:
        st.error(f"Erreur : {exc}")
        tn_status = None

    if not tn_status:
        return

    if tn_status.get("available"):
        # Distinguish "model file loaded" (always true once bootstrapped)
        # from "actively scanned by the worker" (depends on the env flag
        # AND on whether a Grand Slam is currently in season).
        active_sports_now = health.get("active_sports", [])
        has_active_tennis = any(s.startswith("tennis_") for s in active_sports_now)
        kpi = st.columns(4)
        kpi[0].metric("Modèle ELO", "✓ chargé")
        scanned_label = "✓ oui" if has_active_tennis else "✗ pas en saison"
        kpi[1].metric("Scanné par le worker", scanned_label,
                      delta_color="normal" if has_active_tennis else "off")
        kpi[2].metric("Joueurs notés", tn_status.get("n_players", 0))
        most_recent = tn_status.get("most_recent_match", "?")
        kpi[3].metric("Dernier match", most_recent or "—")

        # Grand Slam countdown when nothing is currently active : helps
        # the user understand WHEN the tennis picks will start showing
        # up in the validation queue. Hardcoded calendar (these dates
        # are stable from year to year within a few days).
        if not has_active_tennis:
            gs_calendar = [
                ("Australian Open", date(2027, 1, 18)),
                ("Roland Garros",   date(2026, 5, 24)),
                ("Wimbledon",       date(2026, 6, 29)),
                ("US Open",         date(2026, 8, 25)),
            ]
            today_d = date.today()
            upcoming = sorted(
                [(name, d) for name, d in gs_calendar if d >= today_d],
                key=lambda x: x[1],
            )
            if upcoming:
                next_name, next_date = upcoming[0]
                days_until = (next_date - today_d).days
                st.info(
                    f"🎾 **Aucun Grand Slam actuellement en saison.** "
                    f"Prochain : **{next_name}** dans **{days_until} jour(s)** "
                    f"({next_date.isoformat()}). "
                    f"Le worker recommencera à scanner du tennis automatiquement "
                    f"quand The Odds API listera le tournoi comme actif.",
                    icon="📅",
                )

        st.markdown("")
        st.markdown("**Top 5 ATP (ELO global)**")
        top5 = tn_status.get("top5", [])
        if top5:
            df_top = pd.DataFrame(top5).rename(columns={"name": "Joueur", "overall": "ELO"})
            st.dataframe(df_top, width='stretch', hide_index=True,
                         column_config={"ELO": st.column_config.NumberColumn(format="%.1f")})

        st.markdown("")
        st.markdown("### 🔮 Prévisualiser un duel")
        c1, c2, c3 = st.columns([2, 2, 1])
        home_in = c1.text_input("Joueur 1", value="Carlos Alcaraz")
        away_in = c2.text_input("Joueur 2", value="Jannik Sinner")
        surf_in = c3.selectbox("Surface", ["Hard", "Clay", "Grass"], index=0)
        if st.button("🎾 Prédire"):
            try:
                pred = api_get("/tennis/predict", home=home_in, away=away_in, surface=surf_in)
            except Exception as exc:
                st.error(f"Erreur : {exc}")
                pred = None
            if pred and not pred.get("error"):
                pcol = st.columns(2)
                pcol[0].metric(
                    f"{pred['matched_home']}",
                    f"{pred['home_win']*100:.1f}%",
                    delta=f"ELO {pred['rating_home']:.0f}",
                    delta_color="off",
                )
                pcol[1].metric(
                    f"{pred['matched_away']}",
                    f"{pred['away_win']*100:.1f}%",
                    delta=f"ELO {pred['rating_away']:.0f}",
                    delta_color="off",
                )
            elif pred:
                st.warning(pred.get("error", "Joueur introuvable"))

        st.markdown("")
        st.markdown("### 🔁 Refresh manuel")
        st.caption("Le worker le fait automatiquement chaque lundi 05h00 UTC. "
                   "Force-le ici si tu veux les ratings les plus récents avant un Grand Slam.")
        if st.button("🔁 Re-calculer depuis Sackmann"):
            with st.spinner("Téléchargement + calcul ELO…"):
                try:
                    result = api_post("/tennis/refresh")
                except Exception as exc:
                    st.error(f"Erreur : {exc}")
                    result = None
            if result and result.get("trained"):
                st.success(
                    f"✅ ELO ré-entraîné : {result['n_matches']} matchs, "
                    f"{result['n_players']} joueurs (années {result['years']})."
                )
            elif result:
                st.warning(f"Refresh échoué : {result}")
    else:
        empty_state(
            "🎾",
            "Aucun rating ELO calculé",
            "Lance un premier bootstrap : `docker compose exec api python -m betbot.tennis_bootstrap` "
            "ou clique sur le bouton refresh ci-dessous.",
        )
        if st.button("🔁 Calculer maintenant"):
            with st.spinner("Téléchargement Sackmann + calcul ELO…"):
                try:
                    result = api_post("/tennis/refresh")
                    if result.get("trained"):
                        st.success(f"✅ {result['n_players']} joueurs notés.")
                        st.rerun()
                except Exception as exc:
                    st.error(f"Erreur : {exc}")


def render_basket_tab(health: dict) -> None:
    st.subheader("🏀 Modèle basket — pace + offensive / defensive rating")
    st.caption(
        "Stats équipes scrapées depuis [basketball-reference.com](https://www.basketball-reference.com). "
        "Modèle Dean Oliver : projection des points via **pace × (ORtg + DRtg adverse) / 200**. "
        "Win prob via CDF normale avec σ adaptatif au pace (base 11.0 pts, "
        "élargi pour les matchs à haut tempo), home advantage NBA = 2.7 pts. "
        "Refresh hebdo automatique chaque mardi 05h00 UTC."
    )

    try:
        bb_status_res = api_get("/basketball/status")
    except Exception as exc:
        st.error(f"Erreur : {exc}")
        bb_status_res = None

    if not bb_status_res:
        return

    if bb_status_res.get("available"):
        # Same distinction as the tennis tab : "model loaded" vs "scanned
        # by worker today". For basket the gating is :
        #   - MULTI_SPORT_BASKETBALL flag in .env  (controllable)
        #   - NBA / EuroLeague currently in active_sports (Odds API tells us)
        active_sports_now = health.get("active_sports", [])
        has_active_basket = any(
            s.startswith("basketball_") for s in active_sports_now
        )
        kpi = st.columns(4)
        kpi[0].metric("Modèle stats", "✓ chargé")
        scanned_label = "✓ oui" if has_active_basket else "✗ off / hors saison"
        kpi[1].metric("Scanné par le worker", scanned_label,
                      delta_color="normal" if has_active_basket else "off")
        kpi[2].metric("Équipes notées", bb_status_res.get("n_teams", 0))
        by_league = bb_status_res.get("by_league", {})
        kpi[3].metric("Ligues couvertes", ", ".join(by_league.keys()) or "—")

        if not has_active_basket:
            st.info(
                "🏀 Basket non scanné en ce moment. Soit le flag "
                "`MULTI_SPORT_BASKETBALL` est à `0` dans `.env`, soit "
                "NBA + EuroLeague sont entre saisons (NBA reprend mi-octobre, "
                "EuroLeague aussi). Pour l'activer : édite `.env` puis "
                "`docker compose up -d --force-recreate api worker`.",
                icon="ℹ️",
            )

        st.markdown("")
        st.markdown("**Top 5 par Net Rating (ORtg − DRtg)**")
        top5 = bb_status_res.get("top5_net_rating", [])
        if top5:
            df_top = pd.DataFrame(top5).rename(columns={
                "name": "Équipe", "off": "ORtg", "def": "DRtg", "net": "Net",
            })
            st.dataframe(
                df_top, width='stretch', hide_index=True,
                column_config={
                    "ORtg": st.column_config.NumberColumn(format="%.1f"),
                    "DRtg": st.column_config.NumberColumn(format="%.1f"),
                    "Net":  st.column_config.NumberColumn(format="%+.1f"),
                },
            )

        st.markdown("")
        st.markdown("### 🔮 Prévisualiser un match")
        c1, c2, c3 = st.columns([2, 2, 1])
        home_in = c1.text_input("Domicile", value="Boston Celtics", key="bb_home")
        away_in = c2.text_input("Extérieur", value="Los Angeles Lakers", key="bb_away")
        league_in = c3.selectbox("Ligue", ["nba", "euroleague"], index=0, key="bb_league")
        if st.button("🏀 Prédire", key="bb_predict_btn"):
            try:
                pred = api_get("/basketball/predict",
                               home=home_in, away=away_in, league=league_in)
            except Exception as exc:
                st.error(f"Erreur : {exc}")
                pred = None
            if pred and not pred.get("error"):
                pcol = st.columns(2)
                pcol[0].metric(
                    f"{pred['matched_home']}",
                    f"{pred['home_win']*100:.1f}%",
                    delta=f"{pred['expected_home_points']} pts",
                    delta_color="off",
                )
                pcol[1].metric(
                    f"{pred['matched_away']}",
                    f"{pred['away_win']*100:.1f}%",
                    delta=f"{pred['expected_away_points']} pts",
                    delta_color="off",
                )
                st.caption(
                    f"Total prévu : **{pred['expected_total']} pts** · "
                    f"Marge : **{pred['expected_margin']:+.1f}** "
                    f"(positive = équipe à domicile favorite)"
                )
            elif pred:
                st.warning(pred.get("error", "Équipe introuvable"))

        st.markdown("")
        st.markdown("### 🔁 Refresh manuel")
        st.caption("Le worker scrape automatiquement chaque mardi 05h00 UTC.")
        if st.button("🔁 Re-scraper bb-ref", key="bb_refresh_btn"):
            with st.spinner("Téléchargement basketball-reference + calcul…"):
                try:
                    result = api_post("/basketball/refresh")
                except Exception as exc:
                    st.error(f"Erreur : {exc}")
                    result = None
            if result and result.get("trained"):
                st.success(f"✅ {result['n_teams']} équipes mises à jour.")
            elif result:
                st.warning(f"Refresh échoué : {result}")
    else:
        empty_state(
            "🏀",
            "Aucune stat équipe calculée",
            "Lance un premier scrape : `docker compose exec api python -m betbot.basketball_bootstrap` "
            "ou clique sur le bouton ci-dessous.",
        )
        if st.button("🔁 Scraper maintenant", key="bb_first_scrape"):
            with st.spinner("Téléchargement basketball-reference…"):
                try:
                    result = api_post("/basketball/refresh")
                    if result.get("trained"):
                        st.success(f"✅ {result['n_teams']} équipes notées.")
                        st.rerun()
                except Exception as exc:
                    st.error(f"Erreur : {exc}")
