"""Matches tabs — available events, validation queue, pending bets."""
from __future__ import annotations

import time

import pandas as pd
import streamlit as st

from betbot_dashboard.api_client import api_get, api_post
from betbot_dashboard.styles import empty_state


LEAGUE_LABELS = {
    # Football — D1 grandes ligues européennes + CL
    "soccer_epl": "⚽ Premier League",
    "soccer_spain_la_liga": "⚽ La Liga",
    "soccer_germany_bundesliga": "⚽ Bundesliga",
    "soccer_italy_serie_a": "⚽ Serie A",
    "soccer_france_ligue1": "⚽ Ligue 1",
    "soccer_uefa_champs_league": "⚽ Champions League",
    "soccer_africa_cup_of_nations": "⚽ CAN",
    # Football — couverture étendue
    "soccer_efl_champ": "⚽ Championship 🇬🇧",
    "soccer_netherlands_eredivisie": "⚽ Eredivisie 🇳🇱",
    "soccer_portugal_primeira_liga": "⚽ Primeira Liga 🇵🇹",
    # Tennis
    "tennis_atp_aus_open": "🎾 Australian Open",
    "tennis_atp_french_open": "🎾 Roland Garros",
    "tennis_atp_wimbledon": "🎾 Wimbledon",
    "tennis_atp_us_open": "🎾 US Open",
    # Basketball
    "basketball_nba": "🏀 NBA",
    "basketball_euroleague": "🏀 EuroLeague",
}


def render_events_tab(filters: dict) -> None:
    st.subheader("Matchs disponibles")
    st.caption("Liste brute des matchs visibles côté Odds API, sans filtre de modèle.")
    if "events_data" not in st.session_state:
        st.session_state.events_data = None

    btn_label = "🔄 Rafraîchir la liste" if st.session_state.events_data else "🔍 Charger les matchs"
    if st.button(btn_label, width='stretch'):
        try:
            params = {"today_only": filters["today_only"]}
            if filters["sport"] != "Toutes":
                params["sport_key"] = filters["sport"]
            st.session_state.events_data = api_get("/events", **params)
            st.session_state.events_ts = time.time()
        except Exception as exc:
            st.error(f"Erreur : {exc}")
            st.session_state.events_data = None

    ev = st.session_state.events_data
    if ev:
        # Cotes périssables : prévenir si la liste affichée commence à dater.
        age = time.time() - st.session_state.get("events_ts", 0.0)
        if age > 600:
            st.warning(
                f"⚠️ Liste chargée il y a ~{int(age // 60)} min — les cotes ont pu "
                f"bouger. Clique « 🔄 Rafraîchir la liste » pour les mettre à jour."
            )
        st.metric("Total", ev["total"])
        for sk, items in ev["by_sport"].items():
            label = f"{LEAGUE_LABELS.get(sk, sk)} ({len(items)})"
            with st.expander(label, expanded=True):
                df = pd.DataFrame(items)
                if not df.empty:
                    visible_cols = [c for c in [
                        "home_team", "away_team", "commence_time", "n_bookmakers",
                    ] if c in df.columns]
                    disp = df[visible_cols].rename(columns={
                        "home_team": "Domicile", "away_team": "Extérieur",
                        "commence_time": "Coup d'envoi", "n_bookmakers": "Books",
                    })
                    st.dataframe(disp, width='stretch', hide_index=True)


