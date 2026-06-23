"""
Self-computed Elo ratings, from the match results we ALREADY fetch
(football-data.org) for Dixon-Coles.

Why: ClubElo's external API (api.clubelo.com) is HTTP-only and 404s globally for
extended periods — when it's down, the model loses its Elo signal entirely. This
module removes that dependency: every team gets an Elo from the same results that
feed the Poisson model, so the signal is always available and fully in our
control. ClubElo, when reachable, still overlays a better cross-league rating on
top (db.update_team_enrichment only writes non-None values, so the overlay never
nulls out the local baseline) — but it is no longer required.

Standard Elo: sequential updates over chronologically-ordered matches, with a
home-field advantage and a mild margin-of-victory amplifier. Pure & deterministic.
"""
from __future__ import annotations

import math

BASE_ELO = 1500.0
K_FACTOR = 20.0
HOME_ADVANTAGE = 65.0   # Elo points credited to the home side (≈ ClubElo's value)


def expected_home_score(elo_home: float, elo_away: float,
                        home_adv: float = HOME_ADVANTAGE) -> float:
    """Elo expected score (≈ win-or-half-draw probability) for the home side."""
    return 1.0 / (1.0 + 10 ** ((elo_away - (elo_home + home_adv)) / 400.0))


def compute_elo_ratings(
    parsed: list[dict],
    base: float = BASE_ELO,
    k: float = K_FACTOR,
    home_adv: float = HOME_ADVANTAGE,
    mov: bool = True,
) -> dict[str, float]:
    """Return {team_name: elo} computed from parsed match results.

    `parsed`: list of {home_team, away_team, home_goals, away_goals, date}.
    Matches are applied in chronological order (by `date`); unknown teams start
    at `base`. Win/draw/loss scores 1/0.5/0; with `mov`, a bigger goal margin
    mildly amplifies the update. Elo is zero-sum per match, so the pool average
    stays at `base`. Pure — no I/O, trivially testable.
    """
    ratings: dict[str, float] = {}
    ordered = sorted(parsed, key=lambda m: m.get("date") or "")
    for m in ordered:
        h, a = m.get("home_team"), m.get("away_team")
        if not (h and a):
            continue
        try:
            hg, ag = int(m["home_goals"]), int(m["away_goals"])
        except (KeyError, ValueError, TypeError):
            continue
        eh = ratings.get(h, base)
        ea = ratings.get(a, base)
        exp_h = expected_home_score(eh, ea, home_adv)
        if hg > ag:
            score_h = 1.0
        elif hg == ag:
            score_h = 0.5
        else:
            score_h = 0.0
        k_eff = k
        if mov:
            gd = abs(hg - ag)
            if gd >= 2:
                # gd2 → 1.35×, gd3 → 1.55×, gd5 → 1.80× — bounded, log-shaped.
                k_eff = k * (1.0 + 0.5 * math.log(gd))
        delta = k_eff * (score_h - exp_h)
        ratings[h] = eh + delta
        ratings[a] = ea - delta
    return {t: round(v, 1) for t, v in ratings.items()}
