"""
NBA / EuroLeague match prediction from team pace + offensive / defensive rating.

Algorithm — the standard Dean Oliver / Basketball-Reference approach :

  1. **Predicted pace** = average of the two teams' season pace
  2. **Predicted home points**  = pace × (off_home + def_away) / 200
     **Predicted away points**  = pace × (off_away + def_home) / 200
       (the / 200 is because off and def are per-100-poss, summing both gives
        the score per 100 of *team* possessions, ≈ half of total game pace)
  3. **Total** = home + away (used to predict the over/under line)
  4. **Margin** = home − away  (for the moneyline)
  5. Add a fixed **home court advantage** (~3.0 points NBA, ~2.0 EuroLeague)
  6. Convert margin to win probability via a normal CDF with σ ≈ 11 points
     (calibrated empirically — Krautmann & Berri 2007, basketball-reference)

The over/under prob is the normal CDF evaluated at (line − total) / σ_total
where σ_total ≈ 14 for NBA, slightly less for EuroLeague.

Output mirrors the football MatchProbs interface so we can plug into the
existing detect_value_bets() pipeline without touching downstream code.
"""
from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass, asdict
from pathlib import Path

logger = logging.getLogger("betbot.basketball_model")


# Tunable constants ----------------------------------------------------------

NBA_HOME_ADVANTAGE = 2.7         # points; FiveThirtyEight 2019 estimate
EUROLEAGUE_HOME_ADVANTAGE = 1.8  # smaller than NBA per Stein 2021
MARGIN_STD = 11.0                # std-dev of margin in points (calibrated)
TOTAL_STD = 14.0                 # std-dev of total points (calibrated)
LEAGUE_AVG_PACE = 99.0
LEAGUE_AVG_RATING = 115.0

STATS_PATH = Path(os.getenv("BASKETBALL_STATS_PATH", "data/basketball_teams.json"))


@dataclass
class TeamSnapshot:
    name: str
    pace: float
    off_rating: float
    def_rating: float
    games: int = 0
    league: str = "nba"


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

_cached: dict[str, TeamSnapshot] | None = None


def save_teams(teams: dict[str, TeamSnapshot], path: Path | None = None) -> None:
    # Resolve STATS_PATH at CALL time so tests / env overrides take effect.
    path = path if path is not None else STATS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {name: asdict(t) for name, t in teams.items()}
    path.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")
    logger.info("Basketball stats saved : %d teams -> %s", len(payload), path)


def load_teams(path: Path | None = None) -> dict[str, TeamSnapshot]:
    global _cached
    if _cached is not None:
        return _cached
    path = path if path is not None else STATS_PATH
    if not path.exists():
        _cached = {}
        return _cached
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        _cached = {n: TeamSnapshot(**d) for n, d in raw.items()}
        return _cached
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not load basketball stats: %s", exc)
        _cached = {}
        return _cached


def reset_cache() -> None:
    global _cached
    _cached = None


# ---------------------------------------------------------------------------
# Math
# ---------------------------------------------------------------------------

def _normal_cdf(z: float) -> float:
    """CDF of a standard normal — Abramowitz approximation."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


# ---------------------------------------------------------------------------
# Match probability
# ---------------------------------------------------------------------------

@dataclass
class BasketballPrediction:
    home_win: float
    away_win: float
    expected_home_points: float
    expected_away_points: float
    expected_total: float
    expected_margin: float            # positive = home favored
    matched_home: str
    matched_away: str
    league: str


def _name_lookup(name: str, teams: dict[str, TeamSnapshot]) -> tuple[TeamSnapshot | None, str]:
    """Match an Odds-API team name to our snapshot keys.

    The Odds API uses common names like "Boston Celtics" matching bb-ref exactly.
    For EuroLeague the names may differ (FC Barcelona vs Barcelona) — we fall
    back to a token-set match like the football lookup.
    """
    if not name:
        return None, ""
    if name in teams:
        return teams[name], name
    norm = " ".join(name.lower().split())
    for k, t in teams.items():
        if " ".join(k.lower().split()) == norm:
            return t, k
    query_tokens = set(norm.split())
    if not query_tokens:
        return None, ""
    best = None
    best_overlap = 0
    for k, t in teams.items():
        k_tokens = set(k.lower().split())
        shorter = query_tokens if len(query_tokens) <= len(k_tokens) else k_tokens
        longer = k_tokens if shorter == query_tokens else query_tokens
        if shorter and shorter.issubset(longer) and len(shorter) > best_overlap:
            best_overlap = len(shorter)
            best = (t, k)
    if best:
        return best
    return None, ""


def predict(home_name: str, away_name: str, league: str = "nba") -> BasketballPrediction | None:
    """Predict an NBA / EuroLeague match using the saved team stats."""
    teams = load_teams()
    if not teams:
        return None
    th, mh = _name_lookup(home_name, teams)
    ta, ma = _name_lookup(away_name, teams)
    if th is None or ta is None:
        return None

    pace = (th.pace + ta.pace) / 2.0
    home_points = pace * (th.off_rating + ta.def_rating) / 200.0
    away_points = pace * (ta.off_rating + th.def_rating) / 200.0

    hca = NBA_HOME_ADVANTAGE if league == "nba" else EUROLEAGUE_HOME_ADVANTAGE
    home_points += hca / 2.0
    away_points -= hca / 2.0

    margin = home_points - away_points
    p_home = _normal_cdf(margin / MARGIN_STD)

    return BasketballPrediction(
        home_win=round(p_home, 4),
        away_win=round(1.0 - p_home, 4),
        expected_home_points=round(home_points, 1),
        expected_away_points=round(away_points, 1),
        expected_total=round(home_points + away_points, 1),
        expected_margin=round(margin, 1),
        matched_home=mh,
        matched_away=ma,
        league=league,
    )


def predict_total_over(line: float, total_expected: float) -> float:
    """P(total > line) using a normal model around the predicted total."""
    z = (total_expected - line) / TOTAL_STD
    return _normal_cdf(z)


def status() -> dict:
    """Diagnostic for the dashboard / API."""
    teams = load_teams()
    if not teams:
        return {"available": False, "n_teams": 0, "path": str(STATS_PATH)}
    by_league: dict[str, int] = {}
    for t in teams.values():
        by_league[t.league] = by_league.get(t.league, 0) + 1
    top5 = sorted(teams.values(), key=lambda t: -(t.off_rating - t.def_rating))[:5]
    return {
        "available": True,
        "n_teams": len(teams),
        "path": str(STATS_PATH),
        "by_league": by_league,
        "top5_net_rating": [
            {"name": t.name, "off": round(t.off_rating, 1), "def": round(t.def_rating, 1),
             "net": round(t.off_rating - t.def_rating, 1)}
            for t in top5
        ],
    }
