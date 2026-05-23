"""Backtest tab — historical validation of the prediction model.

Runs a walk-forward backtest on a chosen football league and displays
Brier score, log-loss, and calibration buckets (predicted vs actual hit
rate per decile). Lets the user answer "is the model actually any good?"
BEFORE looking at live ROI.
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from betbot_dashboard.api_client import api_post
from betbot_dashboard.styles import empty_state


SUPPORTED_LEAGUES = {
    "soccer_epl":                    "⚽ Premier League",
    "soccer_spain_la_liga":          "⚽ La Liga",
    "soccer_germany_bundesliga":     "⚽ Bundesliga",
    "soccer_italy_serie_a":          "⚽ Serie A",
    "soccer_france_ligue1":          "⚽ Ligue 1",
    "soccer_uefa_champs_league":     "⚽ Champions League",
    "soccer_efl_champ":              "⚽ Championship 🇬🇧",
    "soccer_netherlands_eredivisie": "⚽ Eredivisie 🇳🇱",
    "soccer_portugal_primeira_liga": "⚽ Primeira Liga 🇵🇹",
}


def _brier_quality_label(brier: float) -> tuple[str, str]:
    """Map Brier score to (label, color). Lower is better.
    Baselines:
      - 0.667 = uniform 1/3 each (no signal)
      - ~0.55 = poor
      - ~0.50 = decent
      - ~0.45 = strong
      - 0.0   = perfect
    """
    if brier < 0.45:
        return ("Excellent", "normal")
    if brier < 0.50:
        return ("Bon", "normal")
    if brier < 0.58:
        return ("Acceptable", "off")
    return ("Faible", "inverse")


def render_backtest_tab() -> None:
    st.subheader("🧪 Backtest historique — qualité du modèle")
    st.caption(
        "Walk-forward strict : pour chaque match testé, le modèle est ré-entraîné "
        "UNIQUEMENT sur les matchs antérieurs. Mesure 3 choses :"
    )
    st.markdown(
        "- **Brier score** : erreur quadratique sur les 3 issues. "
        "0 = parfait · 0,55 = moyen · 0,667 = aucun signal (uniforme 1/3).\n"
        "- **Log-loss** : pénalise les prédictions confiantes mais fausses.\n"
        "- **Calibration** : sur les paris où le modèle dit 60-70%, observe-t-on "
        "vraiment ~65% de réussites ?"
    )

    c1, c2, c3 = st.columns([2, 1, 1])
    sport_label = c1.selectbox(
        "Ligue",
        options=list(SUPPORTED_LEAGUES.values()),
        index=0,
    )
    sport_key = next(k for k, v in SUPPORTED_LEAGUES.items() if v == sport_label)
    n_holdout = c2.slider("Matchs testés", min_value=20, max_value=300, value=100, step=20)
    use_enrichment = c3.checkbox(
        "Avec ELO/xG",
        value=False,
        help="Snapshote ELO/xG actuels — donne une borne SUPÉRIEURE optimiste "
             "(introduit du look-ahead). Garde décoché pour une mesure honnête.",
    )

    if st.button("🧪 Lancer le backtest", type="primary", width='stretch'):
        with st.spinner(
            f"Walk-forward sur {n_holdout} matchs de {sport_label}… (~10-30 s)"
        ):
            try:
                res = api_post("/stats/backtest", json={
                    "sport_key": sport_key,
                    "n_holdout": n_holdout,
                    "use_enrichment": use_enrichment,
                })
            except Exception as exc:
                st.error(f"Erreur : {exc}")
                res = None
        if res:
            st.session_state.last_backtest = res

    res = st.session_state.get("last_backtest")
    if not res:
        empty_state(
            "🧪",
            "Aucun backtest lancé pour le moment",
            "Clique sur « Lancer le backtest » pour mesurer la qualité du modèle "
            "sur les matchs récents de la ligue choisie.",
        )
        return

    if res["n_matches"] == 0:
        empty_state(
            "📭",
            "Backtest impossible",
            res.get("notes") or "Pas assez d'historique pour cette ligue.",
        )
        return

    # Top KPIs
    quality_label, quality_color = _brier_quality_label(res["brier_score"])
    cols = st.columns(4)
    cols[0].metric("Matchs scorés", res["n_matches"])
    cols[1].metric(
        "Brier score",
        f"{res['brier_score']:.4f}",
        delta=quality_label,
        delta_color=quality_color,
        help="Plus bas = mieux. 0,667 = baseline (1/3 chaque issue), 0 = parfait.",
    )
    cols[2].metric(
        "Log-loss",
        f"{res['log_loss']:.4f}",
        help="Plus bas = mieux. Pénalise sévèrement les prédictions confiantes fausses.",
    )
    cols[3].metric("Durée", f"{res['duration_seconds']:.1f}s")

    # Reference points
    st.caption(f"📊 {res.get('notes', '')}")

    # Calibration buckets — the most actionable view
    st.markdown("### Calibration : prédit vs observé")
    st.caption(
        "Chaque ligne = un décile de probabilité prédite. Si la colonne **Observé** "
        "est proche de **Prédit**, le modèle est bien calibré (= ses 65% sont vraiment 65%)."
    )
    calib = res.get("calibration", [])
    if not calib:
        st.info("Pas assez d'échantillons pour découper en déciles.")
        return

    df = pd.DataFrame(calib)
    df = df.rename(columns={
        "range": "Décile",
        "n_samples": "Échantillons",
        "predicted_avg": "Prédit (moy)",
        "actual_avg": "Observé (taux réel)",
        "abs_error": "|Écart|",
    })
    # Convert to percentages for readability
    for col in ("Prédit (moy)", "Observé (taux réel)", "|Écart|"):
        if col in df.columns:
            df[col] = df[col] * 100

    cfg = {
        "Prédit (moy)":       st.column_config.NumberColumn(format="%.1f%%"),
        "Observé (taux réel)": st.column_config.NumberColumn(format="%.1f%%"),
        "|Écart|":            st.column_config.NumberColumn(format="%.1f pts"),
    }
    st.dataframe(df, width='stretch', hide_index=True, column_config=cfg)

    # Visual chart : prédit vs observé sur un même axe
    if len(df) >= 2:
        st.markdown("### Diagramme de fiabilité")
        st.caption(
            "Idéalement la courbe **Observé** suit la diagonale (= courbe **Prédit**). "
            "Un écart systématique au-dessus = modèle sous-confiant ; en-dessous = sur-confiant."
        )
        chart_df = df[["Décile", "Prédit (moy)", "Observé (taux réel)"]].set_index("Décile")
        st.line_chart(chart_df, height=280)

    st.divider()
    st.caption(
        "💡 **Comment interpréter** : Brier < 0,50 et |écart| moyen < 5 pts sur tous "
        "les déciles → le modèle est utilisable. Si certains déciles ont un écart > 10 pts, "
        "c'est là qu'il faut concentrer l'effort d'amélioration (calibrateur isotonique, "
        "ré-entraînement Poisson, etc.)."
    )
