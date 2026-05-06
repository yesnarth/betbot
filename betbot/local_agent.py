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
    # Tight patterns only — generic words like "miss", "out", "exile" produced
    # too many false positives (e.g. "Mainoo stars after exile" was flagged as
    # injury). Each pattern below must denote an ACTUAL absence, not just any
    # negative-sounding word.
    r"\b("
    r"injury|injured|injuries|"
    r"ruled out|out for (the )?(season|match|game|months?|weeks?|days?)|"
    r"miss(es|ed|ing) the (match|game|fixture|tie|cup|tournament)|"
    r"will miss|set to miss|expected to miss|"
    r"hamstring|knee surgery|ankle (injury|surgery|sprain)|"
    r"suspend(ed|s|ing)?|banned|red[- ]card|"
    r"sidelined|absent for|"
    r"long[- ]term (injury|absen)|"
    r"could miss|might miss"
    r")\b",
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


def _bare_name(team_name: str) -> str:
    """Strip common football suffixes for tolerant matching."""
    bare = team_name
    for suffix in (" FC", " CF", " AC", " SC", " AFC"):
        if bare.endswith(suffix):
            bare = bare[: -len(suffix)]
            break
    return bare.lower().strip()


def _team_mentioned(team_name: str, text: str) -> bool:
    """Return True only when the team name appears as a contiguous chunk."""
    if not team_name:
        return False
    return _bare_name(team_name) in text.lower()


def _team_is_subject_of_hit(team_name: str, hit: dict) -> bool:
    """
    Stricter than _team_mentioned: requires the team to be in the TITLE.

    Real-world bug we're fixing: Tavily searched on "Mallorca" returns an
    article titled "Real Madrid Defender Mendy Could Miss Up to Five Months".
    The article snippet may say "before Real Madrid faces Mallorca next week",
    so "Mallorca" appears in the snippet — but the article SUBJECT is Madrid,
    not Mallorca. To avoid attributing the injury to Mallorca, we require the
    team name to appear in the TITLE specifically.
    """
    if not team_name:
        return False
    bare = _bare_name(team_name)
    title = (hit.get("title") or "").lower()
    return bare in title


def _filter_relevant_hits(team_name: str, news_hits: list[dict]) -> list[dict]:
    """Keep only hits whose TITLE mentions the team."""
    return [h for h in news_hits if _team_is_subject_of_hit(team_name, h)]


def _news_mentions_injury(news_hits: list[dict], team_name: str) -> tuple[bool, str | None]:
    """Returns (matched, title). The hit's TITLE must mention the team AND
    a real injury/suspension keyword must appear somewhere in title or snippet."""
    if not team_name:
        return False, None
    for h in news_hits:
        if not _team_is_subject_of_hit(team_name, h):
            continue
        text = f"{h.get('title', '')} {h.get('snippet', '')}"
        if _INJURY_KEYWORDS.search(text):
            return True, h.get("title", "")[:140]
    return False, None


def _news_mentions_coach_drama(news_hits: list[dict], team_name: str) -> tuple[bool, str | None]:
    """Same strict rule as injuries: the team must be the subject of the hit."""
    if not team_name:
        return False, None
    for h in news_hits:
        if not _team_is_subject_of_hit(team_name, h):
            continue
        text = f"{h.get('title', '')} {h.get('snippet', '')}"
        if _COACH_DRAMA.search(text):
            return True, h.get("title", "")[:140]
    return False, None


# ---------------------------------------------------------------------------
# Individual rules — each takes (eval, …context) and may mutate eval
# ---------------------------------------------------------------------------

