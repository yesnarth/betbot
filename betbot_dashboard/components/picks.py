"""Pure render helpers for value picks and parlays."""
from __future__ import annotations

import pandas as pd
import streamlit as st


def _reliability_badge(score: float) -> str:
    """Emoji + WORD label for the Fiabilité column. The word matters for
    accessibility — a colour-blind user can't distinguish 🟢/🟡/🔴 alone."""
    if score >= 0.70:
        return "🟢 Haute"
    if score >= 0.40:
        return "🟡 Moyenne"
    return "🔴 Faible"


def _pick_rank_key(p: dict) -> tuple:
    return (float(p.get("value_edge") or 0), float(p.get("model_prob") or 0))


def _pick_prob_key(p: dict) -> tuple:
    return (float(p.get("model_prob") or 0), float(p.get("value_edge") or 0))


def is_early_resolving(selection_code: str | None) -> bool:
    """Bets winnable BEFORE full-time: any Over goal-line (O05/O15/O25/O35 — won
    the moment enough goals are scored) or BTTS-Yes (won once both teams score)."""
    c = (selection_code or "").upper()
    return (c.startswith("O") and c[1:].isdigit()) or c == "BTTSY"


def _match_key(p: dict) -> str:
    return p.get("event_id") or f"{p.get('home_team')}|{p.get('away_team')}|{p.get('league')}"


def group_picks_by_match(picks: list[dict], rank_by: str = "edge") -> tuple[list[dict], dict]:
    """Collapse correlated same-match picks. 1X2 / totals / Double Chance / Draw
    No Bet on ONE fixture are alternative expressions of the same view, NOT
    independent bets. Returns (primary, alternatives): `primary` = the single best
    pick per match (best edge, then prob), sorted best-first; `alternatives` =
    {match_key: [the other markets on that match]}."""
    groups: dict[str, list] = {}
    order: list[str] = []
    for p in picks:
        key = _match_key(p)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(p)
    key_fn = _pick_prob_key if rank_by == "prob" else _pick_rank_key
    primary: list[dict] = []
    alternatives: dict[str, list] = {}
    for key in order:
        ranked = sorted(groups[key], key=key_fn, reverse=True)
        primary.append(ranked[0])
        if len(ranked) > 1:
            alternatives[key] = ranked[1:]
    primary.sort(key=key_fn, reverse=True)
    return primary, alternatives


