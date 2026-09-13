"""Performance tabs — ROI/CLV metrics and bankroll management."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from betbot_dashboard.api_client import api_get, api_post
from betbot_dashboard.styles import empty_state


def _verdict_banner(brier: dict) -> None:
    """The single hardest test: does the model beat the raw bookmaker price?

    `1/best_odds` still contains the bookmaker's margin, so a model that cannot
    beat it is not merely failing to beat the market — it is removing
    information relative to just reading the odds. Showing this first stops the
    ROI from being read in isolation.
    """
    if not brier or not brier.get("n"):
        return
    model, market = brier["model"], brier["market"]
    n = brier["n"]
    if brier.get("model_beats_market"):
        st.success(
            f"✅ **Le modèle bat la cote brute du bookmaker.** "
            f"Brier modèle {model:.4f} contre {market:.4f} pour la cote "
            f"(marge incluse), sur {n} paris tranchés. Plus bas = meilleur."
        )
    else:
        st.error(
            f"🔴 **Le modèle est battu par la cote brute du bookmaker.** "
            f"Brier modèle {model:.4f} contre {market:.4f} pour la cote — "
            f"et cette cote contient encore la marge du book. Sur {n} paris "
            f"tranchés, le modèle n'apporte aucune information au-delà de la "
            f"simple lecture des cotes. C'est le test le plus sévère qui existe."
        )


def _perf_table(rows: list[dict], key: str, label: str) -> None:
    """Worst-ROI-first table for one grouping dimension."""
    if not rows:
        return
    df = pd.DataFrame(rows)
    df = df.rename(columns={
        key: label, "n": "n", "wins": "V", "losses": "D",
        "win_rate": "% réussite", "roi_pct": "ROI %",
        "avg_model_prob": "proba prédite", "avg_implied_prob": "proba marché",
    })
    cols = [label, "n", "V", "D", "% réussite", "ROI %", "proba prédite", "proba marché"]
    st.dataframe(
        df[[c for c in cols if c in df.columns]],
        hide_index=True,
        use_container_width=True,
    )


def render_roi_tab() -> None:
    st.subheader("Performance globale")
    period = st.selectbox("Période (jours)", [7, 14, 30, 60, 90, 180, 365], index=2)
    try:
        s = api_get("/stats/roi", days=period)
    except Exception as exc:
        st.error(f"Erreur : {exc}")
        s = None

    if s is None:
        return
    if s["n_bets"] == 0:
        empty_state(
            "📊",
            f"Aucun pari résolu sur les {period} derniers jours",
            "Les métriques (ROI, hit rate, CLV) s'afficheront automatiquement dès "
            "qu'un pari sera résolu. Le worker le fait à 04h, ou clique "
            "« Résoudre les paris terminés » dans la sidebar.",
        )
        return

    cols = st.columns(4)
    cols[0].metric("Paris tranchés", s["n_bets"])
    cols[1].metric("Victoires", f"{s['n_wins']} ({s['hit_rate']}%)")
    cols[2].metric("ROI", f"{s['roi']:+.1f}%")
    cols[3].metric("Edge moyen", f"{s['avg_edge']:+.2f}%")

    n_void = s.get("n_void", 0)
    if n_void:
        st.caption(
            f"➕ {n_void} pari(s) annulé(s) (remboursés) — exclus du ROI et du "
            "taux de réussite, comme il se doit : un remboursement n'est ni un "
            "gain ni une perte."
        )

    # ---- Diagnostic ------------------------------------------------------
    st.divider()
    st.markdown("### 🔬 Diagnostic du modèle")
    try:
        perf = api_get("/stats/model-performance", days=period, only_placed=False)
    except Exception as exc:  # noqa: BLE001 — diagnostic must never break the tab
        st.caption(f"Diagnostic indisponible : {exc}")
        perf = None

    if perf:
        cov = perf.get("coverage") or {}
        if cov.get("total"):
            pct = cov["pct"]
            msg = (f"Couverture de notation : **{cov['graded']}/{cov['total']} "
                   f"({pct:.1f} %)** des pronostics ont un résultat.")
            if pct < 80:
                st.warning(
                    msg + " En dessous de 80 %, lis le ROI ci-dessus avec "
                    "prudence : il ne porte pas sur toute la production."
                )
            else:
                st.caption(msg)

        _verdict_banner(perf.get("brier") or {})

        by_market = perf.get("by_market") or []
        if by_market:
            st.markdown("#### Par marché — les pires en premier")
            st.caption(
                "C'est ici que se trouve l'argent perdu. Un marché dont la "
                "proba prédite dépasse largement le taux de réussite réel est "
                "un marché où le modèle est surconfiant."
            )
            _perf_table(by_market, "market", "Marché")

        by_model = perf.get("by_model") or []
        if len(by_model) > 1:
            st.markdown("#### Par type de modèle")
            st.caption(
                "`blended` = Dixon-Coles + xG + ELO avec statistiques d'équipe. "
                "`consensus` = repli quand la ligue n'a aucune statistique en base."
            )
            _perf_table(by_model, "model_type", "Modèle")

        calib = perf.get("calibration") or []
        if calib:
            st.markdown("#### Calibration — les probabilités sont-elles honnêtes ?")
            st.caption(
                "Un modèle honnête qui annonce 60 % gagne ≈ 60 % du temps. "
                "L'écart est la surconfiance, en points."
            )
            cdf = pd.DataFrame(calib).rename(columns={
                "bucket": "Tranche", "n": "n",
                "expected_win_rate": "Annoncé %", "actual_win_rate": "Réalisé %",
                "gap": "Surconfiance (pts)",
            })
            # Column order follows how the sentence is read — "le modèle annonce
            # X et réalise Y" — instead of the dict insertion order, which put
            # Réalisé before Annoncé and forced the eye to jump backwards.
            cdf = cdf[["Tranche", "n", "Annoncé %", "Réalisé %", "Surconfiance (pts)"]]
            st.dataframe(cdf, hide_index=True, use_container_width=True)

    st.divider()
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
        st.markdown("### Closing Line Value (CLV) — inactif")
        st.warning(
            "**Aucune cote de clôture n'est enregistrée.** Le CLV est la seule "
            "mesure non circulaire d'un avantage réel : il compare ta cote à "
            "celle du marché à la fermeture, et se prononce sur ~200-400 paris "
            "là où le ROI en demande plusieurs milliers.\n\n"
            "Tant qu'il reste à zéro, il est impossible de distinguer la chance "
            "de la compétence. Le snapshot se pilote par `CLV_SNAPSHOT_ENABLED=1` "
            "dans le `.env` — il consomme du quota Odds API, d'où sa "
            "désactivation par défaut."
        )

    # CLV data-quality view — distinguishes 'pending snap window' from
    # 'permanently missed' so the user can see when there's a real gap
    # rather than a silent NaN
    try:
        cov = api_get("/stats/clv-coverage", days=period)
    except Exception:
        cov = None
    if cov and cov.get("n_total_confirmed", 0) > 0:
        st.divider()
        st.markdown("#### Couverture CLV")
        ccols = st.columns(4)
        coverage = cov["coverage_pct"]
        coverage_color = "normal" if coverage >= 80 else ("off" if coverage >= 50 else "inverse")
        ccols[0].metric("Paris confirmés", cov["n_total_confirmed"])
        ccols[1].metric(
            "Couverture", f"{coverage:.0f}%",
            delta=("OK" if coverage >= 80
                   else "faible" if coverage >= 50
                   else "trous"),
            delta_color=coverage_color,
        )
        ccols[2].metric("En attente snap", cov["n_pending_clv"],
                        help="Match à venir ou tout juste démarré — snap "
                             "toujours possible.")
        ccols[3].metric(
            "Manqués",
            cov["n_missed_clv"],
            delta="OK" if cov["n_missed_clv"] == 0 else None,
            delta_color="normal" if cov["n_missed_clv"] == 0 else "inverse",
            help="Confirmé depuis > 7 jours, jamais snappé. "
                 "Odds API indisponible au moment du kickoff.",
        )
        if cov["n_missed_clv"] > 0:
            st.caption(
                f"⚠ {cov['n_missed_clv']} pari(s) sans closing odds — "
                f"vérifier la santé d'Odds API dans Système → Sources."
            )

    # ── CLV par segment : quelles ligues/marchés battent la clôture ──────
    st.divider()
    st.markdown("### CLV par segment (ligue × marché)")
    st.caption(
        "Le **vrai** juge de paix : un CLV moyen **positif** = le modèle bat la cote "
        "de clôture sur ce segment (à privilégier) ; **négatif** = à éviter. "
        "Significatif surtout avec assez de paris (colonne *Paris*)."
    )
    try:
        seg = api_get("/stats/clv-by-segment", days=max(period, 90))
    except Exception:
        seg = None
    segments = (seg or {}).get("segments", [])
    if not segments:
        empty_state(
            "📈", "Pas encore de CLV par segment",
            "Les snapshots de clôture s'accumulent (toutes les 10 min sur les matchs "
            "qui démarrent). Reviens après quelques paris confirmés et résolus.",
        )
    else:
        sdf = pd.DataFrame(segments)
        sdf["Segment"] = sdf["sport_key"] + " · " + sdf["market"]
        disp = sdf[["Segment", "n_with_clv", "avg_clv_pct", "positive_clv_share"]].rename(
            columns={"n_with_clv": "Paris", "avg_clv_pct": "CLV moyen",
                     "positive_clv_share": "% CLV>0"})
        st.dataframe(
            disp, width='stretch', hide_index=True,
            column_config={
                "CLV moyen": st.column_config.NumberColumn(format="%+.2f%%"),
                "% CLV>0": st.column_config.NumberColumn(format="%.0f%%"),
            },
        )

    # ── Performance modèle : ROI/réussite « would-have » sur TOUS les picks ──
    st.divider()
    st.markdown("### 🧪 Performance modèle (tous les picks historisés)")
    st.caption(
        "Le **vrai juge de paix** : ROI et réussite à **mise plate (1u)** sur **TOUS** "
        "les picks historisés (proposés + confirmés + passés), pas seulement ceux que "
        "tu as joués. Ça mesure le MODÈLE — indépendamment de tes mises (le bankroll "
        "réel est dans l'onglet 💰). Échantillon par segment = petit au début."
    )
    try:
        mp = api_get("/stats/model-performance", days=max(period, 90))
    except Exception as exc:
        mp = None
        st.caption(f"_Indisponible : {exc}_")
    ov = (mp or {}).get("overall", {})
    if not mp or ov.get("n", 0) == 0:
        empty_state(
            "🧪", "Pas encore de picks historisés résolus",
            "Désormais chaque scan (manuel / agent / live) enregistre ses picks "
            "automatiquement. Reviens dans quelques jours : dès que des matchs se "
            "terminent, le ROI et la calibration s'afficheront ici.",
        )
    else:
        mcols = st.columns(4)
        mcols[0].metric("Picks résolus", ov["n"])
        mcols[1].metric("Réussite", f"{ov['win_rate']:.1f}%")
        roi_v = ov["roi_pct"]
        mcols[2].metric("ROI (mise plate)", f"{roi_v:+.1f}%",
                        delta=("positif" if roi_v > 0 else "négatif" if roi_v < 0 else None),
                        delta_color=("normal" if roi_v >= 0 else "inverse"))
        mcols[3].metric("Proba moy / implicite",
                        f"{ov['avg_model_prob']:.2f} / {ov['avg_implied_prob']:.2f}",
                        help="Proba moyenne du modèle vs proba implicite du marché. "
                             "Modèle > marché = le modèle voit de la valeur (souvent des outsiders).")

        segs = mp.get("segments", [])
        if segs:
            st.markdown("**Par segment (ligue × marché)** — trié par ROI décroissant")
            sdf = pd.DataFrame(segs)
            sdf["Segment"] = sdf["sport_key"] + " · " + sdf["market"]
            disp = sdf[["Segment", "n", "win_rate", "roi_pct",
                        "avg_model_prob", "avg_implied_prob"]].rename(
                columns={"n": "Picks", "win_rate": "Réussite", "roi_pct": "ROI",
                         "avg_model_prob": "p modèle", "avg_implied_prob": "p marché"})
            st.dataframe(
                disp, width='stretch', hide_index=True,
                column_config={
                    "Réussite": st.column_config.NumberColumn(format="%.1f%%"),
                    "ROI": st.column_config.NumberColumn(format="%+.1f%%"),
                },
            )

        calib = mp.get("calibration", [])
        if calib:
            st.markdown("**Calibration** — un modèle honnête gagne ≈ le milieu de chaque tranche")
            cdf = pd.DataFrame(calib).rename(columns={
                "bucket": "Proba modèle", "n": "Picks",
                "actual_win_rate": "Réussite réelle", "expected_win_rate": "Attendu"})
            st.dataframe(
                cdf[["Proba modèle", "Picks", "Réussite réelle", "Attendu"]],
                width='stretch', hide_index=True,
                column_config={
                    "Réussite réelle": st.column_config.NumberColumn(format="%.1f%%"),
                    "Attendu": st.column_config.NumberColumn(format="%.1f%%"),
                },
            )
        st.caption(
            "⚠️ « Would-have » à mise plate : mesure le MODÈLE, pas ton bankroll. "
            "Sur petit échantillon, un ROI extrême (±) est surtout du bruit — "
            "ça se stabilise avec le nombre de picks résolus."
        )



def _render_frozen_capital(bk_state: dict) -> None:
    """Capital tiles that describe reality when the ledger no longer moves.

    Replaces "Engagé sur paris 0,00 €" (while bets are open) and "P&L cumulé
    +0,00 €" (while the flat ROI is negative) with the two numbers the system
    can actually stand behind: how many bets are still running, and the flat
    P&L over graded picks.
    """
    try:
        pending = api_get("/predictions/pending")
    except Exception:
        pending = []
    try:
        # 365 is the endpoint's hard ceiling (Query(..., le=365)); asking for
        # more returns a 422 and the tile silently fell back to "—".
        perf = api_get("/stats/model-performance", days=365, only_placed=False)
        overall = perf.get("overall") or {}
    except Exception:
        overall = {}

    n_open = len(pending)
    stake_theo = sum(float(p.get("kelly_stake") or 0.0) for p in pending)
    n_graded = int(overall.get("n") or 0)
    roi = float(overall.get("roi_pct") or 0.0)
    pnl_u = roi / 100.0 * n_graded if n_graded else 0.0

    c = st.columns(4)
    c[0].metric("Capital de référence", f"{bk_state['balance']:.2f} €",
                help="Sert au dimensionnement Kelly. Ne suit pas tes mises réelles.")
    c[1].metric("Paris en cours", str(n_open),
                help="Picks confirmés dont le résultat n'est pas encore connu.")
    c[2].metric("Exposition théorique", f"{stake_theo:.2f} €",
                help="Somme des mises Kelly suggérées sur les paris en cours. "
                     "Tes mises réelles ne sont pas connues du système.")
    if n_graded:
        # Delta text must LEAD with the signed number: Streamlit parses the
        # first token to pick the arrow direction, and "ROI -15.0 %" made it
        # render an UP arrow on a loss.
        c[3].metric("P&L modèle (à plat)", f"{pnl_u:+.1f} u",
                    delta=f"{roi:+.1f} % de ROI à plat",
                    delta_color="normal",
                    help="Mise plate de 1 unité par pari, sur les picks notés, "
                         "annulations comptées 0. Différent du ROI de l'onglet "
                         "Performance, qui est pondéré par la mise Kelly et "
                         "exclut les annulations.")
        st.caption(
            f"**{pnl_u:+.1f} u** sur **{n_graded}** paris notés — soit un "
            f"**ROI à plat de {roi:+.1f} %**. 1 u = 1 pari, tes mises réelles "
            "sont variables. ⚠️ Ce chiffre n'est pas celui de 📊 Performance : "
            "là-bas le ROI est pondéré par la mise Kelly et exclut les paris "
            "annulés."
        )
    else:
        c[3].metric("P&L modèle", "—", help="Aucun pari noté sur la période.")


def render_capital_tab(health: dict | None = None) -> None:
    st.subheader("💰 Gestion du capital")
    frozen = bool((health or {}).get("auto_confirm_picks"))

    if frozen:
        # Every figure below comes from `bankroll_ledger`, which in this mode
        # holds exactly one row: the initial deposit. `committed` joins on
        # kind='bet_placed' — rows that no longer exist — so it reads 0.00
        # while real bets are open at the bookmaker. `pnl` is
        # balance − deposits + withdrawals, structurally 0.00 while the flat
        # ROI is negative. Displaying those as money facts was the single most
        # dangerous statement in the dashboard: it says "nothing is out, you
        # are losing nothing" to someone with open positions.
        st.warning(
            "**Livre comptable GELÉ.** Tes mises sont placées à la main sur "
            "Betclic avec des montants variables : rien ici n'est débité "
            "ni crédité automatiquement. Ce solde est un **capital de "
            "référence**, pas ton argent réel.\n\n"
            "La performance se lit en **ROI à plat** dans 📊 **Performance**."
        )
    else:
        st.caption(
            "Toutes les mises consomment réellement le solde, tous les "
            "gains/pertes le mettent à jour automatiquement. Source : la table "
            "`bankroll_ledger`."
        )

    try:
        bk_state = api_get("/bankroll/state")
    except Exception as exc:
        st.error(f"Erreur : {exc}")
        bk_state = None

    if not bk_state:
        return

    if frozen:
        _render_frozen_capital(bk_state)
    else:
        c = st.columns(4)
        c[0].metric("Solde courant", f"{bk_state['balance']:.2f} €")
        c[1].metric("Capital libre", f"{bk_state['available']:.2f} €")
        c[2].metric("Engagé sur paris", f"{bk_state['committed']:.2f} €")
        pnl = bk_state['pnl']
        c[3].metric("P&L cumulé", f"{pnl:+.2f} €",
                    delta=f"{pnl:+.2f} €" if pnl != 0 else None)

    st.divider()
    c2 = st.columns(4)
    c2[0].metric("Dépôts cumulés", f"{bk_state['total_deposits']:.2f} €")
    c2[1].metric("Retraits cumulés", f"{bk_state['total_withdrawals']:.2f} €")
    c2[2].metric("Gains cumulés", f"{bk_state['total_won']:.2f} €",
                 help="Somme des stakes × cote des paris gagnants (avant déduction de la mise).")
    c2[3].metric("Pertes (mises sur paris perdus)",
                 f"{bk_state['total_lost_stakes']:.2f} €",
                 help="Somme des stakes engagés sur les paris perdants.")

    # Evolution chart — guard against empty / single-point datasets to
    # avoid Vega-Lite "Infinite extent" warnings flooding the console.
    try:
        evo = api_get("/bankroll/evolution", days=60)
    except Exception as exc:
        evo = None
        st.caption(f"_Courbe d'évolution indisponible : {exc}_")
    st.markdown("### Évolution du solde (60 derniers jours)")
    if not evo:
        empty_state("💰", "Aucun mouvement sur la période",
                    "La courbe apparaîtra dès le premier dépôt ou pari.")
    elif len(evo) < 2:
        empty_state("📈", f"Un seul point de données ({len(evo)})",
                    "La courbe s'affichera dès le 2e mouvement de bankroll.")
    else:
        df = pd.DataFrame(evo)
        df["ts"] = pd.to_datetime(df["ts"], errors="coerce")
        df = df.dropna(subset=["ts"]).set_index("ts").sort_index()
        # Vega-Lite emits "Infinite extent" warnings on dataframes < 2 rows
        # or with all-NaN columns. Filter both before calling line_chart.
        if len(df) < 2 or df["balance"].isna().all():
            empty_state("📈", "Pas assez de points pour tracer la courbe",
                        "La courbe s'affichera dès le 2e mouvement valide.")
        else:
            st.line_chart(df["balance"], height=260)

    # Deposit / withdraw — idempotency-protected
    st.divider()
    st.markdown("### Mouvements manuels")
    st.caption(
        "Saisis un montant > 0 puis valide. Un double-clic ou retry après glitch "
        "réseau est neutralisé par une clé d'idempotency dérivée du formulaire — "
        "le serveur replay sans rejouer la mutation. Pour redéposer le MÊME "
        "montant dans la même session, recharge la page."
    )
    # Deterministic idempotency key per (session, endpoint, amount, note). Two
    # clicks with the same form values reuse the same key → server replays.
    # Page refresh = new salt = new key, so the user can legitimately re-do
    # the same deposit in a later session.
    import hashlib
    import uuid
    if "idem_salt" not in st.session_state:
        st.session_state.idem_salt = uuid.uuid4().hex

    def _idem_key(endpoint: str, amount: float, note: str | None) -> str:
        material = f"{st.session_state.idem_salt}:{endpoint}:{amount:.2f}:{note or ''}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:64]

    c3 = st.columns([1, 1, 2])
    with c3[0]:
        dep_amt = st.number_input("Montant dépôt (€)", min_value=0.0,
                                  value=0.0, step=10.0, format="%.2f", key="dep_amt")
        dep_note = st.text_input("Note dépôt", placeholder="ex : recharge mensuelle")
        if st.button("➕ Déposer", width='stretch', disabled=(dep_amt <= 0)):
            try:
                api_post(
                    "/bankroll/deposit",
                    json={"amount": dep_amt, "note": dep_note or None},
                    headers={"Idempotency-Key": _idem_key(
                        "bankroll/deposit", dep_amt, dep_note)},
                )
                st.toast(f"+{dep_amt:.2f} € déposés.", icon="➕")
                st.rerun()
            except Exception as exc:
                st.error(f"Erreur : {exc}")
    with c3[1]:
        wd_amt = st.number_input("Montant retrait (€)", min_value=0.0,
                                 value=0.0, step=10.0, format="%.2f", key="wd_amt")
        wd_note = st.text_input("Note retrait", placeholder="ex : retrait gains")
        if st.button("➖ Retirer", width='stretch', disabled=(wd_amt <= 0)):
            try:
                api_post(
                    "/bankroll/withdraw",
                    json={"amount": wd_amt, "note": wd_note or None},
                    headers={"Idempotency-Key": _idem_key(
                        "bankroll/withdraw", wd_amt, wd_note)},
                )
                st.toast(f"-{wd_amt:.2f} € retirés.", icon="➖")
                st.rerun()
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
