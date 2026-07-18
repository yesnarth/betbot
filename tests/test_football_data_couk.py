"""football-data.co.uk real closing-odds source + real-odds cold-start."""
from datetime import date, timedelta

from betbot.data_sources import football_data_co_uk as fdc
from betbot import ml


def test_season_code_and_recent():
    assert fdc._season_code(2025) == "2526"
    assert fdc._season_code(2024) == "2425"
    seasons = fdc.recent_completed_seasons(2)
    assert len(seasons) == 2 and seasons[0] > seasons[1]   # newest first, descending


_CSV = """Div,Date,HomeTeam,AwayTeam,FTHG,FTAG,FTR,B365CH,B365CD,B365CA,PSCH,PSCD,PSCA
E0,16/08/2024,Man United,Fulham,1,0,H,1.60,4.20,5.25,1.62,4.30,5.40
E0,17/08/2024,Ipswich,Liverpool,0,2,A,7.00,4.50,1.50,7.20,4.60,1.48
E0,18/08/2024,Bad,Row,,,, ,,,,,
"""


def test_parse_csv_prefers_pinnacle_closing():
    matches = fdc._parse_csv(_CSV)
    assert len(matches) == 2                       # the malformed/no-score row is dropped
    m = matches[0]
    assert m["home_team"] == "Man United" and m["away_team"] == "Fulham"
    assert m["home_goals"] == 1 and m["away_goals"] == 0
    assert m["date"] == "2024-08-16"
    assert m["close_home"] == 1.62                 # Pinnacle closing preferred over Bet365
    assert m["close_draw"] == 4.30 and m["close_away"] == 5.40


def test_get_matches_unmapped_league_is_empty():
    assert fdc.get_matches_with_odds("basketball_nba") == []


def _synth_season(n_rounds: int = 3) -> list[dict]:
    """A small round-robin where lower-indexed teams are stronger, so the model
    learns varied probabilities. Real closing odds attached for the shrink."""
    teams = [f"T{i}" for i in range(8)]
    base = date(2025, 1, 1)
    out: list[dict] = []
    day = 0
    for r in range(n_rounds):
        for i in range(len(teams)):
            for j in range(len(teams)):
                if i == j:
                    continue
                day += 1
                hg = max(0, 3 - i // 2)
                ag = max(0, 2 - j // 2)
                out.append({
                    "date": (base + timedelta(days=day)).isoformat(),
                    "home_team": teams[i], "away_team": teams[j],
                    "home_goals": hg, "away_goals": ag,
                    "close_home": 2.0, "close_draw": 3.4, "close_away": 3.9,
                })
    out.sort(key=lambda m: m["date"])
    return out


def test_walk_forward_real_samples_shape():
    samples = ml._walk_forward_real_samples(_synth_season())
    assert samples, "should emit samples once enough history accrues"
    assert len(samples) % 3 == 0                   # exactly 3 (home/draw/away) per scored match
    for p, won in samples:
        assert 0.0 <= p <= 1.0
        assert won in (0, 1)


def test_cold_start_real_uses_fetcher(monkeypatch, tmp_path):
    # Feed synthetic matches for one league; assert a calibrator is persisted.
    monkeypatch.setattr(ml, "CALIBRATOR_PATH", tmp_path / "calib.json")
    monkeypatch.setattr(ml, "MIN_SAMPLES_TO_TRUST", 30)
    monkeypatch.setattr(fdc, "get_matches_with_odds",
                        lambda sk, n_seasons=2: _synth_season() if sk == "soccer_epl" else [])
    ml.reset_cache()
    res = ml.cold_start_train_real(sport_keys=("soccer_epl",))
    assert res["trained"] is True and res["source"] == "cold_start_real_odds"
    assert (tmp_path / "calib.json").exists()
    ml.reset_cache()
