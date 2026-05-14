"""
Surface-aware ELO ratings for ATP / WTA tennis players.

Algorithm: classical Elo with two refinements widely used in tennis analytics:

  1. **Decay-K** : K-factor decays with the number of matches a player has
     played, so new players move quickly and veterans stabilise.
     Formula: K(n) = 250 / (n + 5) ** 0.4  (FiveThirtyEight, 2016)

  2. **Surface-specific ratings** : in addition to the overall rating, every
     player has a separate Hard / Clay / Grass rating. At prediction time
     we BLEND the overall and surface-specific rating: surface_weight = 0.5.
     This captures the empirical fact that some players (e.g. Nadal on clay)
     significantly out-perform their overall rating on certain surfaces.

  3. **Tournament-level weighting** : Grand Slam matches carry 1.5× weight
     of regular tour matches, Masters 1000 carry 1.2×.

Persistence: ratings are kept in a JSON file (`data/tennis_elo.json`) keyed
by player name. The bootstrap CLI rebuilds them from Sackmann history.
"""
from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("betbot.tennis_model")

DEFAULT_RATING = 1500.0
# Maximum weight of the surface-specific rating in the blend. The actual
# weight scales linearly with how many matches the player has played on
# that surface — a player with 3 clay matches shouldn't get 50% clay
# weighting (the sample is pure noise). FiveThirtyEight's tennis Elo
# methodology recommends ≥ 20 surface matches before trusting the
# surface signal fully.
SURFACE_BLEND_MAX = 0.50
SURFACE_BLEND_FULL_AT = 20   # # surface matches needed to reach SURFACE_BLEND_MAX
ELO_SCALE = 400.0            # standard logistic divisor

LEVEL_WEIGHT = {
    "G": 1.5,   # Grand Slam
    "M": 1.2,   # Masters 1000 / WTA 1000
    "F": 1.3,   # Tour Finals
    "A": 1.0,   # Regular ATP / WTA tour
    "C": 0.7,   # Challenger / 125
    "D": 0.5,   # Davis Cup / Fed Cup
    "":  1.0,
}

ELO_PATH = Path(os.getenv("TENNIS_ELO_PATH", "data/tennis_elo.json"))


@dataclass
class PlayerRating:
    name: str
    overall: float = DEFAULT_RATING
    hard:    float = DEFAULT_RATING
    clay:    float = DEFAULT_RATING
    grass:   float = DEFAULT_RATING
    matches: int = 0
    matches_hard:  int = 0
    matches_clay:  int = 0
    matches_grass: int = 0
    last_match: str = ""

    def k_factor(self, surface_matches: int) -> float:
        """Decay-K curve from FiveThirtyEight tennis Elo paper."""
        n = max(1, surface_matches)
        return 250.0 / (n + 5) ** 0.4

    def rating_for(self, surface: str) -> float:
        """Blended rating used at prediction time.

        The surface-specific weight scales with how many matches we have on
        that surface: a player with 0 matches on clay gets 0% weighting on
        the (still-default-1500) clay rating; a player with 20+ clay
        matches gets the full SURFACE_BLEND_MAX. This prevents the bug
        where a hard-court specialist with 3 clay matches got 50%
        weighting on a noisy clay rating, inflating predicted edge.
        """
        s = (surface or "").lower()
        if s == "clay":
            surface_rating = self.clay
            surface_n = self.matches_clay
        elif s == "grass":
            surface_rating = self.grass
            surface_n = self.matches_grass
        else:
            surface_rating = self.hard
            surface_n = self.matches_hard
        # Adaptive blend: 0 surface matches → 0% surface weight (pure overall);
        # 20+ surface matches → SURFACE_BLEND_MAX. Linear ramp in between.
        surface_weight = min(surface_n / SURFACE_BLEND_FULL_AT, 1.0) * SURFACE_BLEND_MAX
        return surface_weight * surface_rating + (1 - surface_weight) * self.overall


# ---------------------------------------------------------------------------
# ELO update step
# ---------------------------------------------------------------------------

def _expected(rating_a: float, rating_b: float) -> float:
    """P(a wins) under Elo logistic."""
    return 1.0 / (1.0 + 10 ** ((rating_b - rating_a) / ELO_SCALE))


def _update_match(
    ratings: dict[str, PlayerRating],
    winner_name: str,
    loser_name: str,
    surface: str,
    level: str,
    date: str,
) -> None:
    """In-place Elo update for one match. Mutates `ratings`."""
    w = ratings.setdefault(winner_name, PlayerRating(name=winner_name))
    l = ratings.setdefault(loser_name,  PlayerRating(name=loser_name))

    # Overall
    expected_w = _expected(w.overall, l.overall)
    weight = LEVEL_WEIGHT.get(level, 1.0)
    k_w = w.k_factor(w.matches) * weight
    k_l = l.k_factor(l.matches) * weight
    w.overall += k_w * (1.0 - expected_w)
    l.overall -= k_l * (1.0 - expected_w)
    w.matches += 1
    l.matches += 1
    w.last_match = date
    l.last_match = date

    # Surface-specific
    s = (surface or "").lower()
    if s in ("hard", "clay", "grass"):
        attr = s
        cnt_attr = f"matches_{s}"
        rating_w_s = getattr(w, attr)
        rating_l_s = getattr(l, attr)
        expected_w_s = _expected(rating_w_s, rating_l_s)
        k_w_s = w.k_factor(getattr(w, cnt_attr)) * weight
        k_l_s = l.k_factor(getattr(l, cnt_attr)) * weight
        setattr(w, attr, rating_w_s + k_w_s * (1.0 - expected_w_s))
        setattr(l, attr, rating_l_s - k_l_s * (1.0 - expected_w_s))
        setattr(w, cnt_attr, getattr(w, cnt_attr) + 1)
        setattr(l, cnt_attr, getattr(l, cnt_attr) + 1)


