"""Rest / fixture-congestion attack factor (pure logic + graceful defaults)."""
from datetime import datetime, timezone

from betbot.fatigue import (
    freshness_factor_from_dates, get_fatigue_factor, FATIGUE_MIN_FACTOR,
)

UTC = timezone.utc


def _dt(y, m, d):
    return datetime(y, m, d, tzinfo=UTC)


def test_well_rested_is_neutral():
    # one match a week ago → fresh
    assert freshness_factor_from_dates([_dt(2026, 9, 13)], _dt(2026, 9, 20)) == 1.0


def test_short_rest_penalised_but_bounded():
    f = freshness_factor_from_dates([_dt(2026, 9, 18)], _dt(2026, 9, 20))  # 2 days rest
    assert FATIGUE_MIN_FACTOR <= f < 1.0


def test_heavy_congestion_floors_at_min():
    # 4 matches in 12 days ending 2 days before kickoff → rest + congestion penalties
    dates = [_dt(2026, 9, 10), _dt(2026, 9, 13), _dt(2026, 9, 16), _dt(2026, 9, 18)]
    assert freshness_factor_from_dates(dates, _dt(2026, 9, 20)) == FATIGUE_MIN_FACTOR


def test_no_dates_is_neutral():
    assert freshness_factor_from_dates([], _dt(2026, 9, 20)) == 1.0


def test_naive_aware_mismatch_is_graceful():
    # naive date vs tz-aware kickoff must not raise → neutral
    assert freshness_factor_from_dates([datetime(2026, 9, 18)], _dt(2026, 9, 20)) == 1.0


def test_disabled_returns_neutral(monkeypatch):
    monkeypatch.delenv("FETCH_FATIGUE", raising=False)
    assert get_fatigue_factor("Arsenal", "soccer_epl", "2026-09-20T14:00:00Z") == 1.0


def test_unmapped_league_neutral(monkeypatch):
    monkeypatch.setenv("FETCH_FATIGUE", "1")
    assert get_fatigue_factor("Some Team", "basketball_nba", "2026-09-20T14:00:00Z") == 1.0
