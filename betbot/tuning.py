"""Per-league blend-weight optimization via walk-forward backtest.

Replaces the hardcoded elo_weight=0.30 / xg_weight=0.35 GUESSES (models.py) with
values fit per league by grid-searching to MINIMIZE log-loss on held-out matches.

HONEST CAVEAT — look-ahead: tuning the xG/ELO weights requires those signals
attached to past matches, but we only have *today's* ELO/xG snapshots, so the fit
is optimistic (it slightly over-credits the enriched signals). Treat the result
as a better-than-guessing prior; the real validation is forward CLV. We persist a
league's weights ONLY when they beat the current defaults' log-loss on the same
holdout — so tuning can never make predictions worse than today.
"""
from __future__ import annotations

import logging

from betbot.backtest import _log_loss, _outcome_index
from betbot.data_sources import club_elo, understat
from betbot.football_api import LEAGUE_MAP, FootballDataClient, parse_match_results
from betbot.models import (
    DEFAULT_AWAY_AVG,
    DEFAULT_HOME_AVG,
    blended_match_probs,
    build_team_stats,
    compute_league_averages,
)

logger = logging.getLogger("betbot.tuning")

DEFAULT_ELO_WEIGHT = 0.30
DEFAULT_XG_WEIGHT = 0.35
# Search grid. elo_weight + xg_weight must stay ≤ 0.95 (Dixon-Coles keeps ≥5%).
ELO_GRID = (0.0, 0.15, 0.25, 0.35, 0.45)
XG_GRID = (0.0, 0.20, 0.35, 0.50)
MIN_MATCHES_TO_TUNE = 60


def _collect_inputs(sport_key: str, fd_api_key: str, n_holdout: int):
    """Walk-forward collection of (home_stats, away_stats, lh, la, actual_idx),
    with ELO/xG enrichment attached (look-ahead — see module docstring).
    Returns (inputs, note)."""
    comp_code = LEAGUE_MAP.get(sport_key)
    if not comp_code:
        return [], f"{sport_key} non supporté par football-data.org"
    fd = FootballDataClient(fd_api_key)
    parsed = parse_match_results(fd.get_recent_matches(comp_code, limit=300))
    if len(parsed) < n_holdout + 20:
        return [], f"historique insuffisant ({len(parsed)} matchs)"
    parsed.sort(key=lambda m: m.get("date", ""))
    holdout = parsed[-n_holdout:]

    elo_snapshot: dict = {}
    xg_by_title: dict = {}
    try:
        elo_snapshot = club_elo.get_all_elo_ratings()
    except Exception as exc:  # noqa: BLE001
        logger.warning("tuning: ELO unavailable (%s)", exc)
    try:
        xg_by_title = {t["title"].lower(): t for t in understat.get_league_xg(sport_key)}
    except Exception as exc:  # noqa: BLE001
        logger.warning("tuning: xG unavailable (%s)", exc)

    def _cache(train: list[dict]):
        lh, la = compute_league_averages(train)
        teams = {m["home_team"] for m in train} | {m["away_team"] for m in train}
        c = {}
        for t in teams:
            ts = build_team_stats(t, train, lh, la)
            if not ts:
                continue
            norm = club_elo._normalize(t)
            ts.elo_rating = elo_snapshot.get(norm)
            if ts.elo_rating is None:
                for k, v in elo_snapshot.items():
                    if len(norm) >= 5 and (norm in k or k in norm):
                        ts.elo_rating = v
                        break
            tlc = t.lower()
            for tl, txg in xg_by_title.items():
                if tl == tlc or tl in tlc or tlc in tl:
                    ts.xg_for = txg["xg_per_match"]
                    ts.xg_against = txg["xga_per_match"]
                    break
            c[t] = ts
        return c, lh, la

    inputs: list[tuple] = []
    last_date, cache, lh, la = None, {}, DEFAULT_HOME_AVG, DEFAULT_AWAY_AVG
    for m in holdout:
        d = m.get("date", "")
        if d != last_date:
            train = [tm for tm in parsed if tm.get("date", "") < d]
            if len(train) < 20:
                continue
            cache, lh, la = _cache(train)
            last_date = d
        h, a = cache.get(m["home_team"]), cache.get(m["away_team"])
        if not h or not a:
            continue
        inputs.append((h, a, lh or DEFAULT_HOME_AVG, la or DEFAULT_AWAY_AVG,
                       _outcome_index(int(m["home_goals"]), int(m["away_goals"]))))
    return inputs, f"{len(inputs)} matchs évalués"


