"""
The /participants audit — a validator must match exactly like production.

Every false team match found so far was discovered inside a live pick. This
audit replays the provider's canonical names through the REAL matcher offline;
these tests lock the two properties that make it trustworthy: the tiers are
derived from the production resolution (never a reimplemented matcher), and
the dangerous tier — 'flou', where every production false match lived — is
what the report surfaces first.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from betbot.participants_audit import audit_league, classify, render_report


def _cache(*names):
    return {n: f"stats-{n}" for n in names}


# ---------------------------------------------------------------------------
# Tier classification wraps the production matcher
# ---------------------------------------------------------------------------

def test_exact_name_is_tier_exact():
    assert classify("Arsenal", _cache("Arsenal", "Chelsea")) == ("exact", "Arsenal")


def test_suffix_variant_is_tier_normalise():
    """'AFC' is a stripped corporate suffix: both sides normalize to 'ajax'."""
    tier, matched = classify("Ajax", _cache("AFC Ajax"))
    assert (tier, matched) == ("normalise", "AFC Ajax")


def test_hand_alias_is_tier_alias():
    tier, matched = classify("Lyon", _cache("Olympique Lyonnais", "Paris FC"))
    assert (tier, matched) == ("alias", "Olympique Lyonnais")


def test_known_alias_fragment_is_tier_alias():
    tier, matched = classify("Inter Milan", _cache("FC Internazionale Milano"))
    assert (tier, matched) == ("alias", "FC Internazionale Milano")


def test_token_fuzzy_resolution_is_tier_flou():
    """'Independ. Rivadavia' resolves by token+similarity scoring — correct
    here, but this is the tier every production false match lived in, so it
    must be surfaced for review, never blended into the safe tiers."""
    tier, matched = classify(
        "Independiente Rivadavia", _cache("Independiente", "Independ. Rivadavia"))
    assert (tier, matched) == ("flou", "Independ. Rivadavia")


def test_no_resolution_is_tier_echec():
    assert classify("Shakhtar Donetsk", _cache("Arsenal")) == ("echec", None)


# ---------------------------------------------------------------------------
# League audit and report
# ---------------------------------------------------------------------------

def test_audit_league_counts_and_review_lists():
    cache = _cache("Arsenal", "AFC Ajax", "Olympique Lyonnais",
                   "Independiente", "Independ. Rivadavia")
    names = ["Arsenal", "Ajax", "Lyon",
             "Independiente Rivadavia", "Shakhtar Donetsk", ""]
    res = audit_league(names, cache)

    assert res["counts"] == {"exact": 1, "alias": 1, "normalise": 1,
                             "flou": 1, "echec": 1}
    assert res["flou"] == [("Independiente Rivadavia", "Independ. Rivadavia")]
    assert res["echec"] == ["Shakhtar Donetsk"]


def test_report_surfaces_flou_and_never_writes_aliases():
    res = {"soccer_epl": {
        "counts": {"exact": 2, "alias": 0, "normalise": 1, "flou": 1, "echec": 1},
        "flou": [("Sporting Lisbon", "Sporting Gijon")],
        "echec": ["Olympiacos"],
    }}
    report = render_report(res, "2026-09-10")

    assert "Sporting Lisbon" in report and "Sporting Gijon" in report
    assert "à revoir en priorité" in report
    assert "à la main" in report          # corrections are manual, always
    assert "clubs" in report and "inactifs" in report  # miss ≠ automatic anomaly


def test_get_participants_parses_names():
    from betbot.api import OddsAPIClient

    client = OddsAPIClient("k")
    client._get = MagicMock(return_value=[
        {"full_name": "Arsenal", "id": "x"},
        {"name": "Chelsea"},
        {"id": "no-name"},
        "not-a-dict",
    ])
    assert client.get_participants("soccer_epl") == ["Arsenal", "Chelsea", ""]
