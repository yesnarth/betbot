"""
The 05/08 -> 10/08 regression: every pick born at model_prob = 1.000.

Nothing in the model was wrong. The Poisson lambdas were sane throughout
(Vasteras 1.47 vs Djurgardens 1.74 — a close match). The damage came from two
mechanisms that are individually reasonable and catastrophic together:

  1. the isotonic calibrator is fitted on RESOLVED PICKS, which only exist
     above MIN_MODEL_PROB, so its trained domain started at 0.454 — yet it was
     applied to every outcome of the 1X2 distribution, draws and underdogs
     included. Out-of-domain inputs were answered with the first point's y,
     which happened to be 0.0;
  2. the group is renormalised afterwards to keep 1+X+2 == 1, so "two zeros
     and one survivor" became a 100% certainty.

59 of the 60 picks made in that window carried model_prob = 1.000 on matches
the market priced near 1.75 — an edge of +73% conjured out of a division.
"""
from __future__ import annotations

import json

import pytest

from betbot.analysis import ValueBet, detect_value_bets  # noqa: F401
from betbot.models import TeamStats


# ---------------------------------------------------------------------------
# 1. Never calibrate outside the trained domain
# ---------------------------------------------------------------------------

@pytest.fixture
def _calibrator(tmp_path, monkeypatch):
    """Install the EXACT production calibrator that caused the incident."""
    payload = {
        "format": "isotonic-segmented-v1",
        "global": {"x": [0.4074, 0.708, 1.0], "y": [0.0, 0.537, 0.537], "n": 4000},
        "segments": {
            "football_h2h": {
                "x": [0.4544, 0.458, 0.481, 0.496, 1.0],
                "y": [0.0, 0.417, 0.417, 0.5286, 0.5286],
                "n": 4000,
            },
        },
        "trained_at": "2026-08-09T00:00:00+00:00",
        "source": "resolved_bets",
    }
    path = tmp_path / "calibrator.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("CALIBRATOR_PATH", str(path))

    import betbot.ml as ml
    monkeypatch.setattr(ml, "CALIBRATOR_PATH", path)
    monkeypatch.setattr(ml, "_cached_calibrator", None)
    return ml


@pytest.mark.parametrize("prob", [0.17, 0.28, 0.41, 0.45])
def test_below_the_trained_domain_the_probability_is_left_alone(_calibrator, prob):
    """These are ordinary 1X2 members — a draw, an underdog. The calibrator has
    never seen them, so it must not pretend to have an opinion. It used to
    answer 0.0, which is not a correction but an annihilation."""
    assert _calibrator.calibrate(prob, "football_h2h") == prob


def test_inside_the_trained_domain_calibration_still_applies(_calibrator):
    got = _calibrator.calibrate(0.49, "football_h2h")
    assert got != 0.49
    assert 0.4 < got < 0.55


def test_the_old_behaviour_would_have_annihilated_a_normal_match(_calibrator):
    """Guards the regression itself: a plain 1X2 must not collapse to a single
    surviving outcome once renormalised."""
    raw = {"1": 0.41, "X": 0.28, "2": 0.31}
    cal = {k: _calibrator.calibrate(v, "football_h2h") for k, v in raw.items()}
    total = sum(cal.values())

    assert total > 0
    assert max(c / total for c in cal.values()) < 0.95, (
        "no outcome may take the whole mass out of a balanced match"
    )


# ---------------------------------------------------------------------------
# 2. The degeneracy guard, independent of the cause
# ---------------------------------------------------------------------------

def _stats(name, atk):
    return TeamStats(name=name, attack_home=atk, defense_home=1.0,
                     attack_away=atk, defense_away=1.0, matches_analyzed=30)


def _event():
    def price(p, o=1.06):
        return round(1.0 / (p * o), 4)

    def book(k, t):
        return {"key": k, "title": t, "markets": [{"key": "h2h", "outcomes": [
            {"name": "Alpha", "price": price(0.42)},
            {"name": "Draw", "price": price(0.27)},
            {"name": "Beta", "price": price(0.31)},
        ]}]}

    return {"id": "evt-deg", "sport_key": "soccer_epl",
            "commence_time": "2026-08-20T19:00:00Z",
            "home_team": "Alpha", "away_team": "Beta",
            "bookmakers": [book("betclic", "Betclic (FR)"),
                           book("bet365", "Bet365")]}


def _run(monkeypatch, calibrate_fn):
    import betbot.analysis as mod
    monkeypatch.setattr(mod, "ml_calibrate", calibrate_fn)
    return detect_value_bets(
        events_by_sport={"soccer_epl": [_event()]},
        match_history_by_sport={},
        prebuilt_stats_by_sport={"soccer_epl": {
            "teams": {"Alpha": _stats("Alpha", 1.1), "Beta": _stats("Beta", 1.0)},
            "home_avg": 1.45, "away_avg": 1.15, "h2h": {}}},
        bankroll=100.0, min_value_edge=-1.0, min_model_prob=0.0,
        min_book_odds=1.0, max_book_odds=0.0, novig_required=False,
        min_edge_vs_novig=0.0, underdog_odds=0.0,
        derive_dc_dnb=False, allow_totals_over=False,
    )


