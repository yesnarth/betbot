"""
Favoris calibrés — the agreement channel.

Born from a funnel measurement (2026-08-27): on 768 real outcomes across 136
fixtures, the value gates retained ZERO picks — 443 died under the confidence
floor, 325 under the price gates. Structurally: prob >= 0.70 AND odds >= 1.50
demands a 7+ point disagreement with the market, and the repaired model hugs
the market — its honesty is what silenced it. The historical "+22.6% above
0.70" zone was fed by the broken model's overconfidence.

The owner's decision, made with the trade-off stated: recommend outcomes where
model AND de-vigged market AGREE at high confidence. No edge claimed, long-run
expectation minus the margin, goal = hit rate. The channel is labelled,
persisted and measured separately so it can never blur the value record.
"""
from __future__ import annotations

import inspect

import pytest

from betbot.analysis import ValueBet, detect_value_bets
from betbot.models import TeamStats


def _price(p: float, overround: float = 1.06) -> float:
    return round(1.0 / (p * overround), 4)


def _event(p_home=0.74, p_draw=0.16, p_away=0.10):
    """A clear favourite, priced as such by two books."""
    return {
        "id": "evt-fav", "sport_key": "soccer_epl",
        "commence_time": "2026-08-29T15:00:00Z",
        "home_team": "Alpha", "away_team": "Beta",
        "bookmakers": [
            {"key": k, "title": t, "markets": [
                {"key": "h2h", "outcomes": [
                    {"name": "Alpha", "price": _price(p_home)},
                    {"name": "Draw", "price": _price(p_draw)},
                    {"name": "Beta", "price": _price(p_away)},
                ]},
            ]}
            for k, t in (("betclic", "Betclic (FR)"), ("bet365", "Bet365"))
        ],
    }


def _stats(name, atk):
    return TeamStats(name=name, attack_home=atk, defense_home=1.0,
                     attack_away=atk, defense_away=1.0, matches_analyzed=30)


def _prebuilt(strong=1.55, weak=0.62):
    return {"soccer_epl": {
        "teams": {"Alpha": _stats("Alpha", strong), "Beta": _stats("Beta", weak)},
        "home_avg": 1.55, "away_avg": 1.10, "h2h": {},
    }}


def _run(event=None, prebuilt=None, **kw):
    params = dict(
        events_by_sport={"soccer_epl": [event or _event()]},
        match_history_by_sport={},
        prebuilt_stats_by_sport=prebuilt if prebuilt is not None else _prebuilt(),
        bankroll=100.0, kelly_fraction=0.25,
        min_value_edge=0.04, max_value_edge=0.0,
        min_model_prob=0.70, min_model_prob_totals=0.55,
        min_book_odds=1.50, max_book_odds=2.22,
        min_edge_vs_novig=0.03, novig_required=True,
        underdog_odds=3.0, underdog_min_prob=0.42,
        derive_dc_dnb=False, derive_dnb=False, allow_totals_over=False,
        favorites_channel=True, favorites_min_prob=0.70,
        favorites_min_odds=1.20,
    )
    params.update(kw)
    return detect_value_bets(**params)


def _favs(bets):
    return [b for b in bets if b.channel == "favoris"]


# ---------------------------------------------------------------------------
# The channel fires exactly on agreement
# ---------------------------------------------------------------------------

def test_agreement_produces_a_favourite_where_value_produces_nothing():
    """The production bind, reproduced: a 74% favourite prices ~1.27 — under
    the 1.50 value gate — so the value channel is silent. The agreement
    channel is the one that speaks."""
    bets = _run()

    assert not [b for b in bets if b.channel == "valeur"], (
        "the value gates cannot retain a true favourite priced at ~1.27")
    favs = _favs(bets)
    assert favs, "model and market both >= 0.70 must yield a favourite"
    assert favs[0].selection_code == "1"
    assert favs[0].market_prob is not None and favs[0].market_prob >= 0.70


def test_no_agreement_no_favourite_even_if_the_model_is_sure():
    """Model says 74%, market says 55%: that is a DISAGREEMENT — the value
    channel's territory, and historically the market's win."""
    ev = _event(p_home=0.55, p_draw=0.25, p_away=0.20)

    assert not _favs(_run(event=ev))


def test_market_agrees_but_model_does_not_no_favourite():
    """Market at 74% but the model lukewarm: no pick — the channel requires
    BOTH voices, that is its entire definition."""
    bets = _run(prebuilt=_prebuilt(strong=1.05, weak=1.00))

    assert not _favs(bets)


def test_the_odds_ceiling_still_binds():
    """A 'favourite' quoted above 2.22 is not a favourite — and that ceiling
    is the strongest measured filter in the system."""
    ev = _event(p_home=0.72, p_draw=0.17, p_away=0.11)
    for bm in ev["bookmakers"]:
        bm["markets"][0]["outcomes"][0]["price"] = 2.60

    assert not _favs(_run(event=ev))


def test_a_tiny_price_is_not_worth_recommending():
    ev = _event(p_home=0.90, p_draw=0.06, p_away=0.04)  # prices ~1.05

    assert not _favs(_run(event=ev, prebuilt=_prebuilt(strong=2.0, weak=0.45)))


def test_totals_never_enter_the_favourites_channel():
    """Totals are in shadow observation — a probe and a recommendation must
    never share a channel."""
    bets = _run(allow_totals_over=True)

    assert not [b for b in _favs(bets) if b.market == "totals"]


