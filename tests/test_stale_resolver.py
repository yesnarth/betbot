"""Stale fallback resolver — _resolve_from_results (football-data.org).

Covers the pure matcher that rescues 'zombie' bets too old for the Odds API
/scores window: token-set name matching (Odds API names ≠ football-data names)
plus h2h / totals outcome decision.
"""
from betbot.resolver import _resolve_from_results


def _bet(eid, market, selection, home, away):
    return {
        "event_id": eid, "market": market, "selection": selection,
        "home_team": home, "away_team": away,
    }


def test_resolve_from_results_token_set_match_h2h_and_totals():
    # football-data.org names deliberately differ from the bet (Odds API) names:
    #   "Bayer 04 Leverkusen" vs "Bayer Leverkusen", "RCD Espanyol" vs "Espanyol".
    parsed = [
        {"home_team": "Bayer 04 Leverkusen", "away_team": "Hamburger SV",
         "home_goals": 1, "away_goals": 1},   # total 2 → Under 2.5 wins
        {"home_team": "CA Osasuna", "away_team": "RCD Espanyol",
         "home_goals": 0, "away_goals": 2},   # away win → selection '2' wins
    ]
    pending = [
        _bet("e1", "totals", "U25", "Bayer Leverkusen", "Hamburger SV"),
        _bet("e2", "h2h", "2", "CA Osasuna", "Espanyol"),
        _bet("e3", "h2h", "1", "Unknown FC", "Phantom FC"),   # no result → skip
    ]
    out = _resolve_from_results(pending, parsed)
    decided = {eid: outcome for eid, _m, _s, outcome in out}

    assert decided["e1"] == "win"      # U25, total 2 < 2.5
    assert decided["e2"] == "win"      # picked away, away won 0-2
    assert "e3" not in decided         # unmatched bet stays pending (safe)


def test_resolve_from_results_records_losses_not_just_wins():
    parsed = [
        {"home_team": "Inter", "away_team": "Juventus FC",
         "home_goals": 3, "away_goals": 0},   # home win, total 3
    ]
    pending = [
        _bet("l1", "h2h", "2", "Inter", "Juventus"),     # picked away, home won → loss
        _bet("l2", "totals", "U25", "Inter", "Juventus"),  # total 3 ≥ 2.5 → loss
        _bet("l3", "totals", "O25", "Inter", "Juventus"),  # total 3 > 2.5 → win
    ]
    out = _resolve_from_results(pending, parsed)
    decided = {eid: outcome for eid, _m, _s, outcome in out}

    assert decided["l1"] == "loss"
    assert decided["l2"] == "loss"
    assert decided["l3"] == "win"
    # every matched bet is reported exactly once (no dupes, none dropped)
    assert len(out) == 3


def test_resolve_from_results_skips_incomplete_and_unhandled_markets():
    parsed = [
        {"home_team": "Arsenal FC", "away_team": "Chelsea FC",
         "home_goals": 2, "away_goals": 1},
        {"home_team": "Bad Row", "away_team": None,            # malformed → skipped
         "home_goals": 1, "away_goals": 0},
    ]
    pending = [
        _bet("u1", "spreads", "H-1", "Arsenal", "Chelsea"),   # market we don't settle
        _bet("u2", "h2h", "1", "Arsenal", "Chelsea"),         # home win → win
    ]
    out = _resolve_from_results(pending, parsed)
    decided = {eid: outcome for eid, _m, _s, outcome in out}

    assert "u1" not in decided          # spreads not handled
    assert decided["u2"] == "win"


def test_resolve_from_results_alias_athletic_bilbao():
    # Regression for the one zombie the token-set matcher missed: football-data
    # calls "Athletic Bilbao" → "Athletic Club" (only the 'athletic' token is
    # shared, so neither name is a subset of the other). Resolved via the
    # _KNOWN_ALIASES tier of _fuzzy_lookup.
    parsed = [
        {"home_team": "Athletic Club", "away_team": "RC Celta de Vigo",
         "home_goals": 1, "away_goals": 0},   # home win → selection '2' loses
    ]
    pending = [_bet("z220", "h2h", "2", "Athletic Bilbao", "Celta Vigo")]
    out = _resolve_from_results(pending, parsed)
    decided = {eid: outcome for eid, _m, _s, outcome in out}
    assert decided["z220"] == "loss"


def test_resolve_from_results_empty_inputs():
    assert _resolve_from_results([], []) == []
    assert _resolve_from_results([_bet("x", "h2h", "1", "A", "B")], []) == []
