"""
Two housekeeping guarantees the measurement layer depends on.

1. QUARANTINE. 59 production picks were born at model_prob >= 0.99 from a
   broken calibrator. They are genuine bets, so they are never deleted — but
   counting them poisons the ROI, the hit rate, and the calibrator's own
   training set, which is how a measurement bug becomes a modelling bug.

2. CROSS-COMPETITION xG. The Champions League and its qualifiers carried no
   xG for 48 teams whose clubs already had it under their domestic league.
   Nothing to fetch: the numbers were in the table, one sport_key away.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# 1. Quarantine
# ---------------------------------------------------------------------------

def test_predictions_carry_a_named_exclusion_reason():
    from betbot.orm_models import Prediction

    assert hasattr(Prediction, "excluded_reason")


def test_roi_never_counts_a_quarantined_pick():
    src = Path("betbot/db.py").read_text(encoding="utf-8")
    roi = src[src.index("def get_roi_stats"):]
    roi = roi[:roi.index("\n    def ", 10)] if "\n    def " in roi[10:] else roi
    assert "Prediction.excluded_reason.is_(None)" in roi


def test_the_calibrator_never_trains_on_a_quarantined_pick():
    """The sharpest edge of the problem: a pick the model got wrong because it
    was broken must not teach the model that it was right."""
    src = Path("betbot/ml.py").read_text(encoding="utf-8")
    assert src.count("Prediction.excluded_reason.is_(None)") == 2, (
        "both the pooled and the segmented training queries must filter")


def test_the_migration_backfills_only_degenerate_rows():
    mig = Path("alembic/versions/o9d2f6h8c1e5_add_excluded_reason.py").read_text(
        encoding="utf-8")

    assert re.search(r'down_revision\s*=\s*"n8c1e5g7b9d4"', mig)
    assert "model_prob >= 0.99 OR model_prob <= 0.01" in mig
    assert "excluded_reason IS NULL" in mig, "must not overwrite an existing reason"
    assert "DELETE" not in mig.upper(), "predictions are never deleted"


def test_quarantine_is_reversible():
    mig = Path("alembic/versions/o9d2f6h8c1e5_add_excluded_reason.py").read_text(
        encoding="utf-8")
    assert "def downgrade" in mig and "drop_column" in mig


# ---------------------------------------------------------------------------
# 2. Cross-competition xG inheritance
# ---------------------------------------------------------------------------

class _FakeRow:
    def __init__(self, name, sport_key, xg_for=None, xg_against=None):
        self.team_name, self.sport_key = name, sport_key
        self.xg_for, self.xg_against = xg_for, xg_against


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, _stmt):
        class _R:
            def __init__(self, rows):
                self._rows = rows

            def all(self):
                return [(r.team_name, r.sport_key, r.xg_for, r.xg_against)
                        for r in self._rows]
        return _R(self._rows)

    def get(self, _model, key):
        name, sport_key = key
        for r in self._rows:
            if r.team_name == name and r.sport_key == sport_key:
                return r
        return None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _run_inheritance(monkeypatch, rows):
    import betbot.enrichment as mod
    import betbot.database as dbmod

    monkeypatch.setattr(dbmod, "session_scope", lambda: _FakeSession(rows))
    return mod._inherit_xg_across_leagues(), rows


def test_a_champions_league_side_inherits_its_domestic_xg(monkeypatch):
    rows = [
        _FakeRow("Arsenal", "soccer_epl", 1.8, 0.9),
        _FakeRow("Arsenal", "soccer_uefa_champs_league"),
    ]
    filled, rows = _run_inheritance(monkeypatch, rows)

    assert filled == 1
    assert rows[1].xg_for == 1.8
    assert rows[1].xg_against == 0.9


def test_an_ambiguous_name_is_declined_not_guessed(monkeypatch):
    """Same exact name with xG in two competitions: we cannot tell which club
    is meant, and a wrong xG is silent. Declining is the only safe answer."""
    rows = [
        _FakeRow("Nacional", "soccer_portugal_primeira_liga", 1.5, 1.1),
        _FakeRow("Nacional", "soccer_uruguay_primera", 1.2, 1.3),
        _FakeRow("Nacional", "soccer_conmebol_copa_libertadores"),
    ]
    filled, rows = _run_inheritance(monkeypatch, rows)

    assert filled == 0
    assert rows[2].xg_for is None


def test_an_existing_xg_is_never_overwritten(monkeypatch):
    rows = [
        _FakeRow("Arsenal", "soccer_epl", 1.8, 0.9),
        _FakeRow("Arsenal", "soccer_uefa_champs_league", 2.1, 0.7),
    ]
    filled, rows = _run_inheritance(monkeypatch, rows)

    assert filled == 0
    assert rows[1].xg_for == 2.1, "this only fills holes"


def test_an_unknown_club_stays_empty(monkeypatch):
    rows = [_FakeRow("Obscure FC", "soccer_uefa_champs_league")]
    filled, _ = _run_inheritance(monkeypatch, rows)
    assert filled == 0


def test_matching_is_exact_never_fuzzy(monkeypatch):
    """Cross-competition fuzzy matching is exactly where clubs get confused."""
    rows = [
        _FakeRow("Dundee", "soccer_spl", 0.8, 1.9),
        _FakeRow("Dundee United", "soccer_scottish_cup"),
    ]
    filled, rows = _run_inheritance(monkeypatch, rows)

    assert filled == 0
    assert rows[1].xg_for is None
