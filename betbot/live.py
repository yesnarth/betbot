"""Live (in-play) scanner — football, basketball, tennis.

Turns the existing PRE-MATCH outputs into IN-PLAY probabilities by folding in the
current score + time/sets remaining, then looks for value against the LIVE odds
The Odds API serves for in-progress games.

Honest limits (surfaced in the UI):
  - Data is ~30 s stale and you place manually → this is a SCANNER (place fast),
    not automated trading.
  - Football/basket "minute" is estimated from wall-clock elapsed since kick-off
    (the feed gives no match clock) → half-time / stoppage are approximate.
    Tennis uses the set state (cleaner).
  - Live = SINGLE bets only (no parlays).
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone

from betbot.analysis import _novig_fair_prob, kelly_stake
from betbot.models import MatchProbs, extract_best_odds
from betbot.reliability import compute_reliability

logger = logging.getLogger("betbot.live")

# Full-game scales used to turn "expected total" into "expected remaining".
_BASKET_MINUTES = {"nba": 48.0, "euroleague": 40.0}
_BASKET_MARGIN_STD = 11.0  # mirrors basketball_model.MARGIN_STD_BASE
# Men's Grand Slams are best-of-5; everything else best-of-3.
_BO5_SPORTS = {
    "tennis_atp_aus_open", "tennis_atp_french_open",
    "tennis_atp_wimbledon", "tennis_atp_us_open",
}


# ---------------------------------------------------------------------------
# In-play probability models (pure)
# ---------------------------------------------------------------------------

def inplay_football_probs(
    lambda_home: float, lambda_away: float, frac_left: float,
    goals_home: int, goals_away: int, max_remaining: int = 10,
) -> MatchProbs:
    """Final-result probabilities given the current score and the fraction of the
    match left. Remaining goals ~ independent Poisson(λ × frac_left); add the
    current score. (Plain Poisson on the remainder — no Dixon-Coles τ, which only
    matters for low-score correlation at kick-off.)"""
    from scipy.stats import poisson

    lh = max(0.0, lambda_home) * max(0.0, frac_left)
    la = max(0.0, lambda_away) * max(0.0, frac_left)
    ph = [float(poisson.pmf(k, lh)) for k in range(max_remaining + 1)]
    pa = [float(poisson.pmf(k, la)) for k in range(max_remaining + 1)]
    ph[-1] += max(0.0, 1.0 - sum(ph))   # dump the tail mass on the last bucket
    pa[-1] += max(0.0, 1.0 - sum(pa))

    p_home = p_draw = p_away = 0.0
    over15 = over25 = over35 = 0.0
    for rh in range(max_remaining + 1):
        for ra in range(max_remaining + 1):
            p = ph[rh] * pa[ra]
            fh, fa = goals_home + rh, goals_away + ra
            if fh > fa:
                p_home += p
            elif fh == fa:
                p_draw += p
            else:
                p_away += p
            total = fh + fa
            if total >= 2:
                over15 += p
            if total >= 3:
                over25 += p
            if total >= 4:
                over35 += p
    return MatchProbs(
        home_win=round(p_home, 4), draw=round(p_draw, 4), away_win=round(p_away, 4),
        over_15=round(over15, 4), under_15=round(1 - over15, 4),
        over_25=round(over25, 4), under_25=round(1 - over25, 4),
        over_35=round(over35, 4), under_35=round(1 - over35, 4),
        lambda_home=round(goals_home + lh, 3), lambda_away=round(goals_away + la, 3),
        model="inplay_football",
    )


def inplay_basketball_probs(
    exp_home: float, exp_away: float, frac_left: float,
    cur_home: int, cur_away: int, sport_key: str | None = None,
) -> MatchProbs:
    """Win probability given the current score and time left. Remaining points ~
    Normal(expected × frac_left, σ·√frac_left); add the current score → final
    margin → P(home win)."""
    from scipy.stats import norm

    f = max(0.0, min(1.0, frac_left))
    final_home = cur_home + max(0.0, exp_home) * f
    final_away = cur_away + max(0.0, exp_away) * f
    mean_margin = final_home - final_away
    sigma = max(0.5, _BASKET_MARGIN_STD * math.sqrt(max(f, 1e-9)))
    p_home = float(norm.sf(0.0, loc=mean_margin, scale=sigma))
    league = "euroleague" if "euroleague" in (sport_key or "") else "nba"
    return MatchProbs(
        home_win=round(p_home, 4), draw=0.0, away_win=round(1.0 - p_home, 4),
        over_25=0.0,
        lambda_home=round(final_home, 1), lambda_away=round(final_away, 1),
        model=f"inplay_basket_{league}",
    )


def _prob_win_match(p: float, need_home: int, need_away: int) -> float:
    """P(home wins `need_home` more sets before away wins `need_away`), each set
    won by home w.p. p. Simple recursion (memo not needed — best-of ≤ 5)."""
    if need_home <= 0:
        return 1.0
    if need_away <= 0:
        return 0.0
    return p * _prob_win_match(p, need_home - 1, need_away) + \
        (1.0 - p) * _prob_win_match(p, need_home, need_away - 1)


def _per_set_prob_from_match(match_p: float, best_of: int) -> float:
    """Invert: find the per-set win prob p that yields the given pre-match match
    win prob, via bisection."""
    need = best_of // 2 + 1
    lo, hi = 0.0, 1.0
    for _ in range(40):
        mid = (lo + hi) / 2.0
        if _prob_win_match(mid, need, need) < match_p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def inplay_tennis_probs(
    prematch_home_win: float, sets_home: int, sets_away: int, best_of: int = 3,
) -> float:
    """In-play match win prob for the home player given current sets won. Derives
    a per-set prob from the pre-match ELO win prob, then recurses over the sets
    still needed."""
    need = best_of // 2 + 1
    p = _per_set_prob_from_match(max(0.001, min(0.999, prematch_home_win)), best_of)
    return round(_prob_win_match(p, need - sets_home, need - sets_away), 4)


# ---------------------------------------------------------------------------
# Live data helpers
# ---------------------------------------------------------------------------

def filter_live(events: list[dict], now: datetime | None = None) -> list[dict]:
    """Keep only events already in progress (commence_time in the past)."""
    now = now or datetime.now(timezone.utc)
    live = []
    for ev in events:
        ct = ev.get("commence_time")
        if not ct:
            continue
        try:
            start = datetime.fromisoformat(str(ct).replace("Z", "+00:00"))
        except ValueError:
            continue
        if start <= now:
            live.append(ev)
    return live


def _elapsed_minutes(event: dict, now: datetime) -> float:
    try:
        start = datetime.fromisoformat(str(event["commence_time"]).replace("Z", "+00:00"))
    except (KeyError, ValueError, TypeError):
        return 0.0
    return max(0.0, (now - start).total_seconds() / 60.0)


def _frac_left(sport_key: str, elapsed_min: float) -> float:
    if sport_key.startswith("basketball_"):
        league = "euroleague" if "euroleague" in sport_key else "nba"
        total = _BASKET_MINUTES[league]
        return max(0.0, min(1.0, (total - elapsed_min) / total))
    # football
    return max(0.0, min(1.0, (90.0 - elapsed_min) / 90.0))


def _score_for(score_event: dict, home: str, away: str) -> tuple[int, int] | None:
    """Extract (home_score, away_score) as ints from a /scores event."""
    sm: dict[str, int] = {}
    for s in score_event.get("scores") or []:
        try:
            sm[s.get("name", "")] = int(s["score"])
        except (KeyError, ValueError, TypeError):
            return None
    h, a = sm.get(home), sm.get(away)
    if h is None or a is None:
        return None
    return h, a


# ---------------------------------------------------------------------------
# Live value scan
# ---------------------------------------------------------------------------

def scan_live(
    events_by_sport: dict[str, list[dict]],
    scores_by_sport: dict[str, list[dict]],
    prebuilt_stats_by_sport: dict[str, dict],
    *,
    bankroll: float,
    kelly_fraction: float = 0.25,
    min_value_edge: float = 0.04,
    min_book_odds: float = 1.30,
    min_edge_vs_novig: float = 0.0,
    now: datetime | None = None,
) -> list[dict]:
    """Return live value bets (SINGLES). Reuses _compute_probs for the pre-match
    base, applies the in-play transform, then the same value gate as pre-match."""
    from betbot.analysis import _compute_probs, _sport_key_to_label
    from betbot.models import DEFAULT_AWAY_AVG, DEFAULT_HOME_AVG

    now = now or datetime.now(timezone.utc)
    out: list[dict] = []

    for sport_key, events in events_by_sport.items():
        score_map = {
            e["id"]: e for e in scores_by_sport.get(sport_key, [])
            if e.get("id") and not e.get("completed") and e.get("scores")
        }
        if not score_map:
            continue
        entry = prebuilt_stats_by_sport.get(sport_key, {})
        team_cache = entry.get("teams", {})
        home_avg = entry.get("home_avg", DEFAULT_HOME_AVG)
        away_avg = entry.get("away_avg", DEFAULT_AWAY_AVG)
        is_tennis = sport_key.startswith("tennis_")
        is_basket = sport_key.startswith("basketball_")
        label = _sport_key_to_label(sport_key)

        for ev in events:
            sc = score_map.get(ev.get("id"))
            if not sc:
                continue
            home, away = ev.get("home_team", ""), ev.get("away_team", "")
            parsed = _score_for(sc, home, away)
            if parsed is None:
                continue
            cur_h, cur_a = parsed

            pre = _compute_probs(home, away, ev, team_cache, home_avg, away_avg, sport_key=sport_key)
            if pre is None:
                continue

            if is_tennis:
                best_of = 5 if sport_key in _BO5_SPORTS else 3
                if cur_h + cur_a > best_of:        # score looks like games, not sets → bail
                    continue
                hw = inplay_tennis_probs(pre.home_win, cur_h, cur_a, best_of)
                probs = MatchProbs(home_win=hw, draw=0.0, away_win=round(1 - hw, 4),
                                   over_25=0.0, model=f"inplay_tennis_bo{best_of}")
                outcome_map = [("1", "Victoire joueur 1", home, "h2h", None, probs.home_win),
                               ("2", "Victoire joueur 2", away, "h2h", None, probs.away_win)]
            elif is_basket:
                frac = _frac_left(sport_key, _elapsed_minutes(ev, now))
                if frac <= 0.01:
                    continue
                probs = inplay_basketball_probs(pre.lambda_home, pre.lambda_away, frac,
                                                cur_h, cur_a, sport_key)
                outcome_map = [("1", "Victoire domicile", home, "h2h", None, probs.home_win),
                               ("2", "Victoire extérieur", away, "h2h", None, probs.away_win)]
            else:  # football
                frac = _frac_left(sport_key, _elapsed_minutes(ev, now))
                if frac <= 0.01:
                    continue
                probs = inplay_football_probs(pre.lambda_home, pre.lambda_away, frac, cur_h, cur_a)
                outcome_map = [
                    ("1", "Victoire domicile", home, "h2h", None, probs.home_win),
                    ("X", "Match nul", "Draw", "h2h", None, probs.draw),
                    ("2", "Victoire extérieur", away, "h2h", None, probs.away_win),
                    ("O25", "Plus de 2.5 buts", "Over", "totals", 2.5, probs.over_25),
                    ("U25", "Moins de 2.5 buts", "Under", "totals", 2.5, probs.under_25),
                    ("O35", "Plus de 3.5 buts", "Over", "totals", 3.5, probs.over_35),
                    ("U35", "Moins de 3.5 buts", "Under", "totals", 3.5, probs.under_35),
                ]

            group_names_by_market: dict = {}
            for code, _l, nm, mk, pt, _p in outcome_map:
                group_names_by_market.setdefault((mk, pt), set()).add(nm)

            for code, lbl, outcome_name, market_key, point, model_prob in outcome_map:
                if model_prob <= 0.0:
                    continue
                best = extract_best_odds(ev, outcome_name, market_key=market_key, point=point)
                if best is None or best.price < min_book_odds:
                    continue
                if min_edge_vs_novig > 0.0:
                    novig = _novig_fair_prob(ev, outcome_name, market_key, point,
                                             group_names_by_market[(market_key, point)])
                    if novig is not None and novig > 0.0 and (model_prob / novig - 1.0) < min_edge_vs_novig:
                        continue
                edge = round(model_prob * best.price - 1.0, 4)
                if edge < min_value_edge:
                    continue
                reliability = compute_reliability(model_prob=model_prob, value_edge=edge,
                                                  model_type=probs.model, n_matches=None)
                stake = kelly_stake(model_prob, best.price, bankroll, kelly_fraction,
                                    reliability=reliability)
                out.append({
                    "event_id": ev.get("id"), "sport_key": sport_key, "league": label,
                    "home_team": home, "away_team": away,
                    "live_score": f"{cur_h}-{cur_a}", "market": market_key,
                    "selection_code": code, "selection_label": lbl,
                    "model_prob": round(model_prob, 4), "best_odds": best.price,
                    "best_book": best.bookmaker, "value_edge": edge,
                    "kelly_stake": stake, "model_type": probs.model,
                    "reliability": reliability,
                })

    out.sort(key=lambda b: (b["value_edge"], b["model_prob"]), reverse=True)
    return out
