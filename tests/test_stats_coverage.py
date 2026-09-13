"""
Closing the two structural holes in Poisson coverage (measured 2026-08-07:
22 of 46 in-season leagues had no team stats and ran on market consensus).

Neither hole was a data-availability problem — the data was already paid for
and, in the cup case, already in the database.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# 1. Season boundary: every autumn-spring league is blind for ~2 months
# ---------------------------------------------------------------------------

def _af(monkeypatch, by_season: dict[int, int]):
    """Stub api-football returning N synthetic finished matches per season."""
    import betbot.stats_inseason as mod

    def fake(league_id, season, **kw):
        return [
            {"home_team": f"T{i % 6}", "away_team": f"T{(i + 1) % 6}",
             "home_goals": 1, "away_goals": 0,
             "date": f"{season}-0{i % 9 + 1}-01T00:00:00Z"}
            for i in range(by_season.get(season, 0))
        ]

    monkeypatch.setattr(mod.api_football, "get_finished_matches", fake)
    return mod


def test_thin_current_season_falls_back_to_the_previous_one(monkeypatch):
    """Measured in production: Belgium had 0 finished matches in season 2026
    and 319 in 2025 — and was skipped entirely, degrading to consensus."""
    mod = _af(monkeypatch, {2026: 0, 2025: 319})

    matches, label, weight = mod._load_matches(144, 2026, min_matches=30)

    assert len(matches) == 319
    assert label == "2025+2026"
    assert weight == pytest.approx(0.75), "pure carry-over is shrunk the most"


def test_a_full_current_season_never_borrows_from_last_year(monkeypatch):
    mod = _af(monkeypatch, {2026: 200, 2025: 300})

    matches, label, weight = mod._load_matches(71, 2026, min_matches=30)

    assert len(matches) == 200
    assert label == "2026"
    assert weight == 1.0, "no shrink when this season stands on its own"


def test_partial_season_blends_and_shrinks_less(monkeypatch):
    """Mexico: 27 matches in 2026, 336 in 2025 — just under the threshold."""
    mod = _af(monkeypatch, {2026: 27, 2025: 336})

    matches, label, weight = mod._load_matches(262, 2026, min_matches=30)

    assert len(matches) == 363, "both seasons, recency handled by time-weighting"
    assert label == "2025+2026"
    assert 0.75 < weight < 1.0, "closer to full season => less shrink"


def test_no_history_at_all_stays_skipped(monkeypatch):
    """A genuinely new competition must not be invented out of nothing."""
    mod = _af(monkeypatch, {2026: 3, 2025: 0})

    matches, label, weight = mod._load_matches(999, 2026, min_matches=30)

    assert len(matches) == 3, "still below min_matches -> caller skips it"
    assert label == "2026"


@pytest.mark.parametrize("coef,weight,expected", [
    (1.40, 0.75, 1.30),   # strong attack, carried over -> pulled toward average
    (0.60, 0.75, 0.70),   # weak attack -> pulled up
    (1.40, 1.00, 1.40),   # current season -> untouched
    (1.00, 0.75, 1.00),   # already average -> nothing to shrink
])
def test_shrink_pulls_toward_league_average(coef, weight, expected):
    from betbot.stats_inseason import _shrink

    assert _shrink(coef, weight) == pytest.approx(expected)


# ---------------------------------------------------------------------------
# 2. Cups: the entrants' stats were already in the database
# ---------------------------------------------------------------------------

def test_cups_declare_their_feeder_leagues():
    from betbot.sport_keys import parent_leagues

    assert "soccer_epl" in parent_leagues("soccer_fa_cup")
    assert "soccer_germany_bundesliga" in parent_leagues("soccer_germany_dfb_pokal")
    assert set(parent_leagues("soccer_concacaf_leagues_cup")) == {
        "soccer_usa_mls", "soccer_mexico_ligamx"}


def test_ordinary_leagues_have_no_parents():
    from betbot.sport_keys import parent_leagues

    assert parent_leagues("soccer_epl") == ()
    assert parent_leagues("soccer_brazil_campeonato") == ()


def test_national_team_competition_is_not_faked():
    """The Nations League is played by NATIONAL teams — no club stats exist
    for them. Borrowing club form would be a silent lie."""
    from betbot.sport_keys import parent_leagues

    assert parent_leagues("soccer_uefa_nations_league") == ()


def _row(name, matches=30, attack=1.2):
    return {"team_name": name, "attack_home": attack, "defense_home": 1.0,
            "attack_away": 1.0, "defense_away": 1.0, "matches_analyzed": matches,
            "elo_rating": None, "xg_for": None, "xg_against": None}


def _db_with(stats_by_league, averages=None, h2h=None):
    db = MagicMock()
    db.get_all_team_stats_for_league.side_effect = \
        lambda k: stats_by_league.get(k, [])
    db.get_league_averages.side_effect = lambda k: (averages or {}).get(k)
    db.get_all_h2h_for_league.side_effect = lambda k: (h2h or {}).get(k, {})
    return db


def test_cup_tie_finds_its_teams_in_the_domestic_leagues():
    """Arsenal plays the FA Cup; its coefficients live under soccer_epl.
    Before this, every cup tie fell through to the market consensus."""
    from betbot.shared import load_team_stats_from_db

    db = _db_with({
        "soccer_fa_cup": [],
        "soccer_epl": [_row("Arsenal"), _row("Man City")],
        "soccer_efl_champ": [_row("Leeds")],
    })
    out = load_team_stats_from_db(db, ["soccer_fa_cup"])

    assert "soccer_fa_cup" in out
    assert set(out["soccer_fa_cup"]["teams"]) == {"Arsenal", "Man City", "Leeds"}


def test_promoted_club_keeps_the_better_supported_rating():
    """A club sits in two tiers after promotion; take the deeper sample."""
    from betbot.shared import load_team_stats_from_db

    db = _db_with({
        "soccer_fa_cup": [],
        "soccer_epl": [_row("Leeds", matches=8, attack=1.5)],
        "soccer_efl_champ": [_row("Leeds", matches=46, attack=1.1)],
    })
    out = load_team_stats_from_db(db, ["soccer_fa_cup"])

    assert out["soccer_fa_cup"]["teams"]["Leeds"].matches_analyzed == 46


def test_cup_scoring_level_comes_from_its_feeder_leagues():
    """Better than the global 1.35/1.10 default for a Bundesliga cup tie."""
    from betbot.shared import load_team_stats_from_db

    db = _db_with(
        {"soccer_germany_dfb_pokal": [],
         "soccer_germany_bundesliga": [_row("Bayern")],
         "soccer_germany_bundesliga2": [_row("HSV")]},
        averages={"soccer_germany_bundesliga": (1.8, 1.4),
                  "soccer_germany_bundesliga2": (1.6, 1.2)},
    )
    out = load_team_stats_from_db(db, ["soccer_germany_dfb_pokal"])

    assert out["soccer_germany_dfb_pokal"]["home_avg"] == pytest.approx(1.7)
    assert out["soccer_germany_dfb_pokal"]["away_avg"] == pytest.approx(1.3)


def test_league_meetings_inform_cup_head_to_head():
    from betbot.shared import load_team_stats_from_db

    pair = ("Arsenal", "Man City")
    db = _db_with(
        {"soccer_fa_cup": [], "soccer_epl": [_row("Arsenal"), _row("Man City")]},
        h2h={"soccer_epl": {pair: {"team_a_wins": 3, "draws": 1,
                                   "team_b_wins": 2, "team_a_goals_avg": 1.5,
                                   "team_b_goals_avg": 1.2}}},
    )
    out = load_team_stats_from_db(db, ["soccer_fa_cup"])

    assert pair in out["soccer_fa_cup"]["h2h"]


def test_ordinary_league_loading_is_unchanged():
    """The cup path must not perturb the 24 leagues that already worked."""
    from betbot.shared import load_team_stats_from_db

    db = _db_with({"soccer_epl": [_row("Arsenal")]},
                  averages={"soccer_epl": (1.5, 1.2)})
    out = load_team_stats_from_db(db, ["soccer_epl"])

    assert set(out["soccer_epl"]["teams"]) == {"Arsenal"}
    assert out["soccer_epl"]["home_avg"] == 1.5
    assert db.get_all_team_stats_for_league.call_count == 1, "no extra queries"


# ---------------------------------------------------------------------------
# 3. The league-id map is a loaded gun: a wrong id trains on another sport
# ---------------------------------------------------------------------------

def test_no_league_id_is_used_twice():
    """Two sport_keys pointing at the same api-football id means one of them
    is silently modelled on the other's results. Caught exactly this while
    adding the second tiers: Turkey was already mapped further down."""
    from betbot.data_sources.api_football import IN_SEASON_LEAGUE_ID

    ids = list(IN_SEASON_LEAGUE_ID.values())
    assert len(ids) == len(set(ids)), "duplicate api-football league id"


def test_second_tiers_are_mapped_and_not_confused_with_lookalikes():
    """79 is 2. Bundesliga (1034 is the women's league); 141 is Segunda
    División (875-877 are the RFEF regional groups)."""
    from betbot.data_sources.api_football import IN_SEASON_LEAGUE_ID as M

    assert M["soccer_germany_bundesliga2"] == 79
    assert M["soccer_spain_segunda_division"] == 141
    assert M["soccer_england_league1"] == 41
    assert M["soccer_england_league2"] == 42
    assert M["soccer_france_ligue_two"] == 62
    assert M["soccer_germany_liga3"] == 80


def test_cup_never_uses_its_own_thin_scoring_average():
    """Measured in production: the EFL Cup's own league_averages row read
    1.31 home / 1.51 away — an INVERTED home advantage, from a few dozen
    tier-mixed ties. The feeder leagues rest on full seasons."""
    from betbot.shared import load_team_stats_from_db

    db = _db_with(
        {"soccer_england_efl_cup": [_row("Arsenal")],
         "soccer_epl": [_row("Arsenal")], "soccer_efl_champ": [_row("Leeds")],
         "soccer_england_league1": [], "soccer_england_league2": []},
        averages={"soccer_england_efl_cup": (1.31, 1.51),   # artefact
                  "soccer_epl": (1.50, 1.20),
                  "soccer_efl_champ": (1.40, 1.10)},
    )
    out = load_team_stats_from_db(db, ["soccer_england_efl_cup"])

    assert out["soccer_england_efl_cup"]["home_avg"] == pytest.approx(1.45)
    assert out["soccer_england_efl_cup"]["away_avg"] == pytest.approx(1.15)
    assert (out["soccer_england_efl_cup"]["home_avg"]
            > out["soccer_england_efl_cup"]["away_avg"]), "home advantage restored"
