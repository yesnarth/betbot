"""Shadow-grading of PROPOSED (never-bet) picks for model measurement."""
import betbot.resolver as resolver


class _FakeDB:
    def __init__(self, proposed):
        self._proposed = proposed
        self.updates = []

    def get_proposed_predictions(self):
        return self._proposed

    def update_result(self, event_id, market, selection, result):
        self.updates.append((event_id, market, selection, result))


def test_grades_proposed_football_picks(monkeypatch):
    db = _FakeDB([{
        "event_id": "e1", "sport_key": "soccer_epl", "market": "totals",
        "selection": "O05", "home_team": "A", "away_team": "B",
        "created_at": "2020-01-01T00:00:00+00:00",   # old → passes min_age
    }])
    # Stub the football-data fetch + the (separately tested) grader.
    import betbot.football_api as fa
    monkeypatch.setattr(fa.FootballDataClient, "get_recent_matches",
                        lambda self, comp, limit=300: [])
    monkeypatch.setattr("betbot.football_api.parse_match_results", lambda raw: [])
    monkeypatch.setattr(resolver, "_resolve_from_results",
                        lambda preds, parsed: [("e1", "totals", "O05", "win")])

    res = resolver.resolve_proposed_picks(db, "real-key")
    assert res["resolved"] == 1
    assert db.updates == [("e1", "totals", "O05", "win")]   # result set (no bankroll — see db guard)


def test_no_key_is_noop():
    assert resolver.resolve_proposed_picks(_FakeDB([]), "")["resolved"] == 0


def test_too_fresh_is_skipped(monkeypatch):
    from datetime import datetime, timezone
    db = _FakeDB([{
        "event_id": "e2", "sport_key": "soccer_epl", "market": "h2h", "selection": "1",
        "home_team": "A", "away_team": "B",
        "created_at": datetime.now(timezone.utc).isoformat(),   # today → below min_age
    }])
    res = resolver.resolve_proposed_picks(db, "real-key", min_age_days=1)
    assert res["resolved"] == 0 and db.updates == []
