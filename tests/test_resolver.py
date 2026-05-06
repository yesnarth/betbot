"""Unit tests for the result-resolution decision logic. Pure-Python."""
from betbot.resolver import _decide_h2h_outcome


def _scores(home_name, hg, away_name, ag):
    return [
        {"name": home_name, "score": str(hg)},
        {"name": away_name, "score": str(ag)},
    ]


def test_home_wins_with_home_pick():
    out = _decide_h2h_outcome(
        selection_code="1",
        home_team="Arsenal",
        away_team="Chelsea",
        scores=_scores("Arsenal", 2, "Chelsea", 0),
    )
    assert out == "win"


def test_draw_with_draw_pick():
    out = _decide_h2h_outcome(
        selection_code="X",
        home_team="Arsenal",
        away_team="Chelsea",
        scores=_scores("Arsenal", 1, "Chelsea", 1),
    )
    assert out == "win"


def test_away_wins_with_home_pick_returns_loss():
    out = _decide_h2h_outcome(
        selection_code="1",
        home_team="Arsenal",
        away_team="Chelsea",
        scores=_scores("Arsenal", 0, "Chelsea", 2),
    )
    assert out == "loss"


def test_returns_none_when_score_missing():
    out = _decide_h2h_outcome(
        selection_code="1",
        home_team="Arsenal",
        away_team="Chelsea",
        scores=[{"name": "Arsenal", "score": "2"}],   # away missing
    )
    assert out is None


def test_returns_none_when_score_unparseable():
    out = _decide_h2h_outcome(
        selection_code="1",
        home_team="Arsenal",
        away_team="Chelsea",
        scores=[{"name": "Arsenal", "score": "TBD"},
                {"name": "Chelsea", "score": "0"}],
    )
    assert out is None