def _mean_log_loss(inputs: list[tuple], elo_weight: float, xg_weight: float,
                   sport_key: str | None) -> float:
    total, n = 0.0, 0
    for h, a, lh, la, actual_idx in inputs:
        try:
            p = blended_match_probs(
                home_stats=h, away_stats=a, league_home_avg=lh, league_away_avg=la,
                elo_weight=elo_weight, xg_weight=xg_weight, sport_key=sport_key,
            )
        except Exception:  # noqa: BLE001
            continue
        total += _log_loss((p.home_win, p.draw, p.away_win), actual_idx)
        n += 1
    return (total / n) if n else float("inf")


def best_weights(inputs: list[tuple], sport_key: str | None) -> dict:
    """Pure grid-search over (elo_weight, xg_weight). Returns the best weights and
    the before/after log-loss. Never selects a combo worse than the defaults."""
    baseline = _mean_log_loss(inputs, DEFAULT_ELO_WEIGHT, DEFAULT_XG_WEIGHT, sport_key)
    best_ew, best_xw, best_ll = DEFAULT_ELO_WEIGHT, DEFAULT_XG_WEIGHT, baseline
    for ew in ELO_GRID:
        for xw in XG_GRID:
            if ew + xw > 0.95:
                continue
            ll = _mean_log_loss(inputs, ew, xw, sport_key)
            if ll < best_ll:
                best_ew, best_xw, best_ll = ew, xw, ll
    return {
        "elo_weight": round(best_ew, 3),
        "xg_weight": round(best_xw, 3),
        "log_loss_before": round(baseline, 4),
        "log_loss_after": round(best_ll, 4),
        "improved": best_ll < baseline - 1e-9,
    }


def optimize_blend(sport_key: str, fd_api_key: str, n_holdout: int = 200) -> dict:
    """Fetch holdout data, grid-search weights. Returns a status dict; the caller
    persists (betbot.blend_params.save_weights) only when `improved` is True."""
    inputs, note = _collect_inputs(sport_key, fd_api_key, n_holdout)
    if len(inputs) < MIN_MATCHES_TO_TUNE:
        return {"tuned": False, "sport_key": sport_key,
                "reason": f"trop peu de matchs ({len(inputs)} < {MIN_MATCHES_TO_TUNE}) — {note}"}
    res = best_weights(inputs, sport_key)
    res.update({"tuned": res["improved"], "sport_key": sport_key, "n_matches": len(inputs)})
    return res


# Football leagues backed by football-data.org (Poisson + xG/ELO) — the only
# ones whose blend weights are tunable. Others run consensus-only.
TUNABLE_LEAGUES = (
    "soccer_epl",
    "soccer_spain_la_liga",
    "soccer_germany_bundesliga",
    "soccer_italy_serie_a",
    "soccer_france_ligue1",
    "soccer_uefa_champs_league",
    "soccer_efl_champ",
    "soccer_netherlands_eredivisie",
    "soccer_portugal_primeira_liga",
)


def tune_all_leagues(fd_api_key: str, n_holdout: int = 200,
                     leagues: tuple[str, ...] = TUNABLE_LEAGUES) -> dict:
    """Run optimize_blend for every tunable league. Returns {sport_key: result}.
    The caller persists each result whose `tuned` is True."""
    results: dict[str, dict] = {}
    for sk in leagues:
        try:
            results[sk] = optimize_blend(sk, fd_api_key, n_holdout)
        except Exception as exc:  # noqa: BLE001
            logger.warning("tune_all: %s failed: %s", sk, exc)
            results[sk] = {"tuned": False, "sport_key": sk, "reason": str(exc)[:200]}
    return results
