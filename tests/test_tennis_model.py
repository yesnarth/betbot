"""Unit tests for betbot/tennis_model.py — surface-aware ELO ratings."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from betbot import tennis_model
from betbot.tennis_model import (
    DEFAULT_RATING,
    PlayerRating,
    SURFACE_BLEND_MAX,
    SURFACE_BLEND_FULL_AT,
    _expected,
    _name_lookup,
    _update_match,
    train_from_matches,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

class _Match:
    def __init__(self, winner, loser, surface="Hard", level="A", date="2025-01-01"):
        self.winner = winner
        self.loser = loser
        self.surface = surface
        self.tourney_level = level
        self.date = date


@pytest.fixture(autouse=True)
def isolated_cache_and_path(tmp_path, monkeypatch):
    """Each test gets a fresh ELO file and a clean module cache."""
    p = tmp_path / "tennis_elo.json"
    monkeypatch.setattr(tennis_model, "ELO_PATH", p)
    tennis_model.reset_cache()
    yield p
    tennis_model.reset_cache()


# ---------------------------------------------------------------------------
# Math primitives
# ---------------------------------------------------------------------------

def test_expected_returns_half_for_equal_ratings():
    assert _expected(1500, 1500) == pytest.approx(0.5)


def test_expected_favors_higher_rating():
    high = _expected(1700, 1500)  # +200 elo
    assert 0.7 < high < 0.85
    # And the symmetric case
    assert _expected(1500, 1700) == pytest.approx(1 - high)


def test_player_rating_with_enough_surface_matches_uses_max_blend():
    """Player with 20+ matches on the queried surface gets the full
    SURFACE_BLEND_MAX (currently 0.5) weighting on the surface rating."""
    p = PlayerRating(
        name="X", overall=2000.0, hard=2200.0, clay=1800.0, grass=1900.0,
        matches_clay=25, matches_grass=30, matches_hard=50,
    )
    # 0.5 * 1800 + 0.5 * 2000 = 1900
    assert p.rating_for("Clay") == pytest.approx(1900.0)
    # 0.5 * 1900 + 0.5 * 2000 = 1950
    assert p.rating_for("Grass") == pytest.approx(1950.0)
    # Unknown surface defaults to hard
    assert p.rating_for("Unknown") == p.rating_for("Hard")


def test_player_rating_adaptive_blend_scales_with_matches():
    """Player with few surface matches gets reduced surface weighting —
    prevents trusting noisy default-1500 ratings on unfamiliar surfaces."""
    p = PlayerRating(name="X", overall=2000.0, clay=1500.0, matches_clay=0)
    # 0 clay matches → 0% surface weight → pure overall
    assert p.rating_for("Clay") == pytest.approx(2000.0)

    # Half-way to full trust (10 matches → 25% weight on clay)
    p.matches_clay = 10
    expected_weight = (10 / SURFACE_BLEND_FULL_AT) * SURFACE_BLEND_MAX
    expected_rating = expected_weight * 1500.0 + (1 - expected_weight) * 2000.0
    assert p.rating_for("Clay") == pytest.approx(expected_rating)


def test_player_rating_rookie_uses_overall_not_default():
    """A rookie with no matches on the predicted surface relies entirely on
    overall rating — the default 1500 surface_rating shouldn't drag down
    a strong overall."""
    rookie = PlayerRating(name="Rookie", overall=1700.0,
                          hard=1500.0, clay=1500.0, grass=1500.0)
    # 0 surface matches everywhere → all queries return overall
    for s in ("Hard", "Clay", "Grass"):
        assert rookie.rating_for(s) == pytest.approx(1700.0)


def test_k_factor_decays_with_experience():
    p = PlayerRating(name="X")
    k_new = p.k_factor(0)        # rookie
    k_mid = p.k_factor(50)
    k_vet = p.k_factor(500)
    assert k_new > k_mid > k_vet
    # Veterans converge toward small adjustments
    assert k_vet < 30.0


# ---------------------------------------------------------------------------
# Update step
# ---------------------------------------------------------------------------

def test_update_match_creates_new_players_at_default():
    ratings: dict[str, PlayerRating] = {}
    _update_match(ratings, "Alice", "Bob", "Hard", "A", "2025-01-01")
    assert "Alice" in ratings and "Bob" in ratings
    assert ratings["Alice"].matches == 1
    assert ratings["Bob"].matches == 1
    # Winner gained, loser lost from default
    assert ratings["Alice"].overall > DEFAULT_RATING
    assert ratings["Bob"].overall < DEFAULT_RATING


def test_update_match_grand_slam_carries_more_weight():
    ratings_atp = {}
    ratings_gs = {}
    _update_match(ratings_atp, "A", "B", "Hard", "A", "2025-01-01")  # tour level
    _update_match(ratings_gs, "A", "B", "Hard", "G", "2025-01-01")   # Grand Slam
    delta_atp = ratings_atp["A"].overall - DEFAULT_RATING
    delta_gs = ratings_gs["A"].overall - DEFAULT_RATING
    assert delta_gs > delta_atp  # Grand Slam = 1.5× weight


def test_update_match_separates_surface_ratings():
    ratings = {}
    _update_match(ratings, "A", "B", "Clay", "A", "2025-01-01")
    p = ratings["A"]
    # Clay went up, hard/grass stay at default (or untouched)
    assert p.clay > DEFAULT_RATING
    assert p.hard == DEFAULT_RATING
    assert p.grass == DEFAULT_RATING
    assert p.matches_clay == 1
    assert p.matches_hard == 0


# ---------------------------------------------------------------------------
# Bulk training
# ---------------------------------------------------------------------------

def test_train_from_matches_replays_in_order():
    matches = [
        _Match("A", "B", "Hard", "A", "2025-01-01"),
        _Match("A", "B", "Hard", "A", "2025-01-08"),
        _Match("A", "B", "Hard", "A", "2025-01-15"),
    ]
    ratings = train_from_matches(matches)
    # A won 3 matches in a row; should be well above default
    assert ratings["A"].overall > 1550.0
    assert ratings["B"].overall < 1450.0
    assert ratings["A"].matches == 3


def test_train_from_matches_preserves_invariants():
    matches = [_Match(f"P{i}", f"P{i+1}", "Hard", "A", f"2025-01-{i:02d}")
               for i in range(1, 11)]
    ratings = train_from_matches(matches)
    # Sanity bounds — no rating should be negative or absurdly high
    for r in ratings.values():
        assert 1000 < r.overall < 2500


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def test_save_and_load_roundtrip(isolated_cache_and_path):
    matches = [_Match("Sinner", "Alcaraz", "Hard", "G", "2025-09-01")]
    ratings = train_from_matches(matches)
    tennis_model.save_ratings(ratings, path=isolated_cache_and_path)
    assert isolated_cache_and_path.exists()

    tennis_model.reset_cache()
    loaded = tennis_model.load_ratings(path=isolated_cache_and_path)
    assert loaded["Sinner"].overall == pytest.approx(ratings["Sinner"].overall)
    assert loaded["Alcaraz"].overall == pytest.approx(ratings["Alcaraz"].overall)


def test_load_returns_empty_when_file_missing(tmp_path, monkeypatch):
    missing = tmp_path / "does_not_exist.json"
    monkeypatch.setattr(tennis_model, "ELO_PATH", missing)
    tennis_model.reset_cache()
    assert tennis_model.load_ratings() == {}


def test_load_returns_empty_on_corrupt_file(isolated_cache_and_path):
    isolated_cache_and_path.write_text("{ this is not valid json", encoding="utf-8")
    tennis_model.reset_cache()
    assert tennis_model.load_ratings() == {}


# ---------------------------------------------------------------------------
# Name lookup (token-set match)
# ---------------------------------------------------------------------------

def test_name_lookup_exact():
    cache = {"Carlos Alcaraz": PlayerRating(name="Carlos Alcaraz", overall=2100)}
    r, matched = _name_lookup("Carlos Alcaraz", cache)
    assert r is not None
    assert matched == "Carlos Alcaraz"


def test_name_lookup_handles_extra_whitespace():
    cache = {"Carlos Alcaraz": PlayerRating(name="Carlos Alcaraz")}
    r, matched = _name_lookup("  carlos  alcaraz  ", cache)
    assert r is not None
    assert matched == "Carlos Alcaraz"


def test_name_lookup_token_set_does_not_create_false_positives():
    """'Djokovic' alone should NOT match 'Novak Djokovic' via the token-set
    rule — query tokens {djokovic} ARE a subset, so it WILL match. This is
    intentional (single-token search). But ensure 'Novak' doesn't match
    'Carlos Alcaraz' because no token overlaps."""
    cache = {
        "Novak Djokovic": PlayerRating(name="Novak Djokovic"),
        "Carlos Alcaraz": PlayerRating(name="Carlos Alcaraz"),
    }
    r, matched = _name_lookup("Djokovic", cache)
    assert matched == "Novak Djokovic"
    r2, matched2 = _name_lookup("Federer", cache)
    assert r2 is None  # not in cache


def test_name_lookup_returns_none_for_empty():
    cache = {"X": PlayerRating(name="X")}
    r, matched = _name_lookup("", cache)
    assert r is None
    assert matched == ""


# ---------------------------------------------------------------------------
# predict() end-to-end
# ---------------------------------------------------------------------------

def test_predict_returns_none_when_player_missing(isolated_cache_and_path):
    cache = {"Solo": PlayerRating(name="Solo", overall=1800.0)}
    tennis_model.save_ratings(cache, path=isolated_cache_and_path)
    tennis_model.reset_cache()
    assert tennis_model.predict("Solo", "GhostPlayer", "Hard") is None


def test_predict_higher_rated_player_is_favored(isolated_cache_and_path):
    # Need at least SURFACE_BLEND_FULL_AT (20) hard matches each to trust
    # the surface ratings beyond pure overall.
    ratings = {
        "Strong": PlayerRating(name="Strong", overall=2100, hard=2150,
                               clay=2050, grass=2000,
                               matches_hard=30, matches_clay=25, matches_grass=20),
        "Weak":   PlayerRating(name="Weak", overall=1500, hard=1500,
                               clay=1500, grass=1500,
                               matches_hard=30, matches_clay=25, matches_grass=20),
    }
    tennis_model.save_ratings(ratings, path=isolated_cache_and_path)
    tennis_model.reset_cache()
    p = tennis_model.predict("Strong", "Weak", "Hard")
    assert p is not None
    assert p.home_win > 0.95  # huge gap
    assert p.away_win < 0.05
    assert p.home_win + p.away_win == pytest.approx(1.0, abs=1e-6)


def test_predict_surface_swap_changes_outcome(isolated_cache_and_path):
    """A clay specialist beats a hard-court specialist on clay, vice-versa on hard.
    Requires both players to have enough surface matches for the surface rating
    to actually carry weight in the blend."""
    ratings = {
        "ClayKing": PlayerRating(name="ClayKing", overall=1900,
                                 hard=1750, clay=2100, grass=1700,
                                 matches_hard=25, matches_clay=40, matches_grass=15),
        "HardKing": PlayerRating(name="HardKing", overall=1900,
                                 hard=2100, clay=1750, grass=1700,
                                 matches_hard=40, matches_clay=25, matches_grass=15),
    }
    tennis_model.save_ratings(ratings, path=isolated_cache_and_path)
    tennis_model.reset_cache()
    on_clay = tennis_model.predict("ClayKing", "HardKing", "Clay")
    on_hard = tennis_model.predict("ClayKing", "HardKing", "Hard")
    assert on_clay.home_win > 0.5  # ClayKing favored on clay
    assert on_hard.home_win < 0.5  # ClayKing not favored on hard
