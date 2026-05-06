"""
Local deterministic agent — zero AI, zero LLM, zero recurring cost.

Takes the raw picks produced by the blended Poisson model and runs them
through a chain of explicit business rules that consult external signals
(Tavily news, Open-Meteo weather, Club Elo). Each rule:

  - has a clear name (e.g. RULE_INJURY_PENALTY)
  - mutates the probability via a documented multiplier
  - logs a flag explaining what it changed and why

The output is a list of "validated picks" with:
  - the calibrated probability (post-rules)
  - the recomputed edge
  - a rationale list any human can read and audit
  - a status: accepted / rejected / flagged

This is NOT a black box. Every decision is explicit Python code that can be
reviewed, unit-tested and adjusted. The trade-off vs an LLM agent is
flexibility — Claude can reason about novel situations, this agent only
applies the rules we've coded — but determinism, cost, and explainability are
worth it for a free baseline that catches the most common failure modes.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from betbot.calibration import _market_implied_prob
from betbot.data_sources import club_elo
from betbot.data_sources.news import TavilyNotConfigured, search_team_news
from betbot.data_sources.weather import get_match_weather

logger = logging.getLogger("betbot.local_agent")


# ---------------------------------------------------------------------------
# Rule severity multipliers (tunable centralized constants)
# ---------------------------------------------------------------------------

# Probability multipliers applied when a rule fires. Less than 1.0 reduces
# confidence; more than 1.0 increases. Values come from intuition + literature
# (Goddard 2005 on home advantage, EPL injury studies on Tier-1 absence
# impact ≈ 8-12% probability swing).
RULE_HUGE_EDGE_NO_CONFIRMATION = 0.65   # raw edge > 35% with no news support
RULE_INJURY_FAVORITE = 0.85             # injury news on the team WE picked
RULE_INJURY_OPPONENT = 1.06             # injury news on the OPPOSING team
RULE_BAD_WEATHER_OVER = 0.85            # rain/wind on an Over 2.5 pick
RULE_ELO_CONTRADICTION = 0.75           # ELO gap >= 250 contradicting the pick
RULE_PROB_TOO_HIGH = 0.90               # raw model prob > 0.85 (over-confidence)


@dataclass
class PickEvaluation:
    """The agent's verdict on a single raw pick."""
    pick: dict[str, Any]              # original pick from find_value_bets
    final_prob: float                  # probability after all rules applied
    final_edge: float                  # recomputed edge based on final_prob
    rationale: list[str] = field(default_factory=list)
    status: str = "accepted"           # "accepted" | "flagged" | "rejected"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_INJURY_KEYWORDS = re.compile(
    r"\b(injur|out\b|ruled out|unavailable|hamstring|knee|ankle|"
    r"miss(es|ing)?|suspend|banned|red card|absent|sidelined|"
    r"long-term)\b",
    re.IGNORECASE,
)
_COACH_DRAMA = re.compile(
    r"\b(sack|fired|resign|quit|stepped down|new manager|new coach|"
    r"crisis|dressing[- ]room|locker[- ]room)\b",
    re.IGNORECASE,
)


def _selection_team(pick: dict) -> str | None:
    """Return the team the pick is FOR (None if it's a draw)."""
    code = pick.get("selection_code")
    if code == "1":
        return pick.get("home_team")
    if code == "2":
        return pick.get("away_team")
    return None  # draw


def _opposing_team(pick: dict) -> str | None:
    code = pick.get("selection_code")
    if code == "1":
        return pick.get("away_team")
    if code == "2":
        return pick.get("home_team")
    return None


def _news_mentions_injury(news_hits: list[dict]) -> tuple[bool, str | None]:
    """Returns (matched, snippet) — whether any hit mentions a relevant absence."""
    for h in news_hits:
        text = f"{h.get('title', '')} {h.get('snippet', '')}"
        if _INJURY_KEYWORDS.search(text):
            return True, h.get("title", "")[:120]
    return False, None


def _news_mentions_coach_drama(news_hits: list[dict]) -> tuple[bool, str | None]:
    for h in news_hits:
        text = f"{h.get('title', '')} {h.get('snippet', '')}"
        if _COACH_DRAMA.search(text):
            return True, h.get("title", "")[:120]
    return False, None


# ---------------------------------------------------------------------------
# Individual rules — each takes (eval, …context) and may mutate eval
# ---------------------------------------------------------------------------

