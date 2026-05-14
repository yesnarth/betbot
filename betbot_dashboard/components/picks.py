"""Pure render helpers for value picks and parlays."""
from __future__ import annotations

import pandas as pd
import streamlit as st


def _reliability_emoji(score: float) -> str:
    """3-bucket visual cue for the Fiabilité column."""
    if score >= 0.70:
        return "🟢"
    if score >= 0.40:
        return "🟡"
    return "🔴"


def render_picks_table(picks: list[dict]) -> None:
    if not picks:
        return
    df = pd.DataFrame(picks)
    # Build a display copy of the reliability column with the emoji prefix.
    # The raw `reliability` column stays in `df` for the warning below.
    if "reliability" in df.columns:
        df["reliability_display"] = df["reliability"].apply(
            lambda s: f"{_reliability_emoji(float(s))} {float(s):.2f}"
        )
    cols = [c for c in [
        "home_team", "away_team", "league", "selection_label",
        "best_odds", "model_prob", "value_edge", "kelly_stake",
        "reliability_display", "best_book", "model_type",
    ] if c in df.columns]
    display = df[cols].copy()
    rename = {
        "home_team": "Domicile", "away_team": "Extérieur", "league": "Ligue",
        "selection_label": "Pari", "best_odds": "Cote", "model_prob": "Proba modèle",
        "value_edge": "Edge", "kelly_stake": "Mise Kelly",
        "reliability_display": "Fiabilité",
        "best_book": "Bookmaker", "model_type": "Modèle",
    }
    display = display.rename(columns=rename)
    column_config = {}
    if "Cote" in display.columns:
        column_config["Cote"] = st.column_config.NumberColumn(format="%.2f")
    if "Proba modèle" in display.columns:
        column_config["Proba modèle"] = st.column_config.NumberColumn(format="%.1f%%")
        display["Proba modèle"] = display["Proba modèle"] * 100
    if "Edge" in display.columns:
        column_config["Edge"] = st.column_config.NumberColumn(format="%+.1f%%")
        display["Edge"] = display["Edge"] * 100
    if "Mise Kelly" in display.columns:
        column_config["Mise Kelly"] = st.column_config.NumberColumn(format="$%.2f")
    if "Fiabilité" in display.columns:
        column_config["Fiabilité"] = st.column_config.TextColumn(
            help="🟢 ≥0.70 = haute · 🟡 0.40–0.69 = moyenne · 🔴 <0.40 = faible. "
                 "Combine taille d'échantillon, magnitude de l'edge, et "
                 "probabilité extrême — une fiabilité faible suggère un "
                 "artefact de modèle, pas une vraie valeur."
        )
    st.dataframe(display, width='stretch', hide_index=True, column_config=column_config)

    # Caveat on suspiciously large edges AND on low-reliability picks
    if "value_edge" in df.columns:
        big_edges = (df["value_edge"] > 0.20).sum()
        if big_edges > 0:
            st.warning(
                f"⚠️ **{big_edges} pari(s) ont un edge > 20%.** Le marché des cotes est "
                "généralement bien calibré ; un edge aussi élevé révèle souvent un "
                "**défaut de modèle**. Privilégie les paris à edge **2-10%** où la valeur est plus fiable."
            )
    if "reliability" in df.columns:
        low_rel = (df["reliability"] < 0.40).sum()
        if low_rel > 0:
            st.warning(
                f"🔴 **{low_rel} pari(s) ont une fiabilité < 0.40.** Soit la taille "
                "d'échantillon (n_matches) est trop faible, soit l'edge ou la "
                "probabilité tombent dans des zones où le modèle est historiquement "
                "moins précis. Considère une mise réduite ou skip."
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
                disp = df[show].rename(columns={
                    "home_team": "Domicile", "away_team": "Extérieur",
                    "selection_label": "Pari", "best_odds": "Cote",
                    "model_prob": "Proba modèle", "value_edge": "Edge",
                })
                cfg = {}
                if "Cote" in disp.columns:
                    cfg["Cote"] = st.column_config.NumberColumn(format="%.2f")
                if "Proba modèle" in disp.columns:
                    cfg["Proba modèle"] = st.column_config.NumberColumn(format="%.1f%%")
                    disp["Proba modèle"] = disp["Proba modèle"] * 100
                if "Edge" in disp.columns:
                    cfg["Edge"] = st.column_config.NumberColumn(format="%+.1f%%")
                    disp["Edge"] = disp["Edge"] * 100
                st.dataframe(disp, width='stretch', hide_index=True, column_config=cfg)