# ---------------------------------------------------------------------------
# Honest bookkeeping
# ---------------------------------------------------------------------------

def test_a_favourite_claims_no_stake():
    """kelly sizes an edge; this channel claims none. Stake stays 0 and the
    email says 'mise libre'."""
    favs = _favs(_run())

    assert favs and all(b.kelly_stake == 0.0 for b in favs)


def test_channel_defaults_keep_legacy_picks_in_valeur():
    assert ValueBet.__dataclass_fields__["channel"].default == "valeur"

    from betbot.db import Database
    assert "channel" in inspect.signature(Database.save_prediction).parameters

    from betbot.orm_models import Prediction
    assert hasattr(Prediction, "channel")


def test_the_migration_backfills_legacy_rows_as_valeur():
    import re
    from pathlib import Path

    mig = Path("alembic/versions/p0e3g7i9d2f6_add_channel.py").read_text(
        encoding="utf-8")
    assert re.search(r'down_revision\s*=\s*"o9d2f6h8c1e5"', mig)
    assert 'server_default="valeur"' in mig


def test_the_email_never_dresses_favourites_as_value():
    """No edge column, no Kelly stake, and the expectation in plain text."""
    from betbot.notifier import _render_favorites_section

    html = _render_favorites_section(_favs(_run()))

    assert "Favoris calibrés" in html
    assert "marge du bookmaker" in html
    assert "taux de réussite" in html
    assert "Mise libre" in html
    assert "edge" not in html.lower()


def test_an_empty_favourites_list_renders_nothing():
    from betbot.notifier import _render_favorites_section

    assert _render_favorites_section([]) == ""


def test_disabled_channel_emits_nothing():
    assert not _favs(_run(favorites_channel=False))


def test_a_value_pick_supersedes_its_favourite_twin():
    """If the value gates DID retain the pick, the favourite duplicate must
    vanish — one selection, one channel, the stronger claim wins."""
    ev = _event(p_home=0.74, p_draw=0.16, p_away=0.10)
    # a third book misprices the favourite generously enough to clear the
    # value gates (odds >= 1.50 with model at ~0.74)
    ev["bookmakers"].append({"key": "pinnacle", "title": "Pinnacle", "markets": [
        {"key": "h2h", "outcomes": [
            {"name": "Alpha", "price": 1.55},
            {"name": "Draw", "price": _price(0.16)},
            {"name": "Beta", "price": _price(0.10)},
        ]},
    ]})
    bets = _run(event=ev, min_edge_vs_novig=0.0, novig_required=False)

    keys_val = {(b.event_id, b.market, b.selection_code)
                for b in bets if b.channel == "valeur"}
    keys_fav = {(b.event_id, b.market, b.selection_code)
                for b in bets if b.channel == "favoris"}
    assert not (keys_val & keys_fav)


# ---------------------------------------------------------------------------
# In-play joins the agreement principle
# ---------------------------------------------------------------------------

def test_live_requires_market_agreement():
    """Both in-play picks ever produced were giant-disagreement draws (0.88 vs
    ~0.50, then 0.71 vs 0.39); both lost. The floor + min-odds clamp mean an
    in-play VALUE pick can only exist on a 7+ point disagreement — so in-play
    now demands what the favourites channel demands."""
    from pathlib import Path

    src = Path("betbot/live.py").read_text(encoding="utf-8")
    assert "if novig is None or novig < min_model_prob:" in src
    assert '"channel": "favoris"' in src


def test_probes_are_exempt_from_price_gates():
    """A probe has no stake; price bounds starved the totals sample twice."""
    bets = _run(allow_totals_over=False, min_book_odds=1.50)
    # the fixture's Under prices ~1.45 — a probe must still come through when
    # its direction survives the deterministic sampling contingent
    from betbot.analysis import _keep_totals_sample
    expected = {d for d in ("Over", "Under")
                if _keep_totals_sample("evt-fav", d)}
    got = {b.selection_code[0] for b in bets if b.market == "totals"}
    mapping = {"O": "Over", "U": "Under"}
    assert {mapping[g] for g in got} <= expected


# ---------------------------------------------------------------------------
# Combos carry the same promise as their legs
# ---------------------------------------------------------------------------

def test_the_daily_scan_no_longer_uses_the_relaxation_ladder():
    """During the September international break the ladder quietly emailed
    combos whose legs were below every floor (prob down to 0.30, consensus
    model) and never persisted. The singles ledger read 13/13 while the owner
    lost money on the one product that had escaped both the discipline and
    the measurement. A combo's legs now clear the same bar as everything
    else, or there is no combo."""
    from pathlib import Path

    src = Path("betbot/main.py").read_text(encoding="utf-8")
    scan = src[src.index("def run_daily_scan"):src.index("\ndef ", src.index("def run_daily_scan") + 10)]
    assert "_ensure_min_combos(" not in scan, (
        "the daily scan must not fill combos by loosening thresholds")
    assert "parlays = build_parlays(favoris_bets" in scan, (
        "combos are built from persisted, graded favourite legs only")


def test_no_favourites_means_no_combos():
    """An empty day yields an honest empty email, never a fabricated ticket."""
    from pathlib import Path

    src = Path("betbot/main.py").read_text(encoding="utf-8")
    scan = src[src.index("def run_daily_scan"):]
    assert "parlays = []" in scan