def render_validate_tab(health: dict) -> None:
    st.subheader("🔔 Picks à valider")
    scan_hours = health.get("scan_hours") or []
    if scan_hours:
        scan_origin = (
            f"Le worker a proposé ces picks lors des scans automatiques "
            f"({' / '.join(scan_hours)}). "
        )
    else:
        scan_origin = (
            "Auto-scan désactivé (SCAN_HOURS vide). Ces picks viennent des "
            "scans manuels que tu lances toi-même via **🛠️ Outils → Scan manuel** "
            "+ le bouton **💾 Sauvegarder ces picks comme proposés**. "
        )
    st.caption(
        scan_origin +
        "**Aucun argent n'a été engagé** — c'est à toi de placer le pari chez ton "
        "bookmaker, puis de revenir cliquer **« J'ai placé »** pour que le solde "
        "soit débité du stake Kelly. **« Skipper »** archive le pick sans débit. "
        "Skip par erreur ? Voir la section « ↩️ Skipped récemment » plus bas."
    )

    try:
        proposed = api_get("/predictions/proposed")
    except Exception as exc:
        st.error(f"Erreur : {exc}")
        proposed = []

    if not proposed:
        if scan_hours:
            hint = (
                f"Les picks proposés par le worker apparaîtront ici après le prochain "
                f"scan automatique ({' ou '.join(scan_hours)} Europe/Paris). "
                f"Auto-archivage des picks proposed > 36h."
            )
        else:
            hint = (
                "Auto-scan désactivé. Va dans **🛠️ Outils → Scan manuel**, "
                "lance le scan, puis clique **💾 Sauvegarder ces picks comme "
                "proposés** pour qu'ils apparaissent ici. "
                "Auto-archivage des picks proposed > 36h."
            )
        empty_state("🔔", "Aucun pick en attente de validation", hint)
    else:
        st.markdown(f"**{len(proposed)} pick(s) à valider**")
        for p in proposed:
            pid = p["id"]
            label = (
                f"#{pid} · {p['home_team']} vs {p['away_team']} — "
                f"{p['selection']} @ {p['best_odds']:.2f} · "
                f"prob {p['model_prob']*100:.1f}% · edge {p['value_edge']*100:+.1f}% · "
                f"Kelly **${p['kelly_stake']:.2f}**"
            )
            with st.expander(label, expanded=False):
                c1, c2, c3 = st.columns([1, 1, 2])
                bookmaker = c3.text_input(
                    "Bookmaker", value="",
                    placeholder="ex : pinnacle, bet365, unibet…",
                    key=f"bm_{pid}",
                )
                confirm_key = f"await_confirm_{pid}"
                if c1.button("✅ J'ai placé", key=f"confirm_{pid}", type="primary"):
                    st.session_state[confirm_key] = True
                if c2.button("❌ Skipper", key=f"skip_{pid}"):
                    try:
                        api_post(f"/predictions/{pid}/skip",
                                 json={"reason": "user_skipped"})
                        st.toast(f"Pick #{pid} skippé.", icon="❌")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Erreur : {exc}")

                # Two-step confirmation — confirming DEBITS the bankroll, so we
                # never act on a single (possibly accidental) click.
                if st.session_state.get(confirm_key):
                    st.warning(
                        f"Confirmer le **débit de ${p['kelly_stake']:.2f}** du bankroll "
                        f"pour le pick #{pid} ({p['home_team']} vs {p['away_team']}) ?"
                    )
                    cc1, cc2 = st.columns(2)
                    if cc1.button("Oui, débiter", key=f"confirm_yes_{pid}", type="primary"):
                        try:
                            api_post(f"/predictions/{pid}/confirm-placed",
                                     json={"bookmaker": bookmaker or None})
                            st.session_state.pop(confirm_key, None)
                            st.toast(
                                f"Pick #{pid} confirmé — bankroll débité de "
                                f"${p['kelly_stake']:.2f}.", icon="✅",
                            )
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Erreur : {exc}")
                    if cc2.button("Annuler", key=f"confirm_no_{pid}"):
                        st.session_state.pop(confirm_key, None)
                        st.rerun()

                st.markdown(f"**Sport** : `{p.get('sport_key', '—')}` · "
                           f"**Modèle** : `{p.get('model_type', '—')}` · "
                           f"**Best book auto-détecté** : `{p['best_book']}`")
                st.caption(f"Proposé le : {p['created_at'][:19]}")

    # ─── Skipped récemment — récupération d'accidents ─────────────────────
    try:
        skipped = api_get("/predictions/skipped", limit=10)
    except Exception:
        skipped = []
    if skipped:
        st.markdown("---")
        with st.expander(f"↩️ Skipped récemment ({len(skipped)})", expanded=False):
            st.caption(
                "Picks que tu as déjà skippés (ou auto-archivés par le cron 36h). "
                "Si tu en as skippé un par erreur, click **↩️ Annuler skip** "
                "pour le remettre dans la file de validation."
            )
            for sk in skipped:
                pid = sk["id"]
                resolved_marker = " · ✓ résolu" if sk.get("result") else ""
                label = (
                    f"#{pid} · {sk['home_team']} vs {sk['away_team']} — "
                    f"{sk['selection']} @ {sk['best_odds']:.2f} · "
                    f"edge {sk['value_edge']*100:+.1f}%{resolved_marker}"
                )
                cols = st.columns([4, 1])
                cols[0].markdown(label)
                # Only allow unskip if the match isn't already resolved.
                # A resolved skipped pick is locked-in history.
                if not sk.get("result"):
                    if cols[1].button("↩️ Annuler skip",
                                      key=f"unskip_{pid}", width='stretch'):
                        try:
                            api_post(f"/predictions/{pid}/unskip", json={})
                            st.toast(f"Pick #{pid} remis en file de validation.", icon="↩️")
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Erreur : {exc}")
                else:
                    cols[1].caption(f"_{sk['result']}_")


def render_pending_tab() -> None:
    st.subheader("⏳ Paris confirmés en attente de résolution")
    st.caption(
        "Les paris que **tu as confirmé avoir placés** chez ton bookmaker. "
        "Le worker récupère les résultats à 04h chaque jour et met à jour "
        "ton solde automatiquement (gain × cote, ou stake perdu)."
    )
    try:
        rows = api_get("/predictions/pending")
        if not rows:
            empty_state(
                "⏳",
                "Aucun pari confirmé en attente",
                "Va dans « 🔔 Picks à valider » pour confirmer les "
                "recommandations du worker que tu as réellement placées.",
            )
        else:
            df = pd.DataFrame(rows)
            cols = [c for c in [
                "id", "created_at", "home_team", "away_team", "selection",
                "best_odds", "model_prob", "value_edge", "kelly_stake",
                "placed_bookmaker",
            ] if c in df.columns]
            disp = df[cols].rename(columns={
                "id": "ID", "created_at": "Confirmé le",
                "home_team": "Domicile", "away_team": "Extérieur",
                "selection": "Pari", "best_odds": "Cote",
                "model_prob": "Proba modèle", "value_edge": "Edge",
                "kelly_stake": "Mise Kelly", "placed_bookmaker": "Bookmaker",
            })
            cfg = {
                "Cote": st.column_config.NumberColumn(format="%.2f"),
                "Mise Kelly": st.column_config.NumberColumn(format="$%.2f"),
            }
            if "Proba modèle" in disp.columns:
                disp["Proba modèle"] = disp["Proba modèle"] * 100
                cfg["Proba modèle"] = st.column_config.NumberColumn(format="%.1f%%")
            if "Edge" in disp.columns:
                disp["Edge"] = disp["Edge"] * 100
                cfg["Edge"] = st.column_config.NumberColumn(format="%+.1f%%")
            st.dataframe(disp, width='stretch', hide_index=True, column_config=cfg)
    except Exception as exc:
        st.error(f"Erreur : {exc}")
