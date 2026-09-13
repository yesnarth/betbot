"""
Regression tests for the four model defects found in the 2026-08-01 production
data (310 picks, 2026-07-20 → 07-30).

Each test pins a defect that was silently costing ROI, with the measurement
that exposed it in the docstring.
"""
from __future__ import annotations

import pytest

from betbot.analysis import _derive_dc_dnb_odds
from betbot.calibration import is_edge_suspicious
from betbot.data_sources.club_elo import elo_expected_score, elo_no_loss_probability
from betbot.models import build_team_stats


# ---------------------------------------------------------------------------
# 1. Recency ordering is enforced, not assumed
# ---------------------------------------------------------------------------

def _match(home, away, hg, ag, date):
    return {"home_team": home, "away_team": away,
            "home_goals": hg, "away_goals": ag, "date": date}


def _team_x_history():
    """Team X scored 0 a year ago, scores 4 now."""
    old = [_match("X", "Y", 0, 2, f"2025-09-{d:02d}T00:00:00Z") for d in range(1, 6)]
    new = [_match("X", "Z", 4, 0, f"2026-07-{d:02d}T00:00:00Z") for d in range(1, 6)]
    return old, new


def test_input_order_does_not_change_team_stats():
    """`_exp_weight(k)` gives weight 1.0 to index 0, so list order silently
    decided the estimate. `football_api.parse_match_results` returns ASCENDING
    chronological order, so the OLDEST match carried weight 1.0 and the newest
    0.165 — measured deviation of 0.148 on attack_home across the 20 EPL teams.
    """
    old, new = _team_x_history()
    chronological = old + new
    # A genuine permutation of the SAME 10 matches — interleaved, deterministic.
    interleaved = [m for pair in zip(old, new) for m in pair]
    assert sorted(map(id, interleaved)) == sorted(map(id, chronological))

    ascending = build_team_stats("X", chronological, 1.4, 1.1)
    descending = build_team_stats("X", new + old, 1.4, 1.1)
    shuffled = build_team_stats("X", interleaved, 1.4, 1.1)

    assert ascending.attack_home == pytest.approx(descending.attack_home)
    assert ascending.attack_home == pytest.approx(shuffled.attack_home)


def test_recent_form_dominates_stale_form():
    """The whole point of exponential decay: 4 goals last month must outweigh
    0 goals last September."""
    old, new = _team_x_history()
    stats = build_team_stats("X", old + new, 1.4, 1.1)
    # League home average is 1.4; a team scoring 4 recently must be well above it.
    assert stats.attack_home > 1.0


def test_matches_without_a_date_sort_last_not_first():
    """A missing date must not land at index 0 and capture the full weight."""
    _, new = _team_x_history()
    undated = _match("X", "W", 0, 5, "")
    stats = build_team_stats("X", [undated] + new, 1.4, 1.1)
    only_recent = build_team_stats("X", new, 1.4, 1.1)
    # The 0-goal undated match must dilute only mildly, never dominate.
    assert stats.attack_home > only_recent.attack_home * 0.75


# ---------------------------------------------------------------------------
# 2. Elo expected score is not a no-loss probability
# ---------------------------------------------------------------------------

def test_elo_expected_score_is_one_half_for_equal_teams():
    assert elo_expected_score(1700, 1700, home_advantage=0.0) == pytest.approx(0.5)


@pytest.mark.parametrize("draw_prob", [0.22, 0.26, 0.30])
def test_no_loss_probability_adds_back_half_the_draw(draw_prob):
    """For two equal teams, P(no loss) = 0.5*(1-d) + d = 0.5 + 0.5*d.

    The old code fed the expected score (0.5) straight into the no-loss slot,
    losing exactly 0.5*d — and since away_win is computed as the residual
    `1 - home_win - draw`, that deficit was handed to the away side on 100% of
    matches. Production corroboration: the bot took the away side 14 times
    (4W/8L) against 9 home picks (4W/0L).
    """
    got = elo_no_loss_probability(1700, 1700, draw_prob=draw_prob, home_advantage=0.0)
    expected = 0.5 * (1 - draw_prob) + draw_prob
    assert got == pytest.approx(expected)
    assert got - elo_expected_score(1700, 1700, home_advantage=0.0) == pytest.approx(0.5 * draw_prob)


def test_no_loss_probability_stays_in_range_for_a_heavy_favourite():
    assert elo_no_loss_probability(2200, 1400, draw_prob=0.30) <= 1.0
    assert elo_no_loss_probability(1400, 2200, draw_prob=0.30) >= 0.0


# ---------------------------------------------------------------------------
# 3. Draw No Bet must carry the book's margin, like Double Chance does
# ---------------------------------------------------------------------------