def _rule_huge_edge_needs_confirmation(
    ev: PickEvaluation,
    raw_edge: float,
    picked_team: str | None,
    opposing_team: str | None,
    news_picked_team: list[dict],
    news_opposing_team: list[dict],
) -> None:
    """If the raw edge is huge (>35%) and we have NO supporting news on the
    opposing team's bad shape, the pick is almost certainly a model artifact.
    Apply a strong probability penalty.
    """
    if raw_edge <= 0.35:
        return
    # Only count supporting news that ACTUALLY mentions the opposing team
    has_supporting, _ = _news_mentions_injury(news_opposing_team, opposing_team or "")
    if not has_supporting:
        ev.final_prob *= RULE_HUGE_EDGE_NO_CONFIRMATION
        ev.rationale.append(
            f"⚠ Edge brut {raw_edge*100:+.0f}% sans news favorable confirmée — "
            f"probabilité réduite de {(1-RULE_HUGE_EDGE_NO_CONFIRMATION)*100:.0f}%."
        )


def _rule_injury_news(
    ev: PickEvaluation,
    picked_team: str | None,
    opposing_team: str | None,
    news_picked_team: list[dict],
    news_opposing_team: list[dict],
) -> None:
    """Adjust probability based on injury/suspension news on each side."""
    picked_injured, picked_snippet = _news_mentions_injury(news_picked_team, picked_team or "")
    if picked_injured:
        ev.final_prob *= RULE_INJURY_FAVORITE
        ev.rationale.append(
            f"⚠ Blessure/suspension chez {picked_team} — prob réduite : {picked_snippet}"
        )

    opp_injured, opp_snippet = _news_mentions_injury(news_opposing_team, opposing_team or "")
    if opp_injured:
        ev.final_prob *= RULE_INJURY_OPPONENT
        ev.rationale.append(
            f"✓ Blessure/suspension chez l'adversaire {opposing_team} — léger boost : {opp_snippet}"
        )


def _rule_coach_drama(
    ev: PickEvaluation,
    picked_team: str | None,
    news_picked_team: list[dict],
) -> None:
    """Coach sackings or locker-room crises kill predictability — flag, don't reject."""
    drama, snippet = _news_mentions_coach_drama(news_picked_team, picked_team or "")
    if drama:
        ev.final_prob *= 0.80
        ev.rationale.append(
            f"⚠ Crise de coach / vestiaire chez {picked_team} : {snippet}"
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
    bankroll: float = 100.0,
    kelly_fraction: float = 0.25,
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

        # Tavily news (paid quota; only fetch when needed). We FILTER hits
        # to keep only those that actually mention the team — Tavily often
        # returns generic football news when the team has no English coverage.
        news_picked: list[dict] = []
        news_opposing: list[dict] = []
        if fetch_news and tavily_works and (raw_edge > 0.10):
            try:
                if picked_team:
                    raw_hits = search_team_news(picked_team, days_back=3, max_results=5)
                    news_picked = _filter_relevant_hits(picked_team, raw_hits)
                    n_news_calls += 1
                if opposing:
                    raw_hits = search_team_news(opposing, days_back=3, max_results=5)
                    news_opposing = _filter_relevant_hits(opposing, raw_hits)
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
        _rule_injury_news(ev, picked_team, opposing, news_picked, news_opposing)
        _rule_coach_drama(ev, picked_team, news_picked)
        _rule_bad_weather(ev, weather)
        _rule_elo_contradiction(ev, elo_home, elo_away)
        _rule_huge_edge_needs_confirmation(
            ev, raw_edge, picked_team, opposing, news_picked, news_opposing,
        )

        # Recompute the edge AND the Kelly stake from the calibrated probability.
        # Kelly is sensitive to small probability changes, so failing to recompute
        # would leave a stake size that doesn't match the calibrated prob.
        ev.final_prob = max(0.0, min(ev.final_prob, 1.0))
        ev.final_edge = round(ev.final_prob * pick["best_odds"] - 1.0, 4)
        from betbot.analysis import kelly_stake
        ev.pick = {
            **pick,
            "kelly_stake": kelly_stake(
                ev.final_prob, pick["best_odds"], bankroll, kelly_fraction
            ),
        }

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
