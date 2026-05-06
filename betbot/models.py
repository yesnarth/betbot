"""
Poisson-based match outcome prediction model.

Flow:
  1. Collect recent match results per team (from football_api)
  2. Compute time-weighted attack/defense strengths
  3. Calculate expected goals (lambda_home, lambda_away)
  4. Build score probability matrix via Poisson distribution
  5. Derive H2H probabilities (home/draw/away) and Over 2.5

For leagues without stats (e.g. Africa Cup), falls back to a
multi-bookmaker consensus model that averages fair probabilities
across all available bookmakers.
"""
from __future__ import annotations

import math
import logging
from dataclasses import dataclass, field

from scipy.stats import poisson as scipy_poisson

logger = logging.getLogger("betbot.models")

# How many goals to compute per side in the score matrix (0..MAX_GOALS)
MAX_GOALS = 8

# Exponential time-decay for older matches (half-life ≈ 7 matches)
DECAY_RATE = 0.1

# Default league averages when we have too few data points
DEFAULT_HOME_AVG = 1.35
DEFAULT_AWAY_AVG = 1.10

# Minimum matches needed to trust team-level Poisson model
MIN_MATCHES = 4

# Bookmaker weights for consensus model (higher = sharper / more trustworthy)
BOOK_WEIGHTS: dict[str, float] = {
    "pinnacle": 3.0,
    "bet365": 1.5,
    "williamhill": 1.2,
    "unibet": 1.0,
    "betclic": 0.8,
}
DEFAULT_BOOK_WEIGHT = 1.0


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class MatchProbs:
    home_win: float
    draw: float
    away_win: float
    over_25: float
    lambda_home: float
    lambda_away: float
    model: str  # "poisson" or "consensus"

    def outcomes(self) -> list[tuple[str, str, float]]:
        """Return list of (code, label, prob) tuples."""
        return [
            ("1", "Victoire domicile", self.home_win),
            ("X", "Match nul", self.draw),
            ("2", "Victoire extérieur", self.away_win),
            ("O25", "Plus de 2.5 buts", self.over_25),
        ]


@dataclass
class TeamStats:
    name: str
    attack_home: float    # avg goals scored at home / league_home_avg
    defense_home: float   # avg goals conceded at home / league_away_avg
    attack_away: float    # avg goals scored away / league_away_avg
    defense_away: float   # avg goals conceded away / league_home_avg
    matches_analyzed: int
    # Phase 8 enrichment — all optional to keep legacy callers working
    elo_rating: float | None = None
    xg_for: float | None = None       # xG per match
    xg_against: float | None = None   # xGA per match

    # Kept for DB compatibility — represent expected goals vs an average opponent
    @property
    def lambda_home(self) -> float:
        return self.attack_home  # attack strength IS the lambda vs average defense=1.0

    @property
    def lambda_away(self) -> float:
        return self.attack_away


# ---------------------------------------------------------------------------
# Poisson core
# ---------------------------------------------------------------------------

def _exp_weight(k: int) -> float:
    """Weight for the k-th most recent match (k=0 is most recent)."""
    return math.exp(-DECAY_RATE * k)


def build_team_stats(
    team_name: str,
    parsed_matches: list[dict],
    league_home_avg: float,
    league_away_avg: float,
) -> TeamStats | None:
    """
    Build TeamStats from parsed match results for a single team.

    parsed_matches: list of {home_team, away_team, home_goals, away_goals, date}
    sorted most-recent first.
    """
    home_games = [m for m in parsed_matches if m["home_team"] == team_name]
    away_games = [m for m in parsed_matches if m["away_team"] == team_name]

    if len(home_games) + len(away_games) < MIN_MATCHES:
        return None

    def weighted_avg(values: list[tuple[float, float]]) -> float:
        """values = [(value, weight), ...]"""
        total_w = sum(w for _, w in values)
        if total_w == 0:
            return 0.0
        return sum(v * w for v, w in values) / total_w

    home_scored = weighted_avg(
        [(m["home_goals"], _exp_weight(k)) for k, m in enumerate(home_games)]
    )
    home_conceded = weighted_avg(
        [(m["away_goals"], _exp_weight(k)) for k, m in enumerate(home_games)]
    )
    away_scored = weighted_avg(
        [(m["away_goals"], _exp_weight(k)) for k, m in enumerate(away_games)]
    )
    away_conceded = weighted_avg(
        [(m["home_goals"], _exp_weight(k)) for k, m in enumerate(away_games)]
    )

    # Strengths relative to league average (1.0 = average team)
    attack_home  = home_scored   / league_home_avg if league_home_avg > 0 else 1.0
    defense_home = home_conceded / league_away_avg if league_away_avg > 0 else 1.0
    attack_away  = away_scored   / league_away_avg if league_away_avg > 0 else 1.0
    defense_away = away_conceded / league_home_avg if league_home_avg > 0 else 1.0

    # Clamp strengths to sensible range
    attack_home  = max(0.1, min(attack_home, 4.0))
    defense_home = max(0.1, min(defense_home, 4.0))
    attack_away  = max(0.1, min(attack_away, 4.0))
    defense_away = max(0.1, min(defense_away, 4.0))

    return TeamStats(
        name=team_name,
        attack_home=round(attack_home, 4),
        defense_home=round(defense_home, 4),
        attack_away=round(attack_away, 4),
        defense_away=round(defense_away, 4),
        matches_analyzed=len(home_games) + len(away_games),
    )


