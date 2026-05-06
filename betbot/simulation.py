"""
Monte Carlo simulator — answer "what's the probability of going broke in 6
months given my model, my Kelly strategy, my filters?"

Why: a +EV strategy can still go bust due to variance, especially if Kelly
fraction is too aggressive or the bankroll is too small. This module rejoue
N trajectoires aléatoires en utilisant les vraies probabilités du modèle
et révèle la distribution des résultats — pas juste l'espérance.

Pas un fake "if I had bet X back then" — c'est une vraie simulation forward
qui assume:
  - Le modèle est correctement calibré (probabilité = fréquence réelle)
  - On parie selon Kelly fractionnel
  - Chaque pari est résolu indépendamment (loi binomiale)

Output: distribution de la balance finale + P(faillite) + drawdown max.
"""
from __future__ import annotations

import logging
import math
import random
from dataclasses import dataclass

logger = logging.getLogger("betbot.simulation")


@dataclass
class StrategyParams:
    """Inputs that fully define a simulation."""
    starting_bankroll: float = 100.0
    n_bets: int = 200
    avg_edge: float = 0.05            # mean of the value_edge distribution
    edge_std: float = 0.03            # spread of edges
    avg_odds: float = 2.5             # mean of decimal odds
    odds_std: float = 0.8
    kelly_fraction: float = 0.25
    max_kelly_fraction: float = 0.05  # cap per bet
    ruin_threshold: float = 0.10      # below this fraction of starting → "broke"


@dataclass
class SimulationResult:
    n_trajectories: int
    final_balance_mean: float
    final_balance_median: float
    final_balance_p5: float            # 5th percentile (worst case)
    final_balance_p95: float           # 95th percentile (best case)
    p_broke: float                     # fraction of trajectories that hit ruin
    p_doubled: float                   # fraction that hit ≥ 2× starting bankroll
    avg_max_drawdown_pct: float        # mean of max drawdown across trajectories
    notes: str = ""


def _kelly_size(prob: float, odds: float, fraction: float, max_fraction: float) -> float:
    """Fractional Kelly capped at max_fraction (mirrors analysis.kelly_stake)."""
    if odds <= 1.0 or prob <= 0:
        return 0.0
    b = odds - 1.0
    full = (b * prob - (1 - prob)) / b
    if full <= 0:
        return 0.0
    return min(full * fraction, max_fraction)


def _simulate_one_trajectory(params: StrategyParams, rng: random.Random) -> tuple[float, float, bool]:
    """
    Play `params.n_bets` bets, using random edges/odds drawn from the param
    distributions. Returns (final_balance, max_drawdown_pct, went_broke).
    """
    balance = params.starting_bankroll
    peak = balance
    max_drawdown = 0.0
    ruin = params.starting_bankroll * params.ruin_threshold

    for _ in range(params.n_bets):
        if balance <= ruin:
            return balance, max_drawdown, True

        # Sample random odds and the edge we have on this bet
        odds = max(1.10, rng.gauss(params.avg_odds, params.odds_std))
        edge = rng.gauss(params.avg_edge, params.edge_std)
        # True probability = (1 + edge) / odds  (the model is correctly calibrated)
        true_prob = max(0.01, min(0.99, (1.0 + edge) / odds))

        # Kelly stake based on the bet's specific prob × odds
        fraction = _kelly_size(true_prob, odds,
                               params.kelly_fraction, params.max_kelly_fraction)
        stake = balance * fraction
        if stake <= 0:
            continue

        # Resolve: with probability true_prob → win, otherwise lose
        if rng.random() < true_prob:
            balance += stake * (odds - 1)
        else:
            balance -= stake

        # Track drawdown
        peak = max(peak, balance)
        drawdown = (peak - balance) / peak if peak > 0 else 0
        max_drawdown = max(max_drawdown, drawdown)

    return balance, max_drawdown, False


def run_simulation(
    params: StrategyParams | None = None,
    n_trajectories: int = 1000,
    seed: int | None = None,
) -> SimulationResult:
    """
    Replay `n_trajectories` independent betting trajectories with the same
    strategy parameters. Returns aggregated statistics.

    Set `seed` for reproducible runs (e.g. in tests).
    """
    params = params or StrategyParams()
    rng = random.Random(seed)
    finals: list[float] = []
    drawdowns: list[float] = []
    n_broke = 0
    target_double = params.starting_bankroll * 2

    for _ in range(n_trajectories):
        final, dd, broke = _simulate_one_trajectory(params, rng)
        finals.append(final)
        drawdowns.append(dd)
        if broke:
            n_broke += 1

    finals.sort()
    p5 = finals[int(0.05 * n_trajectories)]
    p95 = finals[int(0.95 * n_trajectories)]
    median = finals[n_trajectories // 2]
    n_doubled = sum(1 for f in finals if f >= target_double)

    return SimulationResult(
        n_trajectories=n_trajectories,
        final_balance_mean=round(sum(finals) / n_trajectories, 2),
        final_balance_median=round(median, 2),
        final_balance_p5=round(p5, 2),
        final_balance_p95=round(p95, 2),
        p_broke=round(n_broke / n_trajectories, 4),
        p_doubled=round(n_doubled / n_trajectories, 4),
        avg_max_drawdown_pct=round(sum(drawdowns) / n_trajectories * 100, 2),
        notes=(
            f"params: edge={params.avg_edge:+.1%}±{params.edge_std:.1%}, "
            f"odds={params.avg_odds:.1f}±{params.odds_std:.1f}, "
            f"Kelly={params.kelly_fraction*100:.0f}% (cap {params.max_kelly_fraction*100:.0f}%)"
        ),
    )