def _render_picks_df(picks: list[dict]) -> None:
    """Render a flat table of picks (no grouping)."""
    df = pd.DataFrame(picks)
    if "reliability" in df.columns:
        df["reliability_display"] = df["reliability"].apply(
            lambda s: f"{_reliability_badge(float(s))} ({float(s):.2f})"
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


def render_picks_table(picks: list[dict]) -> None:
    if not picks:
        return
    # ONE best pick per match. Same-match markets (Double Chance / Draw No Bet /
    # totals on one fixture) are CORRELATED — showing them as separate rows made
    # them look like independent (even contradictory) bets. They move to the
    # expander below; the full list is still available to the combo builder.
    primary, alternatives = group_picks_by_match(picks)
    _render_picks_df(primary)

    # Warnings apply to the picks you'd actually place (the primary set).
    pdf = pd.DataFrame(primary)
    if "value_edge" in pdf.columns:
        big_edges = int((pdf["value_edge"] > 0.20).sum())
        if big_edges > 0:
            st.warning(
                f"⚠️ **{big_edges} pari(s) ont un edge > 20%.** Le marché des cotes est "
                "généralement bien calibré ; un edge aussi élevé révèle souvent un "
                "**défaut de modèle**. Privilégie les paris à edge **2-10%** où la valeur est plus fiable."
            )
    if "reliability" in pdf.columns:
        low_rel = int((pdf["reliability"] < 0.40).sum())
        if low_rel > 0:
            st.warning(
                f"🔴 **{low_rel} pari(s) ont une fiabilité < 0.40.** Soit la taille "
                "d'échantillon (n_matches) est trop faible, soit l'edge ou la "
                "probabilité tombent dans des zones où le modèle est historiquement "
                "moins précis. Considère une mise réduite ou skip."
            )

    # Correlated alternatives per match — grouped, clearly labelled.
    n_alt = sum(len(v) for v in alternatives.values())
    if n_alt:
        prim_by_key = {_match_key(p): p for p in primary}
        with st.expander(
            f"🔀 {n_alt} marché(s) alternatif(s) sur {len(alternatives)} match(s) — "
            "corrélés (mêmes matchs), à NE PAS cumuler"
        ):
            st.caption(
                "Ces lignes portent sur les **mêmes matchs** que le tableau ci-dessus, "
                "exprimés autrement (Double Chance, Draw No Bet, Under/Over…). Elles sont "
                "**corrélées** au pari retenu : choisis-en **une** par match, ne les "
                "empile pas comme des paris indépendants."
            )
            for key, alts in alternatives.items():
                p0 = prim_by_key.get(key, {})
                st.markdown(
                    f"**{p0.get('home_team', '?')} — {p0.get('away_team', '?')}**  ·  "
                    f"retenu : *{p0.get('selection_label', '?')}*"
                )
                _render_picks_df(alts)


def render_safe_picks(picks: list[dict]) -> None:
    """Safe & fast view: one best pick per match ranked by PROBABILITY, with an
    ⚡ badge on early-resolving markets (Over goal-lines / BTTS-Yes)."""
    if not picks:
        return
    primary, _ = group_picks_by_match(picks, rank_by="prob")
    rows = []
    for p in primary:
        rows.append({
            "": "⚡" if is_early_resolving(p.get("selection_code")) else "",
            "Match": f"{p.get('home_team', '?')} — {p.get('away_team', '?')}",
            "Ligue": p.get("league", ""),
            "Pari": p.get("selection_label", "?"),
            "Proba": float(p.get("model_prob") or 0) * 100,
            "Cote": float(p.get("best_odds") or 0),
            "Edge": float(p.get("value_edge") or 0) * 100,
            "Fiabilité": _reliability_badge(float(p.get("reliability") or 0)),
            "Book": p.get("best_book", ""),
        })
    df = pd.DataFrame(rows)
    cfg = {
        "": st.column_config.TextColumn(
            width="small", help="⚡ = validé AVANT la fin du match (Over buts / BTTS-Oui)"),
        "Proba": st.column_config.NumberColumn(format="%.1f%%"),
        "Cote": st.column_config.NumberColumn(format="%.2f"),
        "Edge": st.column_config.NumberColumn(format="%+.1f%%"),
    }
    st.dataframe(df, width='stretch', hide_index=True, column_config=cfg)
    n_early = sum(1 for p in primary if is_early_resolving(p.get("selection_code")))
    st.caption(
        f"⚡ **{n_early}** pari(s) se valident **avant la fin** du match (dès "
        "qu'assez de buts tombent). Triés par probabilité décroissante."
    )


def is_over_goals(selection_code: str | None) -> bool:
    """True for an OVER total-goals line (O05/O15/O25/O35) — never Under, never
    other markets."""
    c = (selection_code or "").upper()
    return c.startswith("O") and c[1:].isdigit()


def _over_line(selection_code: str | None) -> float:
    c = (selection_code or "")
    try:
        return int(c[1:]) / 10.0
    except (ValueError, TypeError):
        return 0.0


def render_over_picks(picks: list[dict], min_line: float = 0.0) -> None:
    """OVER-only specialist view: best Over line per match, ranked by VALUE, with
    the model's expected goals (λ total) — the key Over signal — shown."""
    overs = [
        p for p in picks
        if is_over_goals(p.get("selection_code")) and _over_line(p.get("selection_code")) >= min_line
    ]
    if not overs:
        st.info("Aucun pari **Over** (+EV) sur ces matchs pour la ligne choisie.")
        return
    primary, _ = group_picks_by_match(overs, rank_by="edge")   # best Over line/match, by value
    rows = []
    for p in primary:
        lam = float(p.get("lambda_home") or 0) + float(p.get("lambda_away") or 0)
        rows.append({
            "Match": f"{p.get('home_team', '?')} — {p.get('away_team', '?')}",
            "Ligue": p.get("league", ""),
            "Pari": p.get("selection_label", "?"),
            "Buts attendus": round(lam, 2) if lam > 0 else None,
            "Proba": float(p.get("model_prob") or 0) * 100,
            "Cote": float(p.get("best_odds") or 0),
            "Edge": float(p.get("value_edge") or 0) * 100,
            "Fiabilité": _reliability_badge(float(p.get("reliability") or 0)),
            "Book": p.get("best_book", ""),
        })
    df = pd.DataFrame(rows)
    cfg = {
        "Buts attendus": st.column_config.NumberColumn(
            format="%.2f", help="λ total du modèle = buts attendus dans le match. "
            "Plus il est haut, plus l'Over est probable — LE signal Over."),
        "Proba": st.column_config.NumberColumn(format="%.1f%%"),
        "Cote": st.column_config.NumberColumn(format="%.2f"),
        "Edge": st.column_config.NumberColumn(format="%+.1f%%"),
    }
    st.dataframe(df, width='stretch', hide_index=True, column_config=cfg)


def render_parlays(parlays: list[dict]) -> None:
    if not parlays:
        return
    for i, parlay in enumerate(parlays, 1):
        ev = parlay.get("combined_ev_pct", 0)
        odds = parlay.get("combined_odds", 0)
        prob = parlay.get("combined_prob", 0.0)
        corr_marker = "  ⚠️ corrélé" if parlay.get("correlated") else ""
        # Win probability shown alongside EV : on a ×1000 combo the EV can look
        # large while the real win chance is ~0.1% — surfacing both is honest.
        with st.expander(
            f"Combiné #{i}  —  cote × {odds}  —  proba {prob*100:.2f}%  —  EV {ev:+.1f}%{corr_marker}",
            expanded=(i == 1),
        ):
            if parlay.get("correlated"):
                st.caption(
                    "⚠️ Plusieurs jambes de la même ligue (même jour) — corrélation "
                    "possible. L'EV affichée intègre déjà une décote prudente."
                )
            if ev > 50:
                st.caption(
                    "⚠️ EV très élevée = **artefact de cumul** (le produit des edges du "
                    "modèle exagère, surtout sur les longshots). Fie-toi à la **proba de "
                    "gain** ci-dessus, pas à l'EV."
                )
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
