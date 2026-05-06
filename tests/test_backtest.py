"""Unit tests for the backtest engine — pure-math helpers, no network."""
import math

from betbot.backtest import (
    _brier_multiclass,
    _calibration_buckets,
    _log_loss,
    _outcome_index,
)


def test_outcome_index_home_win():
    assert _outcome_index(2, 1) == 0


def test_outcome_index_draw():
    assert _outcome_index(1, 1) == 1


def test_outcome_index_away_win():
    assert _outcome_index(0, 2) == 2


def test_brier_perfect_prediction():
    # Predicted 100% home win, actual = home win → Brier = 0
    assert _brier_multiclass((1.0, 0.0, 0.0), 0) == 0.0


def test_brier_uniform_prediction():
    # Predicted 1/3 each, actual = home → Brier = (2/3)^2 + (1/3)^2 + (1/3)^2 ≈ 0.667
    score = _brier_multiclass((1 / 3, 1 / 3, 1 / 3), 0)
    assert abs(score - (4 / 9 + 1 / 9 + 1 / 9)) < 1e-9


def test_log_loss_perfect():
    # Predicted ~100% on actual class → log-loss ~ 0
    assert _log_loss((0.9999, 0.0, 0.0001), 0) < 0.001


def test_log_loss_terrible():
    # Predicted ~0% on actual class → log-loss is huge
    assert _log_loss((0.0001, 0.0, 0.9999), 0) > 5


def test_calibration_buckets_well_calibrated():
    """A perfectly calibrated set: predictions of X% should win X% of the time."""
    samples = []
    # 100 samples each at 0.3 / 0.5 / 0.7, with the matching actual rate
    for _ in range(70):
        samples.append((0.7, 1))
    for _ in range(30):
        samples.append((0.7, 0))
    for _ in range(50):
        samples.append((0.5, 1))
    for _ in range(50):
        samples.append((0.5, 0))
    buckets = _calibration_buckets(samples)
    for b in buckets:
        # |predicted - actual| should be tiny for a calibrated model
        assert b["abs_error"] < 0.05


def test_calibration_buckets_overconfident_model():
    """A model that always predicts 80% but only wins 50% of the time."""
    samples = [(0.8, 1) if i % 2 == 0 else (0.8, 0) for i in range(100)]
    buckets = _calibration_buckets(samples)
    # The 80-90% bucket should show large error
    overconf = [b for b in buckets if b["range"] == "80-90%"]
    assert overconf, "Expected the 80-90% bucket"
    assert overconf[0]["abs_error"] > 0.25
