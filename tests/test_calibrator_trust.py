"""
A calibrator must earn the right to correct the model.

Isotonic regression is non-parametric: it fits a step function with as many
steps as the data suggests, and on a small sample those steps land on 0.0 and
1.0 at the extremes. That is not theoretical here — it happened twice on this
dataset, in opposite directions:

  * fitted on 114 clean samples, the global map sends 0.80 -> 0.997, the
    certainty-fabrication that produced 59 counted bets at model_prob = 1.000;
  * fitted on the polluted set, the football_h2h segment flattened EVERYTHING
    above 0.496 to 0.529, making the 0.70 confidence floor unreachable on 1X2
    and silently reducing the bot to derived markets (h2h: 2 picks against
    38 draw-no-bet and 32 double-chance).

Two guards, one for each direction: a sample floor, and hard output bounds.
"""
from __future__ import annotations

import json

import pytest

import betbot.ml as ml


@pytest.fixture(autouse=True)
def _fresh_cache():
    ml._cached_calibrator = None
    yield
    ml._cached_calibrator = None


def _write(tmp_path, monkeypatch, payload):
    path = tmp_path / "calibrator.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(ml, "CALIBRATOR_PATH", path)
    ml._cached_calibrator = None


def _payload(global_n, segments):
    return {
        "format": "isotonic-segmented-v1",
        "global": {"x": [0.4, 0.9], "y": [0.3, 0.8], "n": global_n},
        "segments": {name: {"x": x, "y": y, "n": n}
                     for name, (x, y, n) in segments.items()},
    }


# ---------------------------------------------------------------------------
# The sample floor
# ---------------------------------------------------------------------------

def test_the_floor_is_high_enough_for_a_non_parametric_fit():
    """50 resolved bets cannot support isotonic regression. The two failures
    above both came from fits of roughly a hundred samples."""
    assert ml.MIN_SAMPLES_TO_TRUST >= 300


def test_an_undersupported_segment_is_ignored_at_load(tmp_path, monkeypatch):
    """Raising the floor only governs FUTURE training. Without a load-time
    check the pathological map stays live for as long as the file exists —
    and it was fitted on picks since quarantined as invalid."""
    _write(tmp_path, monkeypatch, _payload(
        global_n=5000,
        segments={"football_h2h": ([0.45, 1.0], [0.529, 0.529], 60)},
    ))

    cal = ml._load_calibrator()

    assert "football_h2h" not in (cal["segments"] or {})


def test_a_well_supported_segment_is_kept(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, _payload(
        global_n=5000,
        segments={"football_h2h": ([0.45, 0.9], [0.40, 0.85], 4000)},
    ))

    assert "football_h2h" in ml._load_calibrator()["segments"]


def test_an_undersupported_global_map_is_ignored(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, _payload(global_n=114, segments={}))

    assert ml._load_calibrator() is None


def test_the_flattening_segment_no_longer_reaches_the_model(tmp_path, monkeypatch):
    """The live symptom: every probability above 0.496 became 0.529, so a
    0.70 floor could never be met on 1X2."""
    _write(tmp_path, monkeypatch, _payload(
        global_n=100,
        segments={"football_h2h": ([0.454, 1.0], [0.529, 0.529], 60)},
    ))

    for raw in (0.55, 0.72, 0.90):
        assert ml.calibrate(raw, "football_h2h") == raw, "must pass through untouched"


# ---------------------------------------------------------------------------
# Output bounds — no fit may assert certainty
# ---------------------------------------------------------------------------

def test_a_map_reaching_one_is_clamped(tmp_path, monkeypatch):
    """An isotonic map legitimately contains a 1.0 knot — that is its extreme
    training bin, not a statement about the world. Once renormalisation sees
    it, "two zeros and one survivor" becomes a fabricated 100%."""
    _write(tmp_path, monkeypatch, _payload(
        global_n=5000,
        segments={"football_h2h": ([0.40, 0.80], [0.50, 1.0], 4000)},
    ))

    assert ml.calibrate(0.80, "football_h2h") == pytest.approx(ml._CAL_OUTPUT_MAX)


def test_a_map_reaching_zero_is_clamped(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, _payload(
        global_n=5000,
        segments={"football_h2h": ([0.40, 0.80], [0.0, 0.60], 4000)},
    ))

    assert ml.calibrate(0.40, "football_h2h") == pytest.approx(ml._CAL_OUTPUT_MIN)


def test_the_bounds_leave_a_sane_fit_alone(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, _payload(
        global_n=5000,
        segments={"football_h2h": ([0.40, 0.80], [0.42, 0.78], 4000)},
    ))

    assert ml.calibrate(0.60, "football_h2h") == pytest.approx(0.60, abs=0.01)


def test_the_bounds_are_not_a_licence_to_extrapolate(tmp_path, monkeypatch):
    """Outside the trained domain the raw probability still passes through
    untouched — clamping is the last line of defence, not the first."""
    _write(tmp_path, monkeypatch, _payload(
        global_n=5000,
        segments={"football_h2h": ([0.50, 0.80], [0.45, 0.75], 4000)},
    ))

    assert ml.calibrate(0.20, "football_h2h") == 0.20
    assert ml.calibrate(0.95, "football_h2h") == 0.95


def test_no_calibrator_at_all_is_a_pass_through(tmp_path, monkeypatch):
    monkeypatch.setattr(ml, "CALIBRATOR_PATH", tmp_path / "absent.json")
    ml._cached_calibrator = None

    assert ml.calibrate(0.73, "football_h2h") == 0.73
