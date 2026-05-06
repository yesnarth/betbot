"""
Backtest engine — validates the prediction model on historical match results.

Without paid access to historical odds, we can't backtest *EV* directly. But
we CAN validate the most fundamental property of the model: are its
probabilities well-calibrated?

Three metrics, each measuring something different:

  1. **Brier score** (multiclass) — sum of squared errors across the 3 outcomes.
     Lower is better. Baselines:
       - perfect model           = 0.0
       - "always 1/3 each"       = 2/3 ≈ 0.667
       - "always picks home win" = 1.34 (very bad)
  2. **Log-loss** (cross-entropy) — heavily penalizes confident wrong
     predictions. Better than Brier when over-confidence is dangerous.
  3. **Calibration buckets** — split predictions by probability decile and
     check that, e.g., bets with predicted 60-70% probability actually win
     about 65% of the time. A well-calibrated model is plottable on a
     diagonal.

Workflow:
  bt = run_backtest(sport_key="soccer_epl", n_holdout=100)
  print(bt["brier"])              # 0.18 = good
  print(bt["calibration"])        # buckets of predicted vs actual

Use this BEFORE tweaking model weights — if calibration is already good,
don't micro-optimize.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import TypedDict

from betbot.data_sources import club_elo, understat
from betbot.football_api import FootballDataClient, LEAGUE_MAP, parse_match_results
from betbot.models import (
    DEFAULT_AWAY_AVG,
    DEFAULT_HOME_AVG,
    TeamStats,
    blended_match_probs,
    build_team_stats,
    compute_league_averages,
)

logger = logging.getLogger("betbot.backtest")


class CalibrationBucket(TypedDict):
    range: str
    n_samples: int
    predicted_avg: float
    actual_avg: float
    abs_error: float


@dataclass
class BacktestResult:
    sport_key: str
    n_matches: int
    brier_score: float
    log_loss: float
    calibration: list[CalibrationBucket] = field(default_factory=list)
    notes: str = ""


def _outcome_index(home_goals: int, away_goals: int) -> int:
    """Return 0 for home win, 1 for draw, 2 for away win."""
    if home_goals > away_goals:
        return 0
    if home_goals == away_goals:
        return 1
    return 2


def _brier_multiclass(probs: tuple[float, float, float], actual_idx: int) -> float:
    """Multiclass Brier = sum_k (p_k - y_k)^2 across the 3 outcomes."""
    return sum(
        (p - (1.0 if i == actual_idx else 0.0)) ** 2
        for i, p in enumerate(probs)
    )


def _log_loss(probs: tuple[float, float, float], actual_idx: int) -> float:
    """Cross-entropy on the actual class. Clipped to avoid log(0)."""
    p = max(min(probs[actual_idx], 0.9999), 0.0001)
    return -math.log(p)


def _calibration_buckets(samples: list[tuple[float, int]]) -> list[CalibrationBucket]:
    """
    samples: list of (predicted_prob, was_correct in {0,1}) pairs.
    Buckets by predicted prob in 10% increments.
    """
    edges = [(i / 10, (i + 1) / 10) for i in range(10)]
    out: list[CalibrationBucket] = []
    for lo, hi in edges:
        bucket = [(p, c) for p, c in samples if lo <= p < hi or (hi == 1.0 and p == 1.0)]
        if not bucket:
            continue
        n = len(bucket)
        pred_avg = sum(p for p, _ in bucket) / n
        actual_avg = sum(c for _, c in bucket) / n
        out.append(CalibrationBucket(
            range=f"{int(lo*100)}-{int(hi*100)}%",
            n_samples=n,
            predicted_avg=round(pred_avg, 3),
            actual_avg=round(actual_avg, 3),
            abs_error=round(abs(pred_avg - actual_avg), 3),
        ))
    return out


def run_backtest(
    sport_key: str,
    fd_api_key: str,
    n_holdout: int = 100,
    use_enrichment: bool = False,
) -> BacktestResult:
    """
    Walk-forward backtest — strictly causal evaluation.

    For each match M in the holdout window, team_stats are rebuilt using ONLY
    the matches that occurred BEFORE M's date. No future information leaks
    into the prediction. This is the only honest way to estimate generalization
    error on time-series data.

    `use_enrichment=False` by default because ELO/xG snapshots are "today's"
    values, which DO contain look-ahead. Use `True` if you want a "best-case"
    upper-bound estimate (typically 5-10% better Brier than the strict run).
    """
    comp_code = LEAGUE_MAP.get(sport_key)
    if not comp_code:
        return BacktestResult(
            sport_key=sport_key, n_matches=0, brier_score=0.0, log_loss=0.0,
            notes=f"sport_key {sport_key} non supporté par football-data.org",
        )

    fd = FootballDataClient(fd_api_key)
    raw = fd.get_recent_matches(comp_code, limit=300)
    parsed = parse_match_results(raw)
    if len(parsed) < n_holdout + 20:
        return BacktestResult(
            sport_key=sport_key, n_matches=0, brier_score=0.0, log_loss=0.0,
            notes=f"Not enough historical matches ({len(parsed)} < {n_holdout + 20})",
        )

    # Sort chronologically (oldest → newest) so we can slice train/holdout cleanly
    parsed.sort(key=lambda m: m.get("date", ""))
    holdout_window = parsed[-n_holdout:]

    # Optional enrichment snapshot. Gives an OPTIMISTIC bound; off by default.
    elo_snapshot: dict = {}
    xg_by_title: dict = {}
    if use_enrichment:
        try:
            elo_snapshot = club_elo.get_all_elo_ratings()
        except Exception as exc:
            logger.warning("Backtest: ELO unavailable (%s)", exc)
        try:
            xg_teams = understat.get_league_xg(sport_key)
            xg_by_title = {t["title"].lower(): t for t in xg_teams}
        except Exception as exc:
            logger.warning("Backtest: Understat unavailable (%s)", exc)

    def _build_walk_forward_cache(train_subset: list[dict]) -> tuple[dict[str, TeamStats], float, float]:
        """Recompute team stats and league averages on a strictly-prior subset."""
        league_h, league_a = compute_league_averages(train_subset)
        teams = {m["home_team"] for m in train_subset} | {m["away_team"] for m in train_subset}
        cache: dict[str, TeamStats] = {}
        for team in teams:
            ts = build_team_stats(team, train_subset, league_h, league_a)
            if not ts:
                continue
            if use_enrichment:
                norm = club_elo._normalize(team)
                ts.elo_rating = elo_snapshot.get(norm)
                if ts.elo_rating is None:
                    for k, v in elo_snapshot.items():
                        if len(norm) >= 5 and (norm in k or k in norm):
                            ts.elo_rating = v
                            break
                tlc = team.lower()
                for tl, txg in xg_by_title.items():
                    if tl == tlc or tl in tlc or tlc in tl:
                        ts.xg_for = txg["xg_per_match"]
                        ts.xg_against = txg["xga_per_match"]
                        break
            cache[team] = ts
        return cache, league_h, league_a

    # Walk-forward scoring loop
    samples_home: list[tuple[float, int]] = []
    samples_draw: list[tuple[float, int]] = []
    samples_away: list[tuple[float, int]] = []
    brier_total = 0.0
    log_loss_total = 0.0
    n_scored = 0
    n_skipped_no_history = 0

    # Pre-compute: for each holdout date, the index where train ends (parsed is sorted)
    # This avoids O(N²) total complexity — we re-scan parsed once and reuse cache
    # boundaries. We rebuild team_stats only when the date changes.
    last_date: str | None = None
    cache: dict[str, TeamStats] = {}
    league_home_avg: float = DEFAULT_HOME_AVG
    league_away_avg: float = DEFAULT_AWAY_AVG

    for m in holdout_window:
        match_date = m.get("date", "")
        if match_date != last_date:
            train_subset = [tm for tm in parsed if tm.get("date", "") < match_date]
            if len(train_subset) < 20:
                n_skipped_no_history += 1
                continue
            cache, league_home_avg, league_away_avg = _build_walk_forward_cache(train_subset)
            last_date = match_date

        home, away = m["home_team"], m["away_team"]
        h, a = cache.get(home), cache.get(away)
        if not h or not a:
            continue
        try:
            probs = blended_match_probs(
                home_stats=h, away_stats=a,
                league_home_avg=league_home_avg or DEFAULT_HOME_AVG,
                league_away_avg=league_away_avg or DEFAULT_AWAY_AVG,
            )
        except Exception:
            continue

        actual_idx = _outcome_index(m["home_goals"], m["away_goals"])
        triple = (probs.home_win, probs.draw, probs.away_win)
        brier_total += _brier_multiclass(triple, actual_idx)
        log_loss_total += _log_loss(triple, actual_idx)
        n_scored += 1

        samples_home.append((probs.home_win, 1 if actual_idx == 0 else 0))
        samples_draw.append((probs.draw, 1 if actual_idx == 1 else 0))
        samples_away.append((probs.away_win, 1 if actual_idx == 2 else 0))

    if n_scored == 0:
        return BacktestResult(
            sport_key=sport_key, n_matches=0, brier_score=0.0, log_loss=0.0,
            notes=f"No predictions scored ({n_skipped_no_history} skipped for lack of prior history)",
        )

    calibration = _calibration_buckets(samples_home + samples_away)

    notes = (f"walk-forward, enrichment={'ON' if use_enrichment else 'OFF'}, "
             f"skipped (no history): {n_skipped_no_history}")
    return BacktestResult(
        sport_key=sport_key,
        n_matches=n_scored,
        brier_score=round(brier_total / n_scored, 4),
        log_loss=round(log_loss_total / n_scored, 4),
        calibration=calibration,
        notes=notes,
    )


def backtest_summary(result: BacktestResult) -> str:
    """Render a one-screen text report — for the dashboard or CLI."""
    lines = [
        f"Backtest {result.sport_key}",
        f"  Matchs scorés     : {result.n_matches}",
        f"  Brier score       : {result.brier_score:.4f}  (lower is better; ~0.667 = baseline 1/3, 0 = perfect)",
        f"  Log-loss          : {result.log_loss:.4f}    (lower is better)",
        f"  Notes             : {result.notes}",
        "",
        "Calibration (predicted vs actual hit rate par décile) :",
    ]
    for b in result.calibration:
        lines.append(
            f"  [{b['range']:>7}]  n={b['n_samples']:>3}  "
            f"prédit={b['predicted_avg']:.2f}  observé={b['actual_avg']:.2f}  "
            f"|écart|={b['abs_error']:.2f}"
        )
    return "\n".join(lines)
