"""Tests for Over/Under (totals) settlement in the resolver — Wave 1, item 1.4.

Before this, the resolver only settled h2h; totals bets the model proposed
stayed result=NULL forever (committed capital locked). These cover the new
`_decide_totals_outcome`.
"""
from betbot.resolver import _decide_totals_outcome


def _scores(home: int, away: int) -> list[dict]:
    return [{"name": "Home", "score": str(home)}, {"name": "Away", "score": str(away)}]


def test_over_25_wins_on_three_goals():
    assert _decide_totals_outcome("O25", _scores(2, 1)) == "win"    # total 3 > 2.5


def test_over_25_loses_on_two_goals():
    assert _decide_totals_outcome("O25", _scores(1, 1)) == "loss"   # total 2 < 2.5


def test_under_25_wins_on_two_goals():
    assert _decide_totals_outcome("U25", _scores(1, 1)) == "win"


def test_under_25_loses_on_three_goals():
    assert _decide_totals_outcome("U25", _scores(2, 1)) == "loss"


def test_other_lines_15_and_35():
    assert _decide_totals_outcome("O15", _scores(1, 1)) == "win"    # 2 > 1.5
    assert _decide_totals_outcome("O35", _scores(2, 1)) == "loss"   # 3 < 3.5
    assert _decide_totals_outcome("U35", _scores(2, 1)) == "win"    # 3 < 3.5


def test_missing_or_bad_score_returns_none():
    # Only one team's score known → not settleable yet.
    assert _decide_totals_outcome("O25", [{"name": "Home", "score": "2"}]) is None
    # Non-numeric score → not settleable.
    assert _decide_totals_outcome(
        "O25", [{"name": "Home", "score": "x"}, {"name": "Away", "score": "1"}]
    ) is None


def test_malformed_selection_returns_none():
    assert _decide_totals_outcome("ZZ", _scores(2, 1)) is None   # not Over/Under
    assert _decide_totals_outcome("O", _scores(2, 1)) is None    # no line digits