def compute_league_averages(parsed_matches: list[dict]) -> tuple[float, float]:
    """Return (avg_home_goals, avg_away_goals) for a league."""
    if not parsed_matches:
        return DEFAULT_HOME_AVG, DEFAULT_AWAY_AVG
    home = sum(m["home_goals"] for m in parsed_matches) / len(parsed_matches)
    away = sum(m["away_goals"] for m in parsed_matches) / len(parsed_matches)
    return home, away


def _dixon_coles_tau(i: int, j: int, lambda_home: float, lambda_away: float,
                     rho: float = -0.10) -> float:
    """
    Dixon-Coles bivariate adjustment for low-scoring matches.

    Independent Poisson under-estimates 0-0 / 1-1 and over-estimates 1-0 / 0-1
    because real football has positive correlation between teams' scoring rates.
    The τ multiplier corrects exactly the four most-affected scores; ρ ∈ [-0.20, 0]
    is the empirical "draw inflation" parameter (Dixon-Coles 1997, Goddard 2005).

    Returns 1.0 for any score outside {(0,0), (0,1), (1,0), (1,1)}.
    """
    if i == 0 and j == 0:
        return 1.0 - lambda_home * lambda_away * rho
    if i == 0 and j == 1:
        return 1.0 + lambda_home * rho
    if i == 1 and j == 0:
        return 1.0 + lambda_away * rho
    if i == 1 and j == 1:
        return 1.0 - rho
    return 1.0


def poisson_match_probs(lambda_home: float, lambda_away: float,
                        dixon_coles_rho: float = -0.10) -> MatchProbs:
    """
    Build the score probability matrix and derive outcome probabilities.

    Uses independent Poisson with the Dixon-Coles bivariate τ correction on
    the 4 low-scoring cells (0-0, 0-1, 1-0, 1-1). Set `dixon_coles_rho=0` to
    disable the correction — useful for unit tests of the raw Poisson.
    """
    grid = range(MAX_GOALS + 1)
    home_pmf = [scipy_poisson.pmf(i, lambda_home) for i in grid]
    away_pmf = [scipy_poisson.pmf(j, lambda_away) for j in grid]

    prob_home = 0.0
    prob_draw = 0.0
    prob_away = 0.0
    prob_over25 = 0.0

    for i in grid:
        for j in grid:
            p = home_pmf[i] * away_pmf[j]
            if dixon_coles_rho != 0.0:
                p *= _dixon_coles_tau(i, j, lambda_home, lambda_away, dixon_coles_rho)
            if i > j:
                prob_home += p
            elif i == j:
                prob_draw += p
            else:
                prob_away += p
            if i + j > 2:
                prob_over25 += p

    # Normalize H2H to sum to 1 (handles truncation error from MAX_GOALS)
    total = prob_home + prob_draw + prob_away
    if total > 0:
        prob_home /= total
        prob_draw /= total
        prob_away /= total

    return MatchProbs(
        home_win=round(prob_home, 6),
        draw=round(prob_draw, 6),
        away_win=round(prob_away, 6),
        over_25=round(prob_over25, 6),
        lambda_home=round(lambda_home, 4),
        lambda_away=round(lambda_away, 4),
        model="poisson",
    )


# ---------------------------------------------------------------------------
# Consensus model (fallback when no team stats available)
# ---------------------------------------------------------------------------

def _remove_margin(outcomes: list[dict]) -> dict[str, float]:
    """
    Convert bookmaker outcomes to fair (no-vig) probabilities.
    Uses the multiplicative method: divide each implied prob by the overround.
    """
    raw = {}
    for o in outcomes:
        try:
            price = float(o["price"])
            if price > 1.0:
                raw[o["name"]] = 1.0 / price
        except (KeyError, ValueError, ZeroDivisionError):
            continue
    overround = sum(raw.values())
    if overround <= 0:
        return {}
    return {name: prob / overround for name, prob in raw.items()}


