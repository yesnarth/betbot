"""ClubElo date fallback — when today's snapshot isn't published yet (404), walk
back to the most recent available date instead of killing the ELO signal."""
from datetime import date

import requests

from betbot.data_sources import club_elo


def _http404(d):
    resp = requests.Response()
    resp.status_code = 404
    raise requests.HTTPError(response=resp)


def setup_function():
    club_elo._CACHE.clear()


def test_falls_back_to_earlier_date_on_404(monkeypatch):
    calls = []

    def fake_fetch(d):
        calls.append(d)
        if d >= date(2026, 6, 22):      # today + yesterday not published yet
            _http404(d)
        return "Rank,Club,Country,Level,Elo\n1,Arsenal,ENG,1,1900.5\n"

    monkeypatch.setattr(club_elo, "_fetch_csv", fake_fetch)
    ratings = club_elo.get_all_elo_ratings(date(2026, 6, 23))
    assert ratings.get("arsenal") == 1900.5
    # walked 2026-06-23 → -22 (both 404) → -21 (ok)
    assert calls[:3] == [date(2026, 6, 23), date(2026, 6, 22), date(2026, 6, 21)]


def test_empty_when_whole_window_unavailable(monkeypatch):
    monkeypatch.setattr(club_elo, "_fetch_csv", _http404)
    out = club_elo.get_all_elo_ratings(date(2026, 6, 23))
    assert out == {}                      # graceful, no exception
    assert club_elo._CACHE[date(2026, 6, 23)] == {}   # cached → no hammering


def test_non_404_http_error_propagates(monkeypatch):
    def http500(d):
        resp = requests.Response()
        resp.status_code = 500
        raise requests.HTTPError(response=resp)

    monkeypatch.setattr(club_elo, "_fetch_csv", http500)
    try:
        club_elo.get_all_elo_ratings(date(2026, 6, 23))
        assert False, "a 500 should propagate, not be swallowed as a missing date"
    except requests.HTTPError:
        pass
