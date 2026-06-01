"""Shared display formatters — one source of truth for how numbers render.

Convention: model probabilities / edges are fractions in [0, 1] (0.65 = 65 %).
Pass already_pct=True when the value is already on a 0-100 scale.
"""
from __future__ import annotations


def fmt_pct(value: float | None, *, already_pct: bool = False, decimals: int = 1) -> str:
    """0.65 → '65.0 %'. Returns '—' for None."""
    if value is None:
        return "—"
    v = value if already_pct else value * 100.0
    return f"{v:.{decimals}f} %"


def fmt_signed_pct(value: float | None, *, already_pct: bool = False, decimals: int = 1) -> str:
    """Like fmt_pct but with an explicit sign (edges / EV): 0.04 → '+4.0 %'."""
    if value is None:
        return "—"
    v = value if already_pct else value * 100.0
    return f"{v:+.{decimals}f} %"


def fmt_odds(value: float | None) -> str:
    """Decimal odds, 2 dp: 2.1 → '2.10'."""
    if value is None:
        return "—"
    return f"{value:.2f}"


def fmt_money(value: float | None, *, symbol: str = "$", decimals: int = 2) -> str:
    """12.5 → '12.50 $'."""
    if value is None:
        return "—"
    return f"{value:.{decimals}f} {symbol}"