def _rule_huge_edge_needs_confirmation(
    ev: PickEvaluation,
    raw_edge: float,
    news_picked_team: list[dict],
    news_opposing_team: list[dict],
) -> None:
    """If the raw edge is huge (>35%) and we have NO supporting news on the
    opposing team's bad shape, the pick is almost certainly a model artifact.
    Apply a strong probability penalty.
    """
    if raw_edge <= 0.35:
        return
    has_supporting_news = any(
        _INJURY_KEYWORDS.search(f"{h.get('title','')} {h.get('snippet','')}")
        for h in news_opposing_team
    )
    if not has_supporting_news:
        ev.final_prob *= RULE_HUGE_EDGE_NO_CONFIRMATION
        ev.rationale.append(
            f"⚠ Edge brut {raw_edge*100:+.0f}% sans news favorable — "
            f"probabilité réduite de {(1-RULE_HUGE_EDGE_NO_CONFIRMATION)*100:.0f}%."
        )


def _rule_injury_news(
    ev: PickEvaluation,
    news_picked_team: list[dict],
    news_opposing_team: list[dict],
) -> None:
    """Adjust probability based on injury/suspension news on each side."""
    picked_injured, picked_snippet = _news_mentions_injury(news_picked_team)
    if picked_injured:
        ev.final_prob *= RULE_INJURY_FAVORITE
        ev.rationale.append(
            f"⚠ Blessure/suspension dans l'équipe ciblée — "
            f"prob réduite : {picked_snippet}"
        )

    opp_injured, opp_snippet = _news_mentions_injury(news_opposing_team)
    if opp_injured:
        ev.final_prob *= RULE_INJURY_OPPONENT
        ev.rationale.append(
            f"✓ Blessure/suspension chez l'adversaire — léger boost : {opp_snippet}"
        )


def _rule_coach_drama(
    ev: PickEvaluation,
    news_picked_team: list[dict],
) -> None:
    """Coach sackings or locker-room crises kill predictability — flag, don't reject."""
    drama, snippet = _news_mentions_coach_drama(news_picked_team)
    if drama:
        ev.final_prob *= 0.80
        ev.rationale.append(
            f"⚠ Crise de coach / vestiaire détectée — fortement réduit : {snippet}"
        )


def _rule_bad_weather(
    ev: PickEvaluation,
    weather: dict | None,
) -> None:
    """Heavy rain or strong wind reduces total goals — penalize Over 2.5 picks."""
    if not weather or ev.pick.get("market") != "h2h":
        return
    label = (ev.pick.get("selection_label") or "").lower()
    is_over_pick = "over" in label or "plus de" in label
    if not is_over_pick:
        return
    if weather.get("will_rain_heavy") or weather.get("is_windy"):
        ev.final_prob *= RULE_BAD_WEATHER_OVER
        wx = []
        if weather.get("will_rain_heavy"):
            wx.append(f"pluie {weather.get('precipitation_mm',0):.1f}mm")
        if weather.get("is_windy"):
            wx.append(f"vent {weather.get('wind_kmh',0):.0f}km/h")
        ev.rationale.append(f"⚠ Météo défavorable ({', '.join(wx)}) sur pari Over.")


def _rule_elo_contradiction(
    ev: PickEvaluation,
    elo_home: float | None,
    elo_away: float | None,
) -> None:
    """If we picked home (or away) but the other side has 250+ ELO advantage,
    flag and downweight."""
    if elo_home is None or elo_away is None:
        return
    code = ev.pick.get("selection_code")
    if code == "1" and (elo_away - elo_home) >= 250:
        ev.final_prob *= RULE_ELO_CONTRADICTION
        ev.rationale.append(
            f"⚠ Pari domicile mais ELO opposant +{int(elo_away - elo_home)} pts — "
            f"prob réduite."
        )
    elif code == "2" and (elo_home - elo_away) >= 250:
        ev.final_prob *= RULE_ELO_CONTRADICTION
        ev.rationale.append(
            f"⚠ Pari extérieur mais ELO domicile +{int(elo_home - elo_away)} pts — "
            f"prob réduite."
        )


