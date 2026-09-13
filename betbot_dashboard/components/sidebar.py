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
    n_pending: int = 0,
) -> dict:
    """Render the left-hand sidebar and return the scan filter selections."""
    auto_confirm = bool(health.get("auto_confirm_picks"))
    with st.sidebar:
        # ─── Section 1 : Action — what to do NOW ─────────────────────────
        st.header("Action")
        if auto_confirm:
            # The validation queue is empty BY DESIGN in this mode, so the old
            # "Picks à valider: 0 — rien à faire" was a permanently dead KPI in
            # the most prominent slot. What the user tracks instead is how many
            # bets are still awaiting a result.
            if n_pending > 0:
                st.metric("Paris en attente", str(n_pending),
                          delta="résultat à venir", delta_color="off")
                st.caption("Onglet **🔔 Mes picks → En attente de résultat**.")
            elif n_pending == 0:
                st.metric("Paris en attente", "0", delta="tout est noté")
                st.caption("Lance un scan : **🛠️ Outils → 🎯 Scan manuel**.")
            else:
                st.metric("Paris en attente", "—", delta="API ?", delta_color="off")
        elif n_proposed > 0:
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
        delta_str = f"{balance - initial:+.0f} €" if initial > 0 else None
        st.metric("Solde courant", f"{balance:.0f} €", delta=delta_str)
        if balance != available:
            st.caption(f"Disponible : **{available:.0f} €** · "
                       f"Engagé : {balance - available:.0f} €")
        if auto_confirm:
            # Under manual variable staking the ledger only moves on an explicit
            # "J'ai placé", so this balance is NOT the real exposure. Saying so
            # here prevents it from being read as a P&L.
            st.caption(
                "⚠️ Mises réelles variables → ce solde ne suit pas tes paris. "
                "La performance se lit en **ROI à plat** dans 📊 Performance."
            )

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
            # Price the NEXT scan instead of comparing to a hardcoded 100.
            # With 46 leagues x eu,uk x h2h,totals a scan bills 184 credits:
            # "120 req · OK" was green while the next scan was already
            # unaffordable, and the user rotates keys by hand off this number.
            cost = int(health.get("odds_scan_cost", 0))
            n_scans = int(health.get("odds_scans_affordable", -1))
            if cost > 0 and n_scans >= 0:
                if n_scans == 0:
                    st.metric("Quota Odds API", f"{quota} req",
                              delta=f"⚠ 0 scan ({cost} req requis)",
                              delta_color="inverse")
                    st.caption(
                        f"**Rotation de clé nécessaire.** Un scan coûte {cost} "
                        f"requêtes ({len(health.get('active_sports', []))} ligues). "
                        "Ajoute une clé dans `ODDS_API_KEYS` (séparées par des "
                        "virgules) : le bot bascule tout seul sur la première "
                        "qui a le budget."
                    )
                else:
                    st.metric("Quota Odds API", f"{quota} req",
                              delta=f"{n_scans} scan(s) · {cost} req/scan",
                              delta_color="normal" if n_scans >= 3 else "off")
            else:
                delta_q = "OK" if quota >= 100 else f"min {quota_min}"
                st.metric("Quota Odds API", f"{quota} req", delta=delta_q,
                          delta_color="normal" if quota >= 100 else "off")

        # api-football sits next to the Odds quota on purpose: both are
        # recharged on the same monthly cycle, so both deadlines belong in one
        # glance. This one was invisible until its daily allowance ran dry and
        # every league silently refreshed to "0 équipe".
        af = health.get("apifootball") or {}
        af_state = af.get("state")
        if af_state == "ok":
            jours = af.get("days_left")
            used, limit = af.get("requests_used"), af.get("requests_limit")
            libelle = f"{used}/{limit} req" if limit else (af.get("plan") or "actif")
            if jours is not None and jours <= 3:
                st.metric("api-football", libelle,
                          delta=f"⚠ expire dans {jours} j", delta_color="inverse")
                st.caption("**Renouvelle ton abonnement api-football** — sans lui, "
                           "plus de statistiques d'équipe ni de xG, donc retour "
                           "au modèle consensus.")
            else:
                d = f"{jours} j restants" if jours is not None else (af.get("plan") or "")
                st.metric("api-football", libelle, delta=d, delta_color="off")
        elif af_state == "daily_limit_reached":
            st.metric("api-football", "quota du jour épuisé",
                      delta="repart au prochain cycle", delta_color="off")
            st.caption("L'abonnement reste valide. Les scans de pronostics ne "
                       "dépendent pas d'api-football — seuls le rafraîchissement "
                       "des stats et les xG attendent.")
        elif af_state == "inactive":
            st.metric("api-football", "abonnement inactif",
                      delta="⚠ à renouveler", delta_color="inverse")
        elif af_state == "unconfigured":
            st.metric("api-football", "non configuré", delta="clé absente",
                      delta_color="off")
        elif af_state:
            st.metric("api-football", "état inconnu", delta=af_state,
                      delta_color="off")

        st.metric("Équipes en DB", health["teams_in_db"])
        scan_hours = health.get('scan_hours') or []
        if scan_hours:
            st.caption(f"Scans auto : {' · '.join(scan_hours)}")
        else:
            st.caption("Scans : **manuel uniquement** "
                       "(lance via 🛠️ Outils → Scan manuel quand tu veux parier)")

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
        if st.button("🔄 Résoudre / noter les matchs terminés", width='stretch',
                     help="Note aussi les picks proposés dont le match est fini "
                          "(mesure du modèle, sans toucher à la bankroll)."):
            try:
                res = api_post("/predictions/resolve")
                st.success(f"Paris résolus : {res.get('resolved')} · "
                           f"picks notés : {res.get('proposed_graded', 0)} · "
                           f"en attente : {res.get('still_pending')}")
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
                    # Football — ligues d'été (couverture api-football :
                    # modèle blended complet, actives pendant la pause estivale
                    # européenne). Voir 🔌 Sources → Couverture données équipes.
                    "soccer_norway_eliteserien",
                    "soccer_sweden_allsvenskan",
                    "soccer_sweden_superettan",
                    "soccer_finland_veikkausliiga",
                    "soccer_brazil_campeonato",
                    "soccer_brazil_serie_b",
                    "soccer_usa_mls",
                    "soccer_japan_j_league",
                    "soccer_korea_kleague1",
                    "soccer_china_superleague",
                    "soccer_argentina_primera_division",
                    "soccer_chile_campeonato",
                    "soccer_league_of_ireland",
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

            # Sliders start AT the server discipline and cannot go below it.
            # They used to start at 0.40/-10%/1.0 and always sent an explicit
            # value, silently bypassing the env floors on every dashboard scan
            # — while the worker's auto-scan respected them. The API now clamps
            # (tighten-only) regardless, so these bounds are honesty in the UI,
            # not the enforcement itself.
            floor_edge = float(health.get("min_value_edge", 0.04)) * 100
            floor_prob = float(health.get("min_model_prob", 0.40))
            floor_odds = float(health.get("min_book_odds", 1.50))
            ceil_odds = float(health.get("max_book_odds") or 0) or 5.0
            min_edge_pct = st.slider(
                "Edge minimum (%)", floor_edge, 20.0, floor_edge, 0.5,
                help=f"Plancher serveur : {floor_edge:.1f} %. Tu peux exiger plus, jamais moins.")
            min_prob = st.slider(
                "Probabilité modèle minimale", floor_prob, 0.90, floor_prob, 0.05,
                help=f"Plancher serveur : {floor_prob:.2f} — la zone où le modèle est calibré "
                     "(77 % de réussite mesurée). Tu peux exiger plus, jamais moins.")
            min_odds = st.slider(
                "Cote minimum", floor_odds, ceil_odds, floor_odds, 0.05,
                help=f"Bornes serveur : {floor_odds:.2f} à {ceil_odds:.2f}. Au-delà du "
                     "plafond, le serveur écarte le pick de toute façon.")
            n_legs = st.slider("Jambes par combiné", 1, 6, 3)
            n_combos = st.slider("Combinés à générer", 1, 10, 3)

            # Same-hour batch workflow: one slot, every match kicks off together,
            # every bet resolves together. Applies to every sandbox scan mode.
            slot_labels = ["Tous les créneaux"] + [f"{h:02d}:00 – {h:02d}:59" for h in range(10, 24)]
            slot_choice = st.selectbox(
                "Créneau coup d'envoi (heure de Paris)", slot_labels, index=0,
                help="Ne garde que les matchs démarrant dans ce créneau d'une heure. "
                     "L'onglet « Matchs disponibles » liste les coups d'envoi du jour.")
            kickoff_hour = None if slot_choice == slot_labels[0] else int(slot_choice[:2])

    return {
        "sport": sport,
        "today_only": today_only,
        "min_edge_pct": min_edge_pct,
        "min_prob": min_prob,
        "min_odds": min_odds,
        "n_legs": n_legs,
        "n_combos": n_combos,
        "kickoff_hour": kickoff_hour,
    }
