"""Double Chance + Draw No Bet — derived markets (zero extra API quota).

Covers the three places a bug could hide:
  1. odds derivation from a book's own 1X2 (vig-inclusive, no free money)
  2. settlement (DC = win/loss ; DNB = win/loss/VOID-on-draw → refund)
  3. surfacing through detect_value_bets + the derive_dc_dnb toggle
"""
from betbot.analysis import detect_value_bets, _derive_dc_dnb_odds
from betbot.resolver import _decide_dc_outcome, _decide_dnb_outcome
from betbot.reliability import compute_reliability


def _h2h(home, draw, away, key="pinnacle", title="P"):
    return {"key": key, "title": title, "markets": [{"key": "h2h", "outcomes": [
        {"name": "Alpha", "price": home},
        {"name": "Draw", "price": draw},
        {"name": "Beta", "price": away},
    ]}]}


# ---------------------------------------------------------------------------
# 1. Odds derivation
# ---------------------------------------------------------------------------

def test_derive_dc_dnb_odds_formula():
    event = {"home_team": "Alpha", "away_team": "Beta",
             "bookmakers": [_h2h(2.0, 4.0, 4.0)]}
    d = _derive_dc_dnb_odds(event, "Alpha", "Beta")
    # q1=0.5 qX=0.25 q2=0.25
    assert round(d["1X"].price, 3) == 1.333       # 1/(0.5+0.25)
    assert round(d["X2"].price, 3) == 2.0         # 1/(0.25+0.25)
    assert round(d["12"].price, 3) == 1.333       # 1/(0.5+0.25)
    assert round(d["DNB1"].price, 3) == 1.5       # (0.5+0.25)/0.5
    assert round(d["DNB2"].price, 3) == 3.0       # (0.5+0.25)/0.25


def test_derive_takes_best_price_across_books():
    event = {"home_team": "Alpha", "away_team": "Beta", "bookmakers": [
        _h2h(2.0, 4.0, 4.0, key="pinnacle", title="P"),
        _h2h(2.4, 4.2, 3.6, key="betclic", title="B"),   # more generous home/draw
    ]}
    d = _derive_dc_dnb_odds(event, "Alpha", "Beta")
    # betclic 1X = 1/(1/2.4 + 1/4.2) = 1/(0.4167+0.2381) = 1.527 > pinnacle 1.333
    assert d["1X"].price > 1.5
    assert d["1X"].bookmaker == "B"


def test_derive_skips_incomplete_1x2():
    # A book missing the Draw price can't derive coherently → ignored.
    event = {"home_team": "Alpha", "away_team": "Beta", "bookmakers": [
        {"key": "x", "title": "X", "markets": [{"key": "h2h", "outcomes": [
            {"name": "Alpha", "price": 2.0}, {"name": "Beta", "price": 3.5},
        ]}]},
    ]}
    assert _derive_dc_dnb_odds(event, "Alpha", "Beta") == {}


# ---------------------------------------------------------------------------
# 2. Settlement
# ---------------------------------------------------------------------------

def _scores(hg, ag):
    return [{"name": "Alpha", "score": str(hg)}, {"name": "Beta", "score": str(ag)}]


def test_dc_outcome():
    # Alpha win (2-1)
    assert _decide_dc_outcome("1X", "Alpha", "Beta", _scores(2, 1)) == "win"
    assert _decide_dc_outcome("12", "Alpha", "Beta", _scores(2, 1)) == "win"
    assert _decide_dc_outcome("X2", "Alpha", "Beta", _scores(2, 1)) == "loss"
    # Draw (1-1)
    assert _decide_dc_outcome("1X", "Alpha", "Beta", _scores(1, 1)) == "win"
    assert _decide_dc_outcome("X2", "Alpha", "Beta", _scores(1, 1)) == "win"
    assert _decide_dc_outcome("12", "Alpha", "Beta", _scores(1, 1)) == "loss"  # 12 excludes draw
    # incomplete
    assert _decide_dc_outcome("1X", "Alpha", "Beta", []) is None


def test_dnb_outcome_void_on_draw():
    # home win → DNB1 win, DNB2 loss
    assert _decide_dnb_outcome("DNB1", "Alpha", "Beta", _scores(3, 0)) == "win"
    assert _decide_dnb_outcome("DNB2", "Alpha", "Beta", _scores(3, 0)) == "loss"
    # away win → mirror
    assert _decide_dnb_outcome("DNB1", "Alpha", "Beta", _scores(0, 2)) == "loss"
    assert _decide_dnb_outcome("DNB2", "Alpha", "Beta", _scores(0, 2)) == "win"
    # draw → BOTH void (stake refunded)
    assert _decide_dnb_outcome("DNB1", "Alpha", "Beta", _scores(1, 1)) == "void"
    assert _decide_dnb_outcome("DNB2", "Alpha", "Beta", _scores(1, 1)) == "void"


# ---------------------------------------------------------------------------
# 3. Reliability + surfacing
# ---------------------------------------------------------------------------

def test_reliability_skips_high_prob_penalty_for_derived():
    common = dict(model_prob=0.85, value_edge=0.03, model_type="blended", n_matches=20)
    penalized = compute_reliability(**common)
    exempt = compute_reliability(**common, skip_extreme_prob_penalty=True)
    assert exempt > penalized                     # 0.85 no longer punished as "overconfident"


def _value_event():
    # One generous book (betclic) inflates the derived DC line above the
    # consensus-implied → a positive DC edge the model can surface.
    return {"id": "e1", "home_team": "Alpha", "away_team": "Beta", "bookmakers": [
        _h2h(2.00, 3.40, 3.80, key="pinnacle", title="P"),
        _h2h(2.02, 3.40, 3.75, key="bet365", title="B365"),
        _h2h(2.35, 3.70, 3.40, key="betclic", title="Betclic"),
    ]}


def test_dc_dnb_surface_and_toggle(monkeypatch):
    monkeypatch.setattr("betbot.analysis.ml_calibrate", lambda p, *a, **k: p)
    events = {"soccer_epl": [_value_event()]}
    on = detect_value_bets(events, {}, bankroll=100.0, min_value_edge=0.04,
                           min_model_prob=0.0, min_book_odds=1.50,
                           derived_min_edge=0.0, derived_min_odds=1.10,
                           require_positive_stake=False)
    assert any(b.market in ("double_chance", "draw_no_bet") for b in on), \
        "derived DC/DNB selections should surface"

    off = detect_value_bets(events, {}, bankroll=100.0, min_value_edge=0.04,
                            min_model_prob=0.0, min_book_odds=1.50,
                            derive_dc_dnb=False, require_positive_stake=False)
    assert not any(b.market in ("double_chance", "draw_no_bet") for b in off), \
        "derive_dc_dnb=False must suppress them"