# ---------------------------------------------------------------------------
# Bulk training / persistence
# ---------------------------------------------------------------------------

def train_from_matches(matches: list) -> dict[str, PlayerRating]:
    """Replay a chronologically-sorted list of TennisMatch and return final ratings."""
    ratings: dict[str, PlayerRating] = {}
    for m in matches:
        _update_match(ratings, m.winner, m.loser, m.surface, m.tourney_level, m.date)
    return ratings


def save_ratings(ratings: dict[str, PlayerRating], path: Path | None = None) -> None:
    # Resolve ELO_PATH at CALL time so tests (and runtime overrides via env)
    # see the current module-level value, not the value captured at import.
    path = path if path is not None else ELO_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {name: r.__dict__ for name, r in ratings.items()}
    path.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")
    logger.info("Tennis ELO saved : %d players -> %s", len(payload), path)


_cached: dict[str, PlayerRating] | None = None


def load_ratings(path: Path | None = None) -> dict[str, PlayerRating]:
    global _cached
    if _cached is not None:
        return _cached
    path = path if path is not None else ELO_PATH
    if not path.exists():
        _cached = {}
        return _cached
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        _cached = {name: PlayerRating(**data) for name, data in raw.items()}
        return _cached
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not load tennis ELO file: %s", exc)
        _cached = {}
        return _cached


def reset_cache() -> None:
    global _cached
    _cached = None


# ---------------------------------------------------------------------------
# Match probability for live odds events
# ---------------------------------------------------------------------------

@dataclass
class TennisMatchProbs:
    home_win: float
    away_win: float
    surface: str
    rating_home: float
    rating_away: float
    matched_home: str  # may differ from query if fuzzy-matched
    matched_away: str


def _name_lookup(name: str, ratings: dict[str, PlayerRating]) -> tuple[PlayerRating | None, str]:
    """Find a player's rating, with simple normalization fallback."""
    if not name:
        return None, ""
    if name in ratings:
        return ratings[name], name
    # Sackmann uses "Carlos Alcaraz", Odds API may use "Alcaraz Carlos" or "Carlos Alcaraz"
    norm = " ".join(name.lower().split())
    for k, r in ratings.items():
        if " ".join(k.lower().split()) == norm:
            return r, k
    # Tokens-must-match fallback (handles "Djokovic" vs "Novak Djokovic")
    query_tokens = set(norm.split())
    if not query_tokens:
        return None, ""
    best = None
    best_overlap = 0
    for k, r in ratings.items():
        k_tokens = set(k.lower().split())
        shorter = query_tokens if len(query_tokens) <= len(k_tokens) else k_tokens
        longer = k_tokens if shorter == query_tokens else query_tokens
        if shorter and shorter.issubset(longer) and len(shorter) > best_overlap:
            best_overlap = len(shorter)
            best = (r, k)
    if best:
        return best
    return None, ""


def predict(
    home_name: str,
    away_name: str,
    surface: str = "Hard",
) -> TennisMatchProbs | None:
    """
    Predict a tennis match outcome using the saved ELO ratings.
    Returns None if either player has no rating.
    """
    ratings = load_ratings()
    if not ratings:
        return None
    rh, mh = _name_lookup(home_name, ratings)
    ra, ma = _name_lookup(away_name, ratings)
    if rh is None or ra is None:
        return None
    rating_h = rh.rating_for(surface)
    rating_a = ra.rating_for(surface)
    p_home = _expected(rating_h, rating_a)
    return TennisMatchProbs(
        home_win=round(p_home, 4),
        away_win=round(1 - p_home, 4),
        surface=surface,
        rating_home=round(rating_h, 1),
        rating_away=round(rating_a, 1),
        matched_home=mh,
        matched_away=ma,
    )


def status() -> dict:
    """Diagnostic for the dashboard / API."""
    ratings = load_ratings()
    n = len(ratings)
    if n == 0:
        return {"available": False, "n_players": 0, "path": str(ELO_PATH)}
    most_recent = max((r.last_match for r in ratings.values() if r.last_match), default="")
    top5 = sorted(ratings.values(), key=lambda r: -r.overall)[:5]
    return {
        "available": True,
        "n_players": n,
        "path": str(ELO_PATH),
        "most_recent_match": most_recent,
        "top5": [{"name": r.name, "overall": round(r.overall, 1)} for r in top5],
    }