def _event_with_1x2(o1: float, ox: float, o2: float) -> dict:
    def book(key, title):
        return {
            "key": key, "title": title,
            "markets": [{"key": "h2h", "outcomes": [
                {"name": "Lyon", "price": o1},
                {"name": "Draw", "price": ox},
                {"name": "Nice", "price": o2},
            ]}],
        }
    # Two books required: the derivation needs a cross-book consensus (n >= 2).
    return {"bookmakers": [book("betclic", "Betclic (FR)"), book("bet365", "Bet365")]}


@pytest.fixture(autouse=True)
def _no_whitelist(monkeypatch):
    monkeypatch.delenv("BOOKMAKER_WHITELIST", raising=False)


def test_dnb_price_is_below_the_true_fair_price():
    """`(q1+q2)/q1` is a RATIO of margin-carrying implieds, so the margin
    cancels and the derived price is exactly true-fair — a price no bookmaker
    offers. Double Chance sums implieds, so its margin survives.

    Production: double_chance showed +4.5..+7.7% edge and was calibrated
    (68% predicted / 68% realised); draw_no_bet showed +23.9..+27.3% and was
    21 points overconfident (66% / 45%).
    """
    o1, ox, o2 = 2.10, 3.40, 3.60
    q1, qx, q2 = 1 / o1, 1 / ox, 1 / o2
    overround = q1 + qx + q2
    assert overround > 1.0, "fixture must carry a real bookmaker margin"

    derived, _ = _derive_dc_dnb_odds(_event_with_1x2(o1, ox, o2), "Lyon", "Nice")

    true_fair_dnb1 = (q1 + q2) / q1
    assert derived["DNB1"].price < true_fair_dnb1
    # `_derive_dc_dnb_odds` stores prices rounded to 3 decimals.
    assert derived["DNB1"].price == pytest.approx(true_fair_dnb1 / overround, abs=5e-4)


def test_dnb_and_dc_carry_the_same_conservatism_factor():
    """Both markets must be quoted at `fair_probability × overround`, so the
    model faces the same hurdle whichever one it picks."""
    o1, ox, o2 = 2.10, 3.40, 3.60
    q1, qx, q2 = 1 / o1, 1 / ox, 1 / o2
    overround = q1 + qx + q2

    derived, _ = _derive_dc_dnb_odds(_event_with_1x2(o1, ox, o2), "Lyon", "Nice")

    dnb1_implied = 1.0 / derived["DNB1"].price
    dnb1_fair = q1 / (q1 + q2)
    dc_1x_implied = 1.0 / derived["1X"].price
    dc_1x_fair = (q1 + qx) / overround

    # Tolerance covers the 3-decimal rounding applied to stored prices.
    assert dnb1_implied / dnb1_fair == pytest.approx(overround, abs=1e-3)
    assert dc_1x_implied / dc_1x_fair == pytest.approx(overround, abs=1e-3)


def test_sub_hundred_book_does_not_inflate_the_dnb_price():
    """A book quoting under 100% (arb or stale data) must not turn the
    overround correction into a bonus. Odds are deliberately asymmetric so the
    degenerate-line guard below does not fire instead."""
    o1, ox, o2 = 3.40, 4.20, 3.90
    derived, _ = _derive_dc_dnb_odds(_event_with_1x2(o1, ox, o2), "Lyon", "Nice")
    q1, q2 = 1 / o1, 1 / o2
    # 1e-3 covers the 3-decimal rounding applied when the price is stored.
    assert derived["DNB1"].price <= (q1 + q2) / q1 + 1e-3


def test_symmetric_placeholder_line_is_rejected_before_derivation():
    """A perfectly symmetric 1X2 is a market the book has not really made. It
    derives to DNB = 2.000 exactly, which reads as an enormous edge against any
    model. Production found 25 such rows — all Betfair, all DNB at exactly
    2.000, average claimed edge 57.8%, and 0/25 ever resolved.

    The pre-existing median×1.20 outlier guard cannot catch these: it fails
    precisely when SEVERAL books publish the same placeholder.
    """
    derived, consensus = _derive_dc_dnb_odds(
        _event_with_1x2(3.60, 4.20, 3.60), "Lyon", "Nice"
    )
    assert derived == {}, "a symmetric placeholder must yield no derived price"
    assert consensus == {}


def test_near_symmetric_line_within_tolerance_is_also_rejected():
    """1% apart is still a placeholder, not a made market."""
    derived, _ = _derive_dc_dnb_odds(_event_with_1x2(3.60, 4.20, 3.63), "Lyon", "Nice")
    assert "DNB1" not in derived


