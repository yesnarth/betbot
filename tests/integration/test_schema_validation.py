"""
Pydantic schema validation tests — bad request bodies must return 422
with a structured error, not silently coerce.

These tests catch regressions where a schema field gets relaxed (wrong
bound, missing required, accepting wrong type). They run against the
live FastAPI app via TestClient.
"""
from __future__ import annotations


# ---------------------------------------------------------------------------
# BankrollMutation
# ---------------------------------------------------------------------------

def test_deposit_rejects_negative_amount(client):
    r = client.post("/bankroll/deposit", json={"amount": -10})
    assert r.status_code == 422


def test_deposit_rejects_zero_amount(client):
    r = client.post("/bankroll/deposit", json={"amount": 0})
    assert r.status_code == 422


def test_deposit_rejects_huge_amount(client):
    r = client.post("/bankroll/deposit", json={"amount": 5_000_000})
    assert r.status_code == 422


def test_deposit_rejects_missing_amount(client):
    r = client.post("/bankroll/deposit", json={"note": "no amount"})
    assert r.status_code == 422


def test_deposit_rejects_string_amount(client):
    r = client.post("/bankroll/deposit", json={"amount": "fifty"})
    assert r.status_code == 422


def test_withdraw_rejects_negative_amount(client):
    r = client.post("/bankroll/withdraw", json={"amount": -10})
    assert r.status_code == 422


def test_withdraw_rejects_note_too_long(client):
    r = client.post("/bankroll/withdraw",
                    json={"amount": 5, "note": "x" * 600})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# ConfirmPlacedRequest
# ---------------------------------------------------------------------------

def test_confirm_placed_rejects_garbage_unconfirm(client):
    """Random non-bool strings must produce a 422, not a silent default.
    (Pydantic 2 coerces 'true'/'false'/'yes'/'no'/'1'/'0' by design;
    'banana' is not a valid bool alias.)"""
    r = client.post("/predictions/1/confirm-placed",
                    json={"unconfirm": "banana"})
    assert r.status_code == 422


def test_confirm_placed_accepts_empty_body(client):
    """Defaults: bookmaker=None, unconfirm=False — both optional."""
    r = client.post("/predictions/1/confirm-placed", json={})
    # 200 because mock_db.confirm_prediction_placed returns True by default
    assert r.status_code == 200


def test_confirm_placed_rejects_bookmaker_too_long(client):
    r = client.post("/predictions/1/confirm-placed",
                    json={"bookmaker": "x" * 100})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# SkipRequest
# ---------------------------------------------------------------------------

def test_skip_rejects_reason_too_long(client):
    r = client.post("/predictions/1/skip", json={"reason": "x" * 200})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# ProposedPickInput — /admin/save-pick-as-proposed
# ---------------------------------------------------------------------------

def _valid_pick(**overrides) -> dict:
    base = {
        "event_id": "evt-test-1",
        "sport_key": "soccer_epl",
        "home_team": "Arsenal",
        "away_team": "Chelsea",
        "market": "h2h",
        "selection_code": "1",
        "model_prob": 0.55,
        "best_odds": 2.10,
        "best_book": "pinnacle",
        "value_edge": 0.155,
        "kelly_stake": 5.0,
        "model_type": "blended",
    }
    base.update(overrides)
    return base


def test_save_pick_accepts_valid(client):
    r = client.post("/admin/save-pick-as-proposed", json=_valid_pick())
    assert r.status_code == 200


def test_save_pick_rejects_prob_over_one(client):
    r = client.post("/admin/save-pick-as-proposed",
                    json=_valid_pick(model_prob=1.5))
    assert r.status_code == 422


def test_save_pick_rejects_odds_below_one(client):
    r = client.post("/admin/save-pick-as-proposed",
                    json=_valid_pick(best_odds=0.5))
    assert r.status_code == 422


def test_save_pick_rejects_missing_event_id(client):
    payload = _valid_pick()
    payload.pop("event_id")
    r = client.post("/admin/save-pick-as-proposed", json=payload)
    assert r.status_code == 422


def test_save_pick_rejects_invalid_reliability(client):
    r = client.post("/admin/save-pick-as-proposed",
                    json=_valid_pick(reliability=1.5))
    assert r.status_code == 422


def test_save_pick_accepts_optional_reliability(client):
    r = client.post("/admin/save-pick-as-proposed",
                    json=_valid_pick(reliability=0.75))
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# BacktestRequest
# ---------------------------------------------------------------------------

def test_backtest_rejects_n_holdout_too_small(client):
    r = client.post("/stats/backtest",
                    json={"sport_key": "soccer_epl", "n_holdout": 5})
    assert r.status_code == 422


def test_backtest_rejects_missing_sport_key(client):
    r = client.post("/stats/backtest", json={"n_holdout": 50})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Query parameter bounds
# ---------------------------------------------------------------------------

def test_roi_rejects_days_zero(client):
    r = client.get("/stats/roi?days=0")
    assert r.status_code == 422


def test_roi_rejects_days_too_large(client):
    r = client.get("/stats/roi?days=99999")
    assert r.status_code == 422


def test_bankroll_history_rejects_limit_zero(client):
    r = client.get("/bankroll/history?limit=0")
    assert r.status_code == 422
