"""
Bookmaker whitelist — restrict odds comparison to operators the user can
actually place a bet with.

Why this exists
---------------
`extract_best_odds` used to take the maximum price across every bookmaker
returned by The Odds API (23 distinct operators in practice, dominated by
Betfair and Matchbook exchanges plus offshore books). The edge was therefore
computed against a price the user could not obtain: an audit of 310 production
picks found 99.4% of them priced on an operator the user has no account with,
and only 2 on Betclic.

Taking the max across a wide book universe is also adverse selection — the
book with the highest price is the one that is slowest to move or plainly
wrong, i.e. the one that will void, limit, or have already corrected by the
time the bet is placed.

Restricting the universe to the operators actually used makes `value_edge`
and the Kelly stake denominated in obtainable prices.

Configuration
-------------
`BOOKMAKER_WHITELIST` — comma-separated list of substrings matched against
both the Odds API bookmaker `key` and its display `title`, case- and
punctuation-insensitive. Empty or unset disables filtering (legacy behaviour,
all bookmakers considered).

    BOOKMAKER_WHITELIST=betclic,bet365     # the audited default
    BOOKMAKER_WHITELIST=                   # no filtering

Reality check (2026-09-10): bet365 has vanished from The Odds API feed —
absent from a live eu,uk response and from the official uk bookmaker list,
and 0 of 386 all-time picks ever had it as best book. The whitelist is
therefore de facto Betclic alone (which matches how the owner actually bets).
The 'bet365' entry stays: it is harmless while absent and resumes working by
itself if the feed ever lists bet365 again. Consequence to keep in mind: any
gate counting "distinct whitelisted books" bottoms out at n=1.

This module deliberately has no internal imports so both `models.py` and
`analysis.py` can use it without a circular import.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger("betbot.bookmaker_filter")

_ENV_VAR = "BOOKMAKER_WHITELIST"

# Cache keyed on the raw env string so a runtime change (tests, dashboard)
# is picked up without a restart, while the common case stays allocation-free.
_cache_raw: str | None = None
_cache_tokens: tuple[str, ...] = ()


def _normalize(value: str) -> str:
    """Lowercase and strip everything that is not a letter or a digit, so
    'Betclic (FR)', 'betclic_fr' and 'BetClic' all collapse to 'betclicfr' /
    'betclic' and match the same token."""
    return "".join(ch for ch in value.lower() if ch.isalnum())


def whitelist_tokens() -> tuple[str, ...]:
    """Normalized whitelist tokens from the environment. Empty tuple means
    'no filtering'."""
    global _cache_raw, _cache_tokens
    raw = os.getenv(_ENV_VAR, "")
    if raw != _cache_raw:
        _cache_raw = raw
        _cache_tokens = tuple(
            t for t in (_normalize(part) for part in raw.split(",")) if t
        )
    return _cache_tokens


def is_allowed(bookmaker: dict) -> bool:
    """True if this Odds API bookmaker dict is one the user can bet with.

    Matches a whitelist token against the bookmaker's `key` OR `title`, in
    either direction, so 'bet365' matches both key 'bet365' and title
    'Bet365', and 'betclic' matches title 'Betclic (FR)' (normalized
    'betclicfr').

    Always True when the whitelist is empty — filtering is opt-in.
    """
    tokens = whitelist_tokens()
    if not tokens:
        return True
    key = _normalize(str(bookmaker.get("key", "")))
    title = _normalize(str(bookmaker.get("title", "")))
    for token in tokens:
        for candidate in (key, title):
            if candidate and (token in candidate or candidate in token):
                return True
    return False


def allowed_bookmakers(event: dict) -> list[dict]:
    """The subset of `event['bookmakers']` the user can actually bet with."""
    return [bm for bm in event.get("bookmakers", []) if is_allowed(bm)]


def describe() -> str:
    """Human-readable state, for boot logs and the dashboard."""
    tokens = whitelist_tokens()
    if not tokens:
        return "aucun filtre bookmaker (toutes les cotes comparées)"
    return f"bookmakers restreints à : {', '.join(tokens)}"