def test_genuinely_balanced_but_made_market_survives():
    """5% apart is a real, merely balanced fixture — it must NOT be dropped."""
    derived, _ = _derive_dc_dnb_odds(_event_with_1x2(3.40, 4.20, 3.60), "Lyon", "Nice")
    assert "DNB1" in derived and "1X" in derived


# ---------------------------------------------------------------------------
# 4. The edge cap is wired to the production path
# ---------------------------------------------------------------------------

def test_edge_cap_threshold_semantics():
    """Production 2026-08-01, by claimed-edge bracket (void excluded):
        <10%    n=71  overconfidence  +8.0 pts  ROI  -6.7%
        10-15%  n=52  overconfidence +17.4 pts  ROI -27.1%
        15-20%  n=23  overconfidence +16.3 pts  ROI -20.0%
        20-30%  n=12  overconfidence +20.2 pts  ROI -29.4%
        >30%    n=12  overconfidence +18.5 pts  ROI -15.0%
    A large claimed edge measures the size of the model's error, not an
    opportunity — the winner's curse.
    """
    assert is_edge_suspicious(0.20, 0.15) is True
    assert is_edge_suspicious(0.15, 0.15) is False
    assert is_edge_suspicious(0.08, 0.15) is False


def test_detect_value_bets_accepts_max_value_edge():
    """The parameter must exist on the production entry point — `is_edge_suspicious`
    lived in calibration.py from day one and was never called from it."""
    import inspect

    from betbot.analysis import detect_value_bets

    params = inspect.signature(detect_value_bets).parameters
    assert "max_value_edge" in params
    assert params["max_value_edge"].default == 0.0, "must stay opt-in"

# ---------------------------------------------------------------------------
# 5. The Over gate must actually run through detect_value_bets
# ---------------------------------------------------------------------------

def _football_event() -> dict:
    def book(key, title):
        return {
            "key": key, "title": title,
            "markets": [
                {"key": "h2h", "outcomes": [
                    {"name": "Lyon", "price": 2.10},
                    {"name": "Draw", "price": 3.40},
                    {"name": "Nice", "price": 3.60},
                ]},
                {"key": "totals", "outcomes": [
                    {"name": "Over", "price": 1.90, "point": 2.5},
                    {"name": "Under", "price": 1.95, "point": 2.5},
                    {"name": "Over", "price": 1.30, "point": 1.5},
                    {"name": "Under", "price": 3.20, "point": 1.5},
                ]},
            ],
        }
    return {
        "id": "evt-1", "home_team": "Lyon", "away_team": "Nice",
        "commence_time": "2026-08-02T18:00:00Z",
        "bookmakers": [book("betclic", "Betclic (FR)"), book("bet365", "Bet365")],
    }


def _run_scan(allow_over: bool):
    from betbot.analysis import detect_value_bets
    from betbot.models import TeamStats

    def _ts(name):
        return TeamStats(name=name, attack_home=1.20, defense_home=1.0,
                         attack_away=1.20, defense_away=1.0, matches_analyzed=30)

    return detect_value_bets(
        events_by_sport={"soccer_france_ligue1": [_football_event()]},
        match_history_by_sport={},
        # Team stats are now REQUIRED for totals: since `_prob_to_lambda` was
        # fixed the consensus model reads the goals level from the totals
        # market, reproduces the price, and is barred from this market
        # entirely. Without stats here the gate under test would never be
        # reached — the scan would return no totals for a different reason.
        prebuilt_stats_by_sport={"soccer_france_ligue1": {
            "teams": {"Lyon": _ts("Lyon"), "Nice": _ts("Nice")},
            "home_avg": 1.55, "away_avg": 1.25, "h2h": {},
        }},
        bankroll=100.0,
        min_value_edge=-1.0,      # keep everything so the gate is the only filter
        min_model_prob=0.0,
        min_model_prob_totals=0.0,
        min_book_odds=1.0,
        allow_totals_over=allow_over,
        require_positive_stake=False,
    )


def test_over_gate_runs_without_raising():
    """Regression: the gate referenced a variable name that does not exist
    (`specs` instead of `outcome_map`), so every football scan raised
    UnboundLocalError and the endpoint returned 500. No test exercised
    detect_value_bets with allow_totals_over=False, so it shipped."""
    assert _run_scan(False) is not None


def test_gate_hits_over_harder_than_under(monkeypatch):
    """The gate is no longer Over-only.

    It was, and that left an equally edgeless Under market running at full
    flow (n=45, ROI -22.5%). Now both directions are throttled, Over harder
    than Under because the evidence against Over is far stronger
    (t=-4.33 vs t=-1.55).
    """
    monkeypatch.setenv("OVER_SAMPLING_RATE", "0")
    monkeypatch.setenv("UNDER_SAMPLING_RATE", "1")
    codes = {b.selection_code for b in _run_scan(False)}
    assert not {c for c in codes if c.startswith("O")}, "no Over at rate 0"
    assert {c for c in codes if c.startswith("U")}, "every Under at rate 1"


