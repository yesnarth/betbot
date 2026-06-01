"""Per-segment calibration — Wave 3, item 3.1.

A single calibrator trained mostly on football h2h must not be applied blindly
to tennis / basket / totals. These cover the segment routing + v1 fallback.
"""
import json

import pytest

from betbot import ml


def test_segment_for():
    assert ml.segment_for("tennis_atp_us_open", "h2h") == "tennis"
    assert ml.segment_for("basketball_nba", "h2h") == "basketball"
    assert ml.segment_for("soccer_epl", "h2h") == "football_h2h"
    assert ml.segment_for("soccer_epl", "totals") == "football_totals"
    assert ml.segment_for(None, None) == "football_h2h"


def _install(tmp_path, monkeypatch, payload: dict):
    p = tmp_path / "calib.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(ml, "CALIBRATOR_PATH", p)
    ml.reset_cache()


def test_calibrate_prefers_segment_then_global(tmp_path, monkeypatch):
    # global = identity ; tennis segment pins everything to 0.9
    _install(tmp_path, monkeypatch, {
        "format": "isotonic-segmented-v1",
        "global": {"x": [0.0, 1.0], "y": [0.0, 1.0], "n": 100},
        "segments": {"tennis": {"x": [0.0, 1.0], "y": [0.9, 0.9], "n": 60}},
        "trained_at": "2026-01-01T00:00:00+00:00",
        "source": "resolved_bets",
    })
    try:
        assert ml.calibrate(0.30, "tennis") == pytest.approx(0.9, abs=1e-6)   # segment map
        assert ml.calibrate(0.30, "basketball") == pytest.approx(0.30, abs=1e-6)  # → global
        assert ml.calibrate(0.30) == pytest.approx(0.30, abs=1e-6)            # → global
    finally:
        ml.reset_cache()


def test_calibrate_legacy_v1_is_treated_as_global(tmp_path, monkeypatch):
    _install(tmp_path, monkeypatch, {
        "format": "isotonic-thresholds-v1",
        "x_thresholds": [0.0, 1.0],
        "y_thresholds": [0.5, 0.5],
        "trained_at": "2026-01-01T00:00:00+00:00",
    })
    try:
        assert ml.calibrate(0.2) == pytest.approx(0.5, abs=1e-6)
        assert ml.calibrate(0.2, "tennis") == pytest.approx(0.5, abs=1e-6)  # falls back
    finally:
        ml.reset_cache()


def test_calibrate_identity_when_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr(ml, "CALIBRATOR_PATH", tmp_path / "absent.json")
    ml.reset_cache()
    try:
        assert ml.calibrate(0.42, "tennis") == 0.42
    finally:
        ml.reset_cache()


def test_calibrator_status_reports_segments(tmp_path, monkeypatch):
    _install(tmp_path, monkeypatch, {
        "format": "isotonic-segmented-v1",
        "global": {"x": [0.0, 1.0], "y": [0.0, 1.0], "n": 100},
        "segments": {"tennis": {"x": [0.0, 1.0], "y": [0.9, 0.9], "n": 60}},
        "trained_at": "2026-01-01T00:00:00+00:00",
        "source": "resolved_bets",
    })
    try:
        status = ml.calibrator_status()
        assert status["available"] is True
        assert status["segments"] == ["tennis"]
        assert status["has_global"] is True
    finally:
        ml.reset_cache()
