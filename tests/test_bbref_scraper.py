"""Unit tests for betbot/data_sources/bbref_scraper.py — basketball-reference scraper.

Pure unit tests with mocked HTTP. No real network calls — fixtures contain
hand-crafted minimal HTML that mimics the bb-ref page structure.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from betbot.data_sources import bbref_scraper
from betbot.data_sources.bbref_scraper import _current_season_year, fetch_team_stats


# ---------------------------------------------------------------------------
# _current_season_year — date-dependent rollover logic
# ---------------------------------------------------------------------------

def test_current_season_october_rolls_to_next_year(monkeypatch):
    from datetime import datetime, timezone

    class _FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2025, 10, 15, tzinfo=timezone.utc)
    monkeypatch.setattr(bbref_scraper, "datetime", _FakeDT)
    assert _current_season_year() == 2026


def test_current_season_january_uses_current_year(monkeypatch):
    from datetime import datetime, timezone

    class _FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 1, 15, tzinfo=timezone.utc)
    monkeypatch.setattr(bbref_scraper, "datetime", _FakeDT)
    assert _current_season_year() == 2026


def test_current_season_august_uses_current_year(monkeypatch):
    from datetime import datetime, timezone

    class _FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 8, 15, tzinfo=timezone.utc)
    monkeypatch.setattr(bbref_scraper, "datetime", _FakeDT)
    assert _current_season_year() == 2026


# ---------------------------------------------------------------------------
# fetch_team_stats — graceful failure paths
# ---------------------------------------------------------------------------

def test_fetch_returns_empty_on_http_error():
    fake_resp = type("R", (), {"status_code": 503, "text": "", "headers": {}})()
    with patch.object(bbref_scraper.requests, "get", return_value=fake_resp):
        assert fetch_team_stats(season_year=2026) == []


def test_fetch_returns_empty_on_network_exception():
    import requests

    def _raise(*a, **kw):
        raise requests.ConnectionError("simulated")

    with patch.object(bbref_scraper.requests, "get", side_effect=_raise):
        assert fetch_team_stats(season_year=2026) == []


def test_fetch_returns_empty_on_no_tables():
    """An empty HTML page should yield no teams (not crash)."""
    fake_resp = type("R", (), {"status_code": 200, "text": "<html><body></body></html>", "headers": {}})()
    with patch.object(bbref_scraper.requests, "get", return_value=fake_resp):
        assert fetch_team_stats(season_year=2026) == []


# ---------------------------------------------------------------------------
# fetch_team_stats — happy path with synthetic HTML
# ---------------------------------------------------------------------------

_SYNTHETIC_HTML = """
<html><body>
<table id="advanced-team">
  <thead><tr>
    <th>Rk</th><th>Team</th><th>Age</th><th>W</th><th>L</th><th>PW</th><th>PL</th>
    <th>MOV</th><th>SOS</th><th>SRS</th><th>ORtg</th><th>DRtg</th><th>NRtg</th>
    <th>Pace</th><th>FTr</th><th>3PAr</th><th>TS%</th>
  </tr></thead>
  <tbody>
    <tr><td>1</td><td>Boston Celtics*</td><td>27</td><td>50</td><td>20</td><td>49</td><td>21</td>
        <td>+8.0</td><td>0.5</td><td>+8.5</td><td>120.8</td><td>112.7</td><td>+8.1</td>
        <td>98.5</td><td>0.25</td><td>0.45</td><td>0.59</td></tr>
    <tr><td>2</td><td>Oklahoma City Thunder*</td><td>25</td><td>55</td><td>15</td><td>54</td><td>16</td>
        <td>+11</td><td>0</td><td>+11</td><td>118.9</td><td>107.7</td><td>+11.2</td>
        <td>99.0</td><td>0.27</td><td>0.40</td><td>0.60</td></tr>
    <tr><td></td><td>League Average</td><td>26</td><td>0</td><td>0</td><td>0</td><td>0</td>
        <td>0</td><td>0</td><td>0</td><td>115.0</td><td>115.0</td><td>0</td>
        <td>99.0</td><td>0.26</td><td>0.43</td><td>0.58</td></tr>
  </tbody>
</table>
</body></html>
"""


def test_fetch_parses_synthetic_team_table():
    fake_resp = type("R", (), {"status_code": 200, "text": _SYNTHETIC_HTML, "headers": {}})()
    with patch.object(bbref_scraper.requests, "get", return_value=fake_resp):
        teams = fetch_team_stats(season_year=2026)

    assert len(teams) == 2  # league average row filtered out
    by_name = {t.name: t for t in teams}
    assert "Boston Celtics" in by_name        # asterisk stripped
    assert "Oklahoma City Thunder" in by_name
    bos = by_name["Boston Celtics"]
    assert bos.off_rating == pytest.approx(120.8)
    assert bos.def_rating == pytest.approx(112.7)
    assert bos.pace == pytest.approx(98.5)


def test_fetch_skips_league_average_row():
    fake_resp = type("R", (), {"status_code": 200, "text": _SYNTHETIC_HTML, "headers": {}})()
    with patch.object(bbref_scraper.requests, "get", return_value=fake_resp):
        teams = fetch_team_stats(season_year=2026)
    names = [t.name for t in teams]
    assert "League Average" not in names