def _rule_overconfidence(ev: PickEvaluation, raw_prob: float) -> None:
    """Probabilities above 0.85 are usually a model artifact — apply a cap."""
    if raw_prob > 0.85:
        ev.final_prob *= RULE_PROB_TOO_HIGH
        ev.rationale.append(
            f"⚠ Probabilité brute {raw_prob*100:.0f}% — modèle sur-confiant, atténué."
        )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def evaluate_picks(
    picks: list[dict],
    fetch_news: bool = True,
    fetch_weather: bool = True,
    min_final_edge: float = 0.02,
) -> dict:
    """
    Run every pick through the rule chain and return calibrated picks.

    Args:
        picks: output of /recommend/manual (list of pick dicts)
        fetch_news: call Tavily for news on both teams (skipped if no key)
        fetch_weather: call Open-Meteo for the home stadium kick-off forecast
        min_final_edge: drop any pick whose final edge falls below this floor

    Returns:
        {
          "picks": [accepted picks with calibrated probs + rationale],
          "rejected": [picks that failed the rules],
          "n_evaluated": int,
          "n_accepted": int,
          "n_rejected": int,
          "n_news_calls": int,
          "n_weather_calls": int,
          "tavily_available": bool,
        }
    """
    # Pre-fetch the global ELO snapshot once (single HTTP call)
    try:
        elo_snapshot = club_elo.get_all_elo_ratings()
    except Exception as exc:
        logger.warning("Local agent: ELO unavailable (%s)", exc)
        elo_snapshot = {}

    results: list[PickEvaluation] = []
    n_news_calls = 0
    n_weather_calls = 0
    tavily_works = True

    for pick in picks:
        raw_prob = float(pick.get("model_prob", 0))
        raw_edge = float(pick.get("value_edge", 0))
        ev = PickEvaluation(pick=pick, final_prob=raw_prob, final_edge=raw_edge)

        picked_team = _selection_team(pick)
        opposing = _opposing_team(pick)

        # ELO lookup (cheap)
        elo_home = club_elo.get_team_elo(pick.get("home_team", "")) if elo_snapshot else None
        elo_away = club_elo.get_team_elo(pick.get("away_team", "")) if elo_snapshot else None

        # Tavily news (paid quota; only fetch when needed)
        news_picked: list[dict] = []
        news_opposing: list[dict] = []
        if fetch_news and tavily_works and (raw_edge > 0.10):
            try:
                if picked_team:
                    news_picked = search_team_news(picked_team, days_back=3, max_results=4)
                    n_news_calls += 1
                if opposing:
                    news_opposing = search_team_news(opposing, days_back=3, max_results=4)
                    n_news_calls += 1
            except TavilyNotConfigured:
                tavily_works = False
            except Exception as exc:
                logger.debug("Tavily error for %s : %s", picked_team, exc)

        # Weather (free, no quota) — only for matches starting today
        weather = None
        if fetch_weather:
            try:
                # We don't have the kickoff timestamp on the pick — skip if absent.
                # Convention: callers can pass commence_time on the pick dict.
                kickoff = pick.get("commence_time")
                if kickoff:
                    weather = get_match_weather(
                        club_elo._normalize(pick.get("home_team", "")),
                        kickoff,
                    )
                    n_weather_calls += 1
            except Exception as exc:
                logger.debug("Weather error : %s", exc)

        # ---- Apply the rule chain ----
        _rule_overconfidence(ev, raw_prob)
        _rule_injury_news(ev, news_picked, news_opposing)
        _rule_coach_drama(ev, news_picked)
        _rule_bad_weather(ev, weather)
        _rule_elo_contradiction(ev, elo_home, elo_away)
        _rule_huge_edge_needs_confirmation(ev, raw_edge, news_picked, news_opposing)

        # Recompute the edge from the calibrated probability
        ev.final_prob = max(0.0, min(ev.final_prob, 1.0))
        ev.final_edge = round(ev.final_prob * pick["best_odds"] - 1.0, 4)

        # Decide status
        if ev.final_edge < min_final_edge:
            ev.status = "rejected"
            ev.rationale.append(
                f"❌ Edge final {ev.final_edge*100:+.1f}% < seuil {min_final_edge*100:.1f}%."
            )
        elif raw_edge > 0.20 and ev.final_edge > 0.20:
            # Still high after calibration — flag but accept
            ev.status = "flagged"
            ev.rationale.append(
                "🟡 Edge calibré encore élevé — vérifier manuellement."
            )

        if not ev.rationale:
            ev.rationale.append("✓ Aucun signal contradictoire — pari validé tel quel.")

        results.append(ev)

    accepted = [
        {
            **r.pick,
            "model_prob": round(r.final_prob, 4),
            "value_edge": r.final_edge,
            "rationale": r.rationale,
            "status": r.status,
        }
        for r in results if r.status in ("accepted", "flagged")
    ]
    rejected = [
        {
            **r.pick,
            "model_prob": round(r.final_prob, 4),
            "value_edge": r.final_edge,
            "rationale": r.rationale,
            "status": r.status,
        }
        for r in results if r.status == "rejected"
    ]

    return {
        "picks": accepted,
        "rejected": rejected,
        "n_evaluated": len(results),
        "n_accepted": len(accepted),
        "n_rejected": len(rejected),
        "n_news_calls": n_news_calls,
        "n_weather_calls": n_weather_calls,
        "tavily_available": tavily_works,
    }
