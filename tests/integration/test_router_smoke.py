"""
Router smoke tests — every endpoint we ship is reachable and produces a
well-formed response. This protects against the kind of regression a
file-split or rename can silently introduce (wrong prefix on APIRouter,
forgotten include_router, schema field renamed).
"""
from __future__ import annotations

from unittest.mock import patch


# ---------------------------------------------------------------------------
# Predictions — queue queries
# ---------------------------------------------------------------------------

def test_predictions_proposed_returns_200(client):
    r = client.get("/predictions/proposed")
    assert r.status_code == 200, r.text
    assert isinstance(r.json(), list)


def test_predictions_skipped_returns_200(client):
    r = client.get("/predictions/skipped?limit=5")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_predictions_pending_returns_200(client):
    r = client.get("/predictions/pending")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_skip_404_when_prediction_missing(client, mock_db):
    mock_db.skip_prediction.return_value = False
    r = client.post("/predictions/9999/skip", json={"reason": "test"})
    assert r.status_code == 404


def test_unskip_404_when_prediction_missing(client, mock_db):
    mock_db.unskip_prediction.return_value = False
    r = client.post("/predictions/9999/unskip", json={})
    assert r.status_code == 404


def test_confirm_placed_returns_payload_on_success(client):
    r = client.post("/predictions/42/confirm-placed",
                    json={"bookmaker": "pinnacle"})
    assert r.status_code == 200
    body = r.json()
    assert body["prediction_id"] == 42
    assert body["placement_status"] == "confirmed"
    assert body["bookmaker"] == "pinnacle"


def test_confirm_placed_with_unconfirm_returns_proposed(client):
    r = client.post("/predictions/42/confirm-placed",
                    json={"unconfirm": True})
    assert r.status_code == 200
    assert r.json()["placement_status"] == "proposed"


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def test_roi_returns_zero_when_no_bets(client):
    r = client.get("/stats/roi?days=30")
    assert r.status_code == 200
    body = r.json()
    assert body["n_bets"] == 0
    assert body["hit_rate"] == 0.0


def test_clv_coverage_returns_breakdown(client):
    with patch("betbot.clv.count_missed_clv_snapshots", return_value={
        "n_total_confirmed": 10,
        "n_with_clv": 7,
        "n_pending_clv": 2,
        "n_missed_clv": 1,
        "coverage_pct": 70.0,
    }):
        r = client.get("/stats/clv-coverage?days=30")
    assert r.status_code == 200
    body = r.json()
    assert body["coverage_pct"] == 70.0


# ---------------------------------------------------------------------------
# Bankroll
# ---------------------------------------------------------------------------

def test_bankroll_state_returns_snapshot(client):
    from betbot.bankroll import BankrollState
    fake_state = BankrollState(
        balance=100.0, committed=0.0, available=100.0,
        total_deposits=100.0, total_withdrawals=0.0,
        total_won=0.0, total_lost_stakes=0.0, pnl=0.0, n_entries=1,
    )
    with patch("betbot.bankroll.get_state", return_value=fake_state):
        r = client.get("/bankroll/state")
    assert r.status_code == 200
    body = r.json()
    assert body["balance"] == 100.0


def test_bankroll_guards_returns_dict(client):
    with patch("betbot.guards.get_guard_status", return_value={
        "stop_loss_active": False, "balance": 100.0, "committed": 0.0,
    }):
        r = client.get("/bankroll/guards")
    assert r.status_code == 200
    assert isinstance(r.json(), dict)


def test_bankroll_history_returns_list(client):
    with patch("betbot.bankroll.get_history", return_value=[]):
        r = client.get("/bankroll/history?limit=10")
    assert r.status_code == 200
    assert r.json() == []


def test_bankroll_evolution_returns_list(client):
    with patch("betbot.bankroll.get_evolution", return_value=[]):
        r = client.get("/bankroll/evolution?days=7")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


# ---------------------------------------------------------------------------
# ML calibrator
# ---------------------------------------------------------------------------

def test_calibrator_status_returns_payload(client):
    with patch("betbot.ml.calibrator_status", return_value={"available": False, "path": "x"}), \
         patch("betbot.ml._collect_training_data", return_value=[]):
        r = client.get("/ml/calibrator/status")
    assert r.status_code == 200
    body = r.json()
    assert "available" in body
    assert "ready_to_train" in body


# ---------------------------------------------------------------------------
# Agent runs
# ---------------------------------------------------------------------------

def test_agent_runs_list_returns_list(client):
    r = client.get("/agent/runs?limit=5")
    assert r.status_code == 200
    assert r.json() == []


def test_agent_run_detail_404_when_missing(client, mock_db):
    mock_db.get_agent_run.return_value = None
    r = client.get("/agent/runs/999")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Promotions / cash-outs
# ---------------------------------------------------------------------------

def test_promotions_list_returns_list(client):
    with patch("betbot.promotions.list_promotions", return_value=[]):
        r = client.get("/promotions")
    assert r.status_code == 200
    assert r.json() == []


def test_promotions_summary_returns_dict(client):
    with patch("betbot.promotions.promotions_summary", return_value={"n": 0}):
        r = client.get("/promotions/summary")
    assert r.status_code == 200
    assert isinstance(r.json(), dict)


def test_cashouts_list_returns_list(client):
    with patch("betbot.promotions.list_cashouts", return_value=[]):
        r = client.get("/cashouts?limit=5")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


# ---------------------------------------------------------------------------
# Tennis / basketball / sports models
# ---------------------------------------------------------------------------

def test_tennis_status_returns_dict(client):
    with patch("betbot.tennis_model.status", return_value={"available": False}):
        r = client.get("/tennis/status")
    assert r.status_code == 200
    assert isinstance(r.json(), dict)


def test_basketball_status_returns_dict(client):
    with patch("betbot.basketball_model.status", return_value={"available": False}):
        r = client.get("/basketball/status")
    assert r.status_code == 200
    assert isinstance(r.json(), dict)
