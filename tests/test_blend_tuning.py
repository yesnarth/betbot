"""Per-league blend-weight tuning + storage — Vague 6."""
from betbot import blend_params
from betbot.models import TeamStats
from betbot.tuning import best_weights


# ---------------------------------------------------------------------------
# Storage (blend_params)
# ---------------------------------------------------------------------------

def test_blend_params_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(blend_params, "BLEND_PARAMS_PATH", tmp_path / "bp.json")
    blend_params.reset_cache()
    try:
        assert blend_params.get_weights("soccer_epl") is None          # absent
        blend_params.save_weights("soccer_epl", 0.25, 0.40, 0.55, "2026-01-01T00:00:00+00:00")
        assert blend_params.get_weights("soccer_epl") == (0.25, 0.40)   # round-trips
        assert blend_params.get_weights("soccer_other") is None         # other league → defaults
        assert blend_params.get_weights(None) is None
    finally:
        blend_params.reset_cache()


def test_blend_params_rejects_out_of_range(tmp_path, monkeypatch):
    monkeypatch.setattr(blend_params, "BLEND_PARAMS_PATH", tmp_path / "bp.json")
    blend_params.reset_cache()
    try:
        # elo+xg = 1.1 > 0.95 → must be rejected so Dixon-Coles keeps room → defaults.
        blend_params.save_weights("x", 0.6, 0.5, 0.5, "t")
        assert blend_params.get_weights("x") is None
    finally:
        blend_params.reset_cache()


# ---------------------------------------------------------------------------
# Optimizer (tuning.best_weights) — safety invariant
# ---------------------------------------------------------------------------

def _ts(name, ah, dh, aa, da, elo, xgf, xga) -> TeamStats:
    return TeamStats(
        name=name, attack_home=ah, defense_home=dh, attack_away=aa, defense_away=da,
        matches_analyzed=20, elo_rating=elo, xg_for=xgf, xg_against=xga,
    )


def test_best_weights_never_worse_than_baseline():
    home = _ts("H", 1.4, 0.9, 1.1, 1.0, 1600, 1.6, 0.9)
    away = _ts("A", 1.0, 1.1, 0.8, 1.2, 1500, 1.1, 1.3)
    inputs = [(home, away, 1.4, 1.1, idx) for idx in (0, 0, 1, 2, 0)]
    res = best_weights(inputs, "soccer_epl")
    # The grid includes the default (0.30, 0.35), so it can never end up WORSE.
    assert res["log_loss_after"] <= res["log_loss_before"] + 1e-9
    if res["improved"]:
        assert res["log_loss_after"] < res["log_loss_before"]
    # Returned weights respect the bounds / Dixon-Coles headroom.
    assert 0.0 <= res["elo_weight"] <= 0.45
    assert 0.0 <= res["xg_weight"] <= 0.50
    assert res["elo_weight"] + res["xg_weight"] <= 0.95
