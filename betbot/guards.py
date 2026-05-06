"""
Responsible-betting guards — hard limits enforced at bet placement.

These rules sit between `find_value_bets` (which produces candidates) and
`save_prediction` (which immobilizes capital). When a guard trips, the bet is
refused with a clear reason — never a silent skip.

All thresholds are configured via `.env` so the user can tune them without
touching code:

    STOP_LOSS_PCT          : if balance < this fraction of total deposits, stop
                             placing bets. Default 0.50 (-50% drawdown).
    MAX_DAILY_STAKE_PCT    : max stake total per UTC day, as fraction of balance.
                             Default 0.20 (20%).
    MAX_EXPOSURE_PCT       : max % of available capital that can be committed
                             on simultaneously-pending bets. Default 0.30.
    MAX_BETS_PER_DAY       : numerical cap on number of bets placed per UTC day.
                             Default 10.

Set any of these to 0 to disable that specific guard.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, func

from betbot.bankroll import get_state
from betbot.database import session_scope
from betbot.orm_models import BankrollEntry

logger = logging.getLogger("betbot.guards")


@dataclass
class GuardConfig:
    stop_loss_pct: float = float(os.getenv("STOP_LOSS_PCT", "0.50"))
    max_daily_stake_pct: float = float(os.getenv("MAX_DAILY_STAKE_PCT", "0.20"))
    max_exposure_pct: float = float(os.getenv("MAX_EXPOSURE_PCT", "0.30"))
    max_bets_per_day: int = int(os.getenv("MAX_BETS_PER_DAY", "10"))
    # Cool-off: after N consecutive losses, refuse new bets for `cool_off_hours`
    # hours to break tilt-spiral patterns. Set N to 0 to disable.
    cool_off_consecutive_losses: int = int(os.getenv("COOL_OFF_LOSSES", "3"))
    cool_off_hours: int = int(os.getenv("COOL_OFF_HOURS", "12"))


class GuardViolation(RuntimeError):
    """Raised when a bet placement is refused by a guard."""


def _today_utc_iso_prefix() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _consecutive_losses_streak(s, max_check: int = 20) -> tuple[int, str | None]:
    """
    Look at the last `max_check` resolved bets (newest first) and count the
    leading streak of losses. Returns (n_losses_in_streak, last_loss_ts).
    Stops at the first win/void.
    """
    from betbot.orm_models import Prediction
    rows = s.execute(
        select(Prediction.result, Prediction.resolved_at)
        .where(Prediction.result.is_not(None),
               Prediction.actually_placed.is_(True))
        .order_by(Prediction.resolved_at.desc())
        .limit(max_check)
    ).all()
    streak = 0
    last_loss_ts: str | None = None
    for result, resolved_at in rows:
        if result == "loss":
            streak += 1
            if last_loss_ts is None:
                last_loss_ts = resolved_at
        else:
            break
    return streak, last_loss_ts


def _hours_since(ts_iso: str) -> float:
    try:
        ts = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - ts).total_seconds() / 3600
    except (ValueError, AttributeError):
        return 9999.0


def _today_stake_total(s) -> tuple[float, int]:
    """Return (total_staked_today, n_bets_today)."""
    today_prefix = _today_utc_iso_prefix()
    rows = s.execute(
        select(
            func.coalesce(func.sum(-BankrollEntry.amount), 0.0),
            func.count(BankrollEntry.id),
        ).where(
            BankrollEntry.kind == "bet_placed",
            BankrollEntry.ts.like(f"{today_prefix}%"),
        )
    ).first()
    return float(rows[0] or 0.0), int(rows[1] or 0)


def check_can_place_bet(stake: float, config: GuardConfig | None = None) -> None:
    """
    Raise GuardViolation if any responsible-betting limit would be crossed
    by placing a bet of `stake`. Otherwise return silently.

    This is a CHEAP check — should be called inside the same transaction as
    the bet placement to avoid TOCTOU. (For now we accept the small TOCTOU
    window since these are advisory user-protection limits, not adversarial.)
    """
    config = config or GuardConfig()
    state = get_state()

    # Stop-loss: if balance has dropped > X% from total deposits, halt
    if config.stop_loss_pct > 0 and state.total_deposits > 0:
        floor = state.total_deposits * (1.0 - config.stop_loss_pct)
        if state.balance < floor:
            raise GuardViolation(
                f"Stop-loss active: balance {state.balance:.2f}$ < {floor:.2f}$ "
                f"(–{config.stop_loss_pct*100:.0f}% of {state.total_deposits:.2f}$ deposits). "
                f"Re-enable by ajusting STOP_LOSS_PCT in .env."
            )

    # Max exposure: total committed (pre-debit notional) must stay under X% of
    # the GROSS bankroll = balance + currently committed (cash + immobilised stakes).
    gross = state.balance + state.committed
    if config.max_exposure_pct > 0 and gross > 0:
        new_exposure = (state.committed + stake) / gross
        if new_exposure > config.max_exposure_pct:
            raise GuardViolation(
                f"Max exposure: {state.committed + stake:.2f}$ pending would be "
                f"{new_exposure*100:.0f}% of gross capital, "
                f"limit {config.max_exposure_pct*100:.0f}%."
            )

    # Cool-off: after N consecutive losses, force a break. This breaks the
    # tilt-spiral that destroys bankrolls — the moment a strategy enters a
    # losing streak, the user often increases stakes to recover. We refuse.
    if config.cool_off_consecutive_losses > 0:
        with session_scope() as s:
            streak, last_loss_ts = _consecutive_losses_streak(s)
        if streak >= config.cool_off_consecutive_losses and last_loss_ts:
            hours = _hours_since(last_loss_ts)
            if hours < config.cool_off_hours:
                hrs_left = config.cool_off_hours - hours
                raise GuardViolation(
                    f"Cool-off active: {streak} pertes consécutives, "
                    f"reprise dans {hrs_left:.1f}h "
                    f"(défini par COOL_OFF_HOURS={config.cool_off_hours})."
                )

    # Daily stake cap + count
    if config.max_daily_stake_pct > 0 or config.max_bets_per_day > 0:
        with session_scope() as s:
            today_total, n_today = _today_stake_total(s)
        if config.max_daily_stake_pct > 0 and state.balance > 0:
            cap = state.balance * config.max_daily_stake_pct
            if today_total + stake > cap:
                raise GuardViolation(
                    f"Daily stake cap: today {today_total:.2f}$ + {stake:.2f}$ > "
                    f"{cap:.2f}$ ({config.max_daily_stake_pct*100:.0f}% of balance)."
                )
        if config.max_bets_per_day > 0 and n_today >= config.max_bets_per_day:
            raise GuardViolation(
                f"Daily bet count cap: {n_today} bets already placed today, "
                f"limit {config.max_bets_per_day}."
            )


def get_guard_status() -> dict:
    """Diagnostic — what's currently allowed / what's blocking."""
    config = GuardConfig()
    state = get_state()
    with session_scope() as s:
        today_total, n_today = _today_stake_total(s)

    floor = state.total_deposits * (1.0 - config.stop_loss_pct) if config.stop_loss_pct > 0 else 0
    remaining_daily_stake = max(
        0.0,
        state.balance * config.max_daily_stake_pct - today_total,
    ) if config.max_daily_stake_pct > 0 else None
    remaining_bets_today = max(
        0, config.max_bets_per_day - n_today
    ) if config.max_bets_per_day > 0 else None

    return {
        "stop_loss_active": state.total_deposits > 0 and state.balance < floor,
        "stop_loss_floor": round(floor, 2),
        "balance": state.balance,
        "committed": state.committed,
        "exposure_pct": round(state.committed / state.balance * 100, 1) if state.balance else 0.0,
        "exposure_max_pct": round(config.max_exposure_pct * 100, 1),
        "today_n_bets": n_today,
        "today_max_bets": config.max_bets_per_day,
        "today_stake_total": round(today_total, 2),
        "today_remaining_stake": (
            round(remaining_daily_stake, 2) if remaining_daily_stake is not None else None
        ),
        "today_remaining_bets": remaining_bets_today,
    }
