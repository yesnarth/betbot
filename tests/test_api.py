"""Tests for OddsAPIClient with mocked HTTP responses."""
import json
from pathlib import Path

import pytest
import requests

from betbot.api import OddsAPIClient, QuotaExhaustedError

FIXTURE = Path(__file__).parent / "fixtures" / "sample_odds.json"


def _mock_response(mocker, json_data, status=200, headers=None):
    mock = mocker.MagicMock()
    mock.status_code = status
    mock.json.return_value = json_data
    mock.headers = headers or {"x-requests-remaining": "480"}
    return mock


def test_fetch_returns_events(mocker):
    sample = json.loads(FIXTURE.read_text())
    mock_get = mocker.patch("requests.Session.get", return_value=_mock_response(mocker, sample))
    client = OddsAPIClient("fake-key")
    events = client.get_events_with_odds("soccer_epl")
    assert len(events) == 2
    assert events[0]["home_team"] == "Arsenal"


def test_fetch_empty_on_non_200(mocker):
    mocker.patch("requests.Session.get", return_value=_mock_response(mocker, [], status=404))
    client = OddsAPIClient("fake-key")
    events = client.get_events_with_odds("soccer_epl")
    assert events == []


def test_quota_guard_raises(mocker):
    mock = _mock_response(mocker, [], headers={"x-requests-remaining": "10"})
    mocker.patch("requests.Session.get", return_value=mock)
    client = OddsAPIClient("fake-key")
    with pytest.raises(QuotaExhaustedError):
        client.get_events_with_odds("soccer_epl")


def test_quota_updates_on_success(mocker):
    sample = json.loads(FIXTURE.read_text())
    mocker.patch(
        "requests.Session.get",
        return_value=_mock_response(mocker, sample, headers={"x-requests-remaining": "350"}),
    )
    client = OddsAPIClient("fake-key")
    client.get_events_with_odds("soccer_epl")
    assert client.quota_remaining == 350


def test_fetch_all_sports_skips_failed(mocker):
    sample = json.loads(FIXTURE.read_text())
    call_count = 0

    def side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise requests.ConnectionError("timeout")
        return _mock_response(mocker, sample)

    mocker.patch("requests.Session.get", side_effect=side_effect)
    client = OddsAPIClient("fake-key")
    results = client.fetch_all_sports()
    assert isinstance(results, dict)
    assert len(results) >= 1