def test_a_calibrator_that_zeroes_outcomes_cannot_manufacture_certainty(monkeypatch):
    """The exact production failure, injected: everything under 0.45 -> 0.0.
    The guard must fall back to the model's own probabilities rather than hand
    the survivor 100%."""
    bets = _run(monkeypatch, lambda p, seg=None: 0.0 if p < 0.45 else 0.53)

    assert bets, "the match must still produce sellable legs"
    for b in bets:
        assert b.model_prob < 0.99, (
            f"{b.selection_code} at {b.model_prob} — certainty conjured by division"
        )


def test_the_fallback_still_sums_to_one_across_the_group(monkeypatch):
    bets = _run(monkeypatch, lambda p, seg=None: 0.0 if p < 0.45 else 0.53)
    h2h = {b.selection_code: b.model_prob for b in bets if b.market == "h2h"}
    if len(h2h) == 3:
        assert sum(h2h.values()) == pytest.approx(1.0, abs=0.01)


def test_an_all_zero_calibration_falls_back_instead_of_dividing_by_zero(monkeypatch):
    bets = _run(monkeypatch, lambda p, seg=None: 0.0)

    assert bets
    for b in bets:
        assert 0.0 < b.model_prob < 0.99


def test_a_healthy_calibrator_is_left_to_do_its_job(monkeypatch):
    """The guard must not fire on a calibrator that merely reshapes."""
    bets = _run(monkeypatch, lambda p, seg=None: min(1.0, p * 1.1))

    assert bets
    probs = {b.selection_code: b.model_prob for b in bets if b.market == "h2h"}
    assert all(0.0 < v < 0.99 for v in probs.values())


def test_an_outcome_the_model_calls_impossible_may_legitimately_stay_zero(monkeypatch):
    """The guard keys on the RAW probability: only a zero that contradicts the
    model is suspicious. A genuinely impossible outcome is not."""
    from betbot.analysis import _CALIB_ANNIHILATION_FLOOR

    assert 0.0 < _CALIB_ANNIHILATION_FLOOR < 0.10


# ---------------------------------------------------------------------------
# 3. The corruption must not outlive its own fix
# ---------------------------------------------------------------------------

def test_degenerate_picks_are_excluded_from_the_next_training_set():
    """59 rows carry model_prob = 1.000. Training on them would teach the next
    calibrator that certainty means a coin flip."""
    from betbot.ml import _is_usable

    assert not _is_usable(1.0)
    assert not _is_usable(0.0)
    assert _is_usable(0.72)
    assert _is_usable(0.28)


# ---------------------------------------------------------------------------
# 4. All or nothing across a coherent group
# ---------------------------------------------------------------------------

def test_a_group_is_never_half_calibrated(monkeypatch):
    """Correcting the favourite while leaving the draw untouched, then
    renormalising, reshapes the distribution by an amount nobody chose."""
    import betbot.analysis as mod

    seen: list[float] = []

    def _cal(p, seg=None):
        seen.append(p)
        return p * 1.2

    # Only probabilities above 0.45 are inside the fitted domain — exactly the
    # production calibrator's shape.
    monkeypatch.setattr(mod, "_ml_in_domain", lambda p, seg=None: p >= 0.45)
    bets = _run(monkeypatch, _cal)

    h2h = {b.selection_code: b.model_prob for b in bets if b.market == "h2h"}
    if len(h2h) == 3:
        assert sum(h2h.values()) == pytest.approx(1.0, abs=0.01)
    for b in bets:
        assert b.model_prob < 0.99


def test_a_fully_in_domain_group_is_calibrated_normally(monkeypatch):
    import betbot.analysis as mod

    monkeypatch.setattr(mod, "_ml_in_domain", lambda p, seg=None: True)
    calls: list[float] = []

    def _cal(p, seg=None):
        calls.append(p)
        return min(1.0, p * 1.15)

    bets = _run(monkeypatch, _cal)
    assert calls, "calibration must still be attempted"
    assert bets


def test_in_domain_reports_false_without_a_calibrator(monkeypatch, tmp_path):
    """No calibrator means nothing is in domain — so groups stay raw rather
    than being silently half-treated."""
    import betbot.ml as ml

    monkeypatch.setattr(ml, "CALIBRATOR_PATH", tmp_path / "absent.json")
    monkeypatch.setattr(ml, "_cached_calibrator", None)
    assert ml.in_domain(0.5, "football_h2h") is False
