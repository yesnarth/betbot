"""Performance tabs — ROI/CLV metrics and bankroll management."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from betbot_dashboard.api_client import api_get, api_post
from betbot_dashboard.styles import empty_state


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


def render_capital_tab() -> None:
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

    if not bk_state:
        return

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
    c2[2].metric("Gains cumulés", f"{bk_state['total_won']:.2f} $",
                 help="Somme des stakes × cote des paris gagnants (avant déduction de la mise).")
    c2[3].metric("Pertes (mises sur paris perdus)",
                 f"{bk_state['total_lost_stakes']:.2f} $",
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

    # Deposit / withdraw
    st.divider()
    st.markdown("### Mouvements manuels")
    st.caption("Saisis un montant > 0 puis valide. Le solde est mis à jour immédiatement.")
    c3 = st.columns([1, 1, 2])
    with c3[0]:
        dep_amt = st.number_input("Montant dépôt ($)", min_value=0.0,
                                  value=0.0, step=10.0, format="%.2f", key="dep_amt")
        dep_note = st.text_input("Note dépôt", placeholder="ex : recharge mensuelle")
        if st.button("➕ Déposer", width='stretch', disabled=(dep_amt <= 0)):
            try:
                api_post("/bankroll/deposit",
                         json={"amount": dep_amt, "note": dep_note or None})
                st.success(f"+{dep_amt:.2f}$ déposés. Recharge la page.")
            except Exception as exc:
                st.error(f"Erreur : {exc}")
    with c3[1]:
        wd_amt = st.number_input("Montant retrait ($)", min_value=0.0,
                                 value=0.0, step=10.0, format="%.2f", key="wd_amt")
        wd_note = st.text_input("Note retrait", placeholder="ex : retrait gains")
        if st.button("➖ Retirer", width='stretch', disabled=(wd_amt <= 0)):
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