def test_overs_present_when_the_gate_is_open():
    codes = {b.selection_code for b in _run_scan(True)}
    assert {c for c in codes if c.startswith("O")}, "gate open → Overs expected"


# ---------------------------------------------------------------------------
# 6. The Over gate must stay falsifiable
# ---------------------------------------------------------------------------

def test_sampling_is_deterministic_per_event(monkeypatch):
    """A rescan of the same match must reach the same verdict.

    Random sampling would let repeated scans accumulate duplicate Over picks on
    the lucky draws, turning a measurement contingent into cherry-picking.
    """
    monkeypatch.setenv("OVER_SAMPLING_RATE", "0.25")
    from betbot.analysis import _keep_totals_sample

    for event_id in ("evt-1", "evt-abc", "12345"):
        first = _keep_totals_sample(event_id, "Over")
        assert all(_keep_totals_sample(event_id, "Over") is first for _ in range(20))


def test_sampling_rate_is_respected(monkeypatch):
    monkeypatch.setenv("OVER_SAMPLING_RATE", "0.25")
    from betbot.analysis import _keep_totals_sample

    ids = [f"evt-{i}" for i in range(4000)]
    share = sum(_keep_totals_sample(i, "Over") for i in ids) / len(ids)
    assert 0.22 < share < 0.28


@pytest.mark.parametrize("rate,expect_any", [("0", False), ("1", True)])
def test_sampling_extremes(monkeypatch, rate, expect_any):
    monkeypatch.setenv("OVER_SAMPLING_RATE", rate)
    from betbot.analysis import _keep_totals_sample, _totals_sampling_rate

    ids = [f"evt-{i}" for i in range(200)]
    assert bool(any(_keep_totals_sample(i, "Over") for i in ids)) is expect_any
    assert _totals_sampling_rate("Over") == float(rate)


def test_gate_off_lets_every_over_through(monkeypatch):
    monkeypatch.delenv("OVER_SAMPLING_RATE", raising=False)
    codes = {b.selection_code for b in _run_scan(True)}
    assert {c for c in codes if c.startswith("O")}


def test_gate_on_with_zero_rate_is_a_hard_cut(monkeypatch):
    """The historical behaviour stays reachable — some users may want it."""
    monkeypatch.setenv("OVER_SAMPLING_RATE", "0")
    codes = {b.selection_code for b in _run_scan(False)}
    assert not {c for c in codes if c.startswith("O")}

def test_under_keeps_a_larger_contingent_than_over(monkeypatch):
    """The rates differ because the EVIDENCE differs, not the market.

    On 87 graded totals picks: Over n=42 ROI -52.0% t=-4.33 (established),
    Under n=45 ROI -22.5% t=-1.55 (clearly negative, not significant). Under
    therefore keeps more flow while the question stays open.
    """
    monkeypatch.delenv("OVER_SAMPLING_RATE", raising=False)
    monkeypatch.delenv("UNDER_SAMPLING_RATE", raising=False)
    from betbot.analysis import _totals_sampling_rate

    assert _totals_sampling_rate("Over") == 0.25
    assert _totals_sampling_rate("Under") == 0.50


def test_over_and_under_sample_independently(monkeypatch):
    """Direction is part of the hash key. Hashing the event alone would tie the
    two decisions together, so a match kept for Over would always be kept for
    Under too — the contingent would then measure one population, not two."""
    monkeypatch.setenv("OVER_SAMPLING_RATE", "0.5")
    monkeypatch.setenv("UNDER_SAMPLING_RATE", "0.5")
    from betbot.analysis import _keep_totals_sample

    ids = [f"evt-{i}" for i in range(3000)]
    both = sum(
        _keep_totals_sample(i, "Over") and _keep_totals_sample(i, "Under")
        for i in ids
    ) / len(ids)
    assert 0.21 < both < 0.29, "independent draws => ~0.5*0.5"


def test_gate_reduces_unders_too(monkeypatch):
    """The cut used to be Over-only, leaving an equally edgeless Under market
    running at full flow."""
    monkeypatch.setenv("OVER_SAMPLING_RATE", "0")
    monkeypatch.setenv("UNDER_SAMPLING_RATE", "0")
    codes = {b.selection_code for b in _run_scan(False)}
    assert not {c for c in codes if c[0] in "OU"}, "no totals selection may survive"
