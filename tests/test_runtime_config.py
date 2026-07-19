"""Runtime Odds-key override — dashboard rotation without restart."""
import betbot.runtime_config as rc
from betbot.api import OddsAPIClient


def test_set_get_and_clear_override(tmp_path, monkeypatch):
    monkeypatch.setattr(rc, "RUNTIME_CONFIG_PATH", tmp_path / "rc.json")
    assert rc.get_odds_api_key_override() is None
    rc.set_odds_api_key("abc123key")
    assert rc.get_odds_api_key_override() == "abc123key"
    rc.set_odds_api_key("")                       # clearing removes it
    assert rc.get_odds_api_key_override() is None


def test_effective_key_prefers_override_over_env(tmp_path, monkeypatch):
    monkeypatch.setattr(rc, "RUNTIME_CONFIG_PATH", tmp_path / "rc.json")
    monkeypatch.setenv("ODDS_API_KEY", "env-key")
    assert rc.get_odds_api_key() == "env-key"      # no override → env
    rc.set_odds_api_key("dash-key")
    assert rc.get_odds_api_key() == "dash-key"      # override wins


def test_status_is_masked(tmp_path, monkeypatch):
    monkeypatch.setattr(rc, "RUNTIME_CONFIG_PATH", tmp_path / "rc.json")
    monkeypatch.setenv("ODDS_API_KEY", "")
    rc.set_odds_api_key("abcd1234wxyz")
    st = rc.odds_key_status()
    assert st["configured"] and st["source"] == "dashboard" and st["masked"] == "…wxyz"


def test_client_picks_up_override_live(tmp_path, monkeypatch):
    monkeypatch.setattr(rc, "RUNTIME_CONFIG_PATH", tmp_path / "rc.json")
    c = OddsAPIClient("explicit-key")
    assert c._key == "explicit-key"                # no override → explicit
    rc.set_odds_api_key("dashboard-key")
    assert c._key == "dashboard-key"               # SAME instance now uses override — no restart
    # force_key pins the exact key (used to validate a NEW key before saving)
    assert OddsAPIClient("brand-new", force_key=True)._key == "brand-new"
