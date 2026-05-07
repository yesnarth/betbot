"""Unit tests for the team-name fuzzy lookup that bridges the two APIs."""
from betbot.analysis import _fuzzy_lookup, _normalize_name


def test_normalize_strips_common_suffixes():
    # Only TRUE corporate suffixes are stripped — discriminating tokens like
    # "united", "city", "hotspur" must be preserved to avoid name collisions
    # (e.g. "Manchester United" vs "Manchester City" both normalising to
    # "manchester" if "united" / "city" were stripped).
    assert _normalize_name("Arsenal FC") == "arsenal"
    assert _normalize_name("Real Madrid CF") == "real madrid"
    assert _normalize_name("Tottenham Hotspur FC") == "tottenham hotspur"
    # Year suffix is still stripped
    assert _normalize_name("Parma Calcio 1913") == "parma"
    # Discriminating tokens must NOT be stripped (the Manchester collision bug)
    assert "united" in _normalize_name("Manchester United FC")
    assert "city" in _normalize_name("Manchester City FC")


def test_manchester_united_does_not_collide_with_manchester_city():
    """Regression test: with `city` and `united` previously in _STRIP_WORDS,
    'Manchester United' silently inherited 'Manchester City FC' team stats."""
    cache = {
        "Manchester City FC":   "city_row",
        "Manchester United FC": "united_row",
    }
    obj_u, _ = _fuzzy_lookup("Manchester United", cache)
    obj_c, _ = _fuzzy_lookup("Manchester City",   cache)
    assert obj_u == "united_row"
    assert obj_c == "city_row"


def test_normalize_strips_accents():
    assert _normalize_name("Atlético de Madrid") == "atletico madrid"


def test_exact_match_wins():
    cache = {"Arsenal FC": "row"}
    obj, matched = _fuzzy_lookup("Arsenal FC", cache)
    assert obj == "row"
    assert matched == "Arsenal FC"


def test_normalized_match():
    """'Arsenal' should resolve to 'Arsenal FC' (normalized identity)."""
    cache = {"Arsenal FC": "row"}
    obj, matched = _fuzzy_lookup("Arsenal", cache)
    assert obj == "row"
    assert matched == "Arsenal FC"


def test_substring_match_for_long_descriptive_names():
    """'Espanyol' must match 'RCD Espanyol de Barcelona'."""
    cache = {"RCD Espanyol de Barcelona": "row"}
    obj, _ = _fuzzy_lookup("Espanyol", cache)
    assert obj == "row"


def test_alias_resolves_inter_milan():
    """The hardcoded alias bridges 'Inter Milan' → 'FC Internazionale Milano'."""
    cache = {
        "AC Milan": "milan_row",
        "FC Internazionale Milano": "inter_row",
    }
    obj, matched = _fuzzy_lookup("Inter Milan", cache)
    assert obj == "inter_row"
    assert matched == "FC Internazionale Milano"


def test_no_match_returns_none():
    cache = {"Arsenal FC": "row"}
    obj, matched = _fuzzy_lookup("FC Barcelona", cache)
    assert obj is None
    assert matched is None