def consensus_match_probs(event: dict) -> MatchProbs | None:
    """
    Multi-bookmaker consensus model.
    Returns None if fewer than 2 bookmakers cover the event.
    """
    home_name = event.get("home_team", "")
    away_name = event.get("away_team", "")

    weighted: dict[str, float] = {}
    total_weight = 0.0
    book_count = 0

    for bm in event.get("bookmakers", []):
        book_key = bm.get("key", "")
        weight = BOOK_WEIGHTS.get(book_key, DEFAULT_BOOK_WEIGHT)
        for mkt in bm.get("markets", []):
            if mkt.get("key") != "h2h":
                continue
            fair = _remove_margin(mkt.get("outcomes", []))
            if len(fair) < 3:
                continue
            for name, prob in fair.items():
                weighted[name] = weighted.get(name, 0.0) + prob * weight
            total_weight += weight
            book_count += 1

    if book_count < 2 or total_weight == 0:
        logger.debug("Pas assez de bookmakers (%d) pour %s vs %s", book_count, home_name, away_name)
        return None

    consensus = {name: val / total_weight for name, val in weighted.items()}

    prob_home = consensus.get(home_name, 0.0)
    prob_draw = consensus.get("Draw", 0.0)
    prob_away = consensus.get(away_name, 0.0)

    total = prob_home + prob_draw + prob_away
    if total <= 0:
        return None

    # Normalize
    prob_home /= total
    prob_draw /= total
    prob_away /= total

    # Estimate Over 2.5 from consensus home/away probs heuristically
    # (average lambdas implied by prob distributions)
    lh = _prob_to_lambda(prob_home, prob_draw, prob_away, home=True)
    la = _prob_to_lambda(prob_home, prob_draw, prob_away, home=False)
    probs = poisson_match_probs(lh, la)

    return MatchProbs(
        home_win=round(prob_home, 6),
        draw=round(prob_draw, 6),
        away_win=round(prob_away, 6),
        over_25=probs.over_25,
        lambda_home=lh,
        lambda_away=la,
        model="consensus",
    )


def _prob_to_lambda(p_home: float, p_draw: float, p_away: float, home: bool) -> float:
    """
    Crude inverse: estimate lambda from H2H probs.
    Uses empirical approximation: lambda ≈ -ln(p_draw) * share
    """
    if p_draw <= 0:
        return DEFAULT_HOME_AVG if home else DEFAULT_AWAY_AVG
    base = -math.log(max(p_draw, 0.01))
    if home:
        return round(max(0.3, base * (p_home + 0.5 * p_draw) / 0.5), 3)
    else:
        return round(max(0.3, base * (p_away + 0.5 * p_draw) / 0.5), 3)


# ---------------------------------------------------------------------------
# Best-odds extractor
# ---------------------------------------------------------------------------

@dataclass
class BestOdds:
    outcome_name: str
    price: float
    bookmaker: str


def extract_best_odds(event: dict, outcome_name: str) -> BestOdds | None:
    """Find the best (highest) decimal odds for a given outcome across all bookmakers."""
    best: BestOdds | None = None
    for bm in event.get("bookmakers", []):
        for mkt in bm.get("markets", []):
            if mkt.get("key") != "h2h":
                continue
            for o in mkt.get("outcomes", []):
                if o.get("name") != outcome_name:
                    continue
                try:
                    price = float(o["price"])
                except (KeyError, ValueError):
                    continue
                if best is None or price > best.price:
                    best = BestOdds(
                        outcome_name=outcome_name,
                        price=price,
                        bookmaker=bm.get("title", bm.get("key", "?")),
                    )
    return best


# ---------------------------------------------------------------------------
# Blended model — Dixon-Coles + xG + ELO with optional weather modifier
# ---------------------------------------------------------------------------

