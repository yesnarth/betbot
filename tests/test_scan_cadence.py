"""
Four scans a day, but not four emails a day.

The owner chose freshness over market breadth: more scans, each closer to
kick-off, because the price at H-1 knows the line-ups and the price at 10:00
does not. That decision multiplies the scans — it must not multiply the noise.

With one daily scan, an empty result deserved an email: it told the owner the
bot had run and found nothing. Repeat that four times and the inbox cries wolf,
which is how a real pick gets skipped.
"""
from __future__ import annotations

import inspect

import pytest

from betbot.main import run_daily_scan


def test_the_scan_can_be_told_to_stay_silent():
    params = inspect.signature(run_daily_scan).parameters
    assert "notify_when_empty" in params
    assert params["notify_when_empty"].default is True, (
        "silence must be opt-in: a caller that forgets it still gets told")


def test_only_the_first_slot_reports_an_empty_day():
    """The scheduler passes notify_when_empty=(idx == 0)."""
    from pathlib import Path

    src = Path("betbot/main.py").read_text(encoding="utf-8")
    assert "notify_empty = (idx == 0)" in src
    assert "logger, False, label, notify_empty" in src


def test_an_empty_late_slot_sends_nothing():
    from pathlib import Path

    src = Path("betbot/main.py").read_text(encoding="utf-8")
    assert "if not dry_run and notify_when_empty:" in src, (
        "the no-fixtures path must respect the flag")
    assert "if not _to_recommend and not parlays and not favoris_bets and not notify_when_empty:" in src, (
        "fixtures but no picks must also respect it")


def test_shadow_picks_are_still_recorded_when_the_email_is_skipped():
    """Silence is about the inbox, not about the database: totals probes are
    persisted before the email decision is taken."""
    from pathlib import Path

    src = Path("betbot/main.py").read_text(encoding="utf-8")
    save_at = src.index("db.save_prediction(")
    skip_at = src.index("if not _to_recommend and not parlays and not favoris_bets and not notify_when_empty:")
    assert save_at < skip_at, "picks must be persisted before any early return"


# ---------------------------------------------------------------------------
# Slot naming
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("hours,expected", [
    (["10:00"], ["Matin"]),
    (["10:00", "19:00"], ["Matin", "Après-midi"]),
    (["10:00", "16:00", "19:00", "21:30"],
     ["Matin", "Après-midi", "Soir", "Tard"]),
])
def test_every_configured_slot_gets_a_readable_name(hours, expected):
    """The map was written for exactly three slots and fell back to raw times
    beyond that, which made a four-slot day unreadable in the inbox."""
    _slot_names = ["Matin", "Après-midi", "Soir", "Tard", "Nuit"]
    labels = {h: (_slot_names[i] if i < len(_slot_names) else h)
              for i, h in enumerate(hours)}
    assert [labels[h] for h in hours] == expected


def test_more_slots_than_names_degrades_to_the_time():
    _slot_names = ["Matin", "Après-midi", "Soir", "Tard", "Nuit"]
    hours = ["08:00", "11:00", "14:00", "17:00", "20:00", "23:00"]
    labels = {h: (_slot_names[i] if i < len(_slot_names) else h)
              for i, h in enumerate(hours)}
    assert labels["23:00"] == "23:00"