def blended_match_probs(
    home_stats: "TeamStats",
    away_stats: "TeamStats",
    league_home_avg: float,
    league_away_avg: float,
    weather_modifier: float = 1.0,
    elo_weight: float = 0.30,
    xg_weight: float = 0.35,
    weights_sum_check: bool = True,
) -> "MatchProbs":
    """
    Production-grade prediction blending three independent signals:

      1. **Dixon-Coles** on goals (the base λ)
      2. **xG** when available — overrides the λ-from-goals with λ-from-xG,
         which strips out finishing variance (5-yr backtests show ~6% lower
         calibration error vs. raw goals).
      3. **ELO** — used as a Bayesian prior to nudge probabilities toward
         the long-term club strength rating.

    Weights:
      - Dixon-Coles weight = 1.0 - elo_weight - xg_weight
      - elo_weight: 0.30   (literature converges on 0.25-0.35)
      - xg_weight:  0.35   (when xG data available; otherwise re-weighted to 0)

    weather_modifier: multiplicative factor on λ (≈ 0.85-1.05). Applied at the
    very end on both sides equally — it lowers expected goals on both teams
    when conditions are bad.
    """
    if weights_sum_check and (elo_weight + xg_weight) > 0.95:
        raise ValueError("elo_weight + xg_weight must leave room for Dixon-Coles")

    # ---- Signal 1: Dixon-Coles λ from historical goals ---------------------
    dc_home = home_stats.attack_home * away_stats.defense_away * league_home_avg
    dc_away = away_stats.attack_away * home_stats.defense_home * league_away_avg

    # ---- Signal 2: λ derived from xG (if available on both teams) ---------
    has_xg = (
        home_stats.xg_for is not None and home_stats.xg_against is not None
        and away_stats.xg_for is not None and away_stats.xg_against is not None
    )
    if has_xg:
        # Mirror Dixon-Coles structure but on xG. xG_for is "attack" ; xG_against is "defense"
        # Normalize each by the league average (use goals avg as a proxy when xG league avg unknown)
        league_total = league_home_avg + league_away_avg
        norm = max(0.5, league_total / 2.0)
        attack_home_xg = (home_stats.xg_for or 0) / norm
        defense_away_xg = (away_stats.xg_against or 0) / norm
        attack_away_xg = (away_stats.xg_for or 0) / norm
        defense_home_xg = (home_stats.xg_against or 0) / norm
        xg_home = attack_home_xg * defense_away_xg * league_home_avg
        xg_away = attack_away_xg * defense_home_xg * league_away_avg
    else:
        xg_home = dc_home
        xg_away = dc_away

    # Effective xg_weight is 0 when no xG data
    eff_xg_weight = xg_weight if has_xg else 0.0
    eff_dc_weight = 1.0 - elo_weight - eff_xg_weight

    # Linear combination of the two λ sources
    lambda_home = eff_dc_weight * dc_home + eff_xg_weight * xg_home
    lambda_away = eff_dc_weight * dc_away + eff_xg_weight * xg_away

    # ---- Signal 3: ELO prior on H2H probability ---------------------------
    has_elo = home_stats.elo_rating is not None and away_stats.elo_rating is not None
    elo_home_prob = None
    if has_elo:
        from betbot.data_sources.club_elo import elo_win_probability
        # P(home doesn't lose) ≈ home_win + draw
        elo_home_no_loss = elo_win_probability(home_stats.elo_rating, away_stats.elo_rating)
        elo_home_prob = elo_home_no_loss   # we'll redistribute home/draw later
    else:
        elo_weight = 0.0  # no ELO → all weight back on Dixon-Coles + xG

    # ---- Apply weather modifier ------------------------------------------
    lambda_home *= weather_modifier
    lambda_away *= weather_modifier
    lambda_home = max(0.2, min(lambda_home, 5.0))
    lambda_away = max(0.2, min(lambda_away, 5.0))

    # ---- Run Poisson on the blended λ -------------------------------------
    poisson_probs = poisson_match_probs(lambda_home, lambda_away)

    # ---- Apply ELO Bayesian shrinkage on H2H probabilities ---------------
    if has_elo:
        # Decompose ELO no-loss into (home, draw) keeping draw ratio from Poisson
        poisson_no_loss = poisson_probs.home_win + poisson_probs.draw
        if poisson_no_loss > 0:
            draw_share = poisson_probs.draw / poisson_no_loss
            blended_no_loss = (1 - elo_weight) * poisson_no_loss + elo_weight * elo_home_prob
            home_win = blended_no_loss * (1 - draw_share)
            draw     = blended_no_loss * draw_share
            away_win = 1.0 - home_win - draw
        else:
            home_win, draw, away_win = poisson_probs.home_win, poisson_probs.draw, poisson_probs.away_win
    else:
        home_win, draw, away_win = poisson_probs.home_win, poisson_probs.draw, poisson_probs.away_win

    return MatchProbs(
        home_win=round(home_win, 6),
        draw=round(draw, 6),
        away_win=round(away_win, 6),
        over_25=poisson_probs.over_25,
        lambda_home=round(lambda_home, 4),
        lambda_away=round(lambda_away, 4),
        model="blended" if (has_xg or has_elo) else "poisson",
    )
