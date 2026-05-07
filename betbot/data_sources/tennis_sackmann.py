"""
ATP / WTA match history from Jeff Sackmann's tennis_atp / tennis_wta repositories.

Sackmann's GitHub repos are the de-facto open dataset for tennis analytics —
used by FiveThirtyEight, Tennis Abstract, and most academic papers on tennis ELO.

The CSV format:
    tourney_id, tourney_name, surface, draw_size, tourney_level, tourney_date,
    match_num, winner_id, winner_seed, winner_entry, winner_name,
    winner_hand, winner_ht, winner_ioc, winner_age,
    loser_id, loser_seed, loser_entry, loser_name, ...
    score, best_of, round, minutes, ...

We only need a subset: tourney_date, surface, winner_name, loser_name, round.
"""
from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests

logger = logging.getLogger("betbot.tennis_sackmann")

ATP_BASE = "https://raw.githubusercontent.com/JeffSackmann/tennis_atp/master"
WTA_BASE = "https://raw.githubusercontent.com/JeffSackmann/tennis_wta/master"


@dataclass(frozen=True)
class TennisMatch:
    date: str            # YYYY-MM-DD
    surface: str         # "Hard" | "Clay" | "Grass" | "Carpet" | ""
    winner: str
    loser: str
    tourney_level: str   # "G" (Grand Slam), "M" (Masters), "A" (regular ATP), "F" (Finals)…


def _fetch_csv(url: str, timeout: int = 30) -> str:
    """Download a Sackmann CSV. Returns empty string on failure (caller logs)."""
    try:
        resp = requests.get(url, timeout=timeout)
        if resp.status_code != 200:
            logger.warning("Sackmann fetch %s -> HTTP %d", url, resp.status_code)
            return ""
        return resp.text
    except requests.RequestException as exc:
        logger.warning("Sackmann fetch %s -> %s", url, exc)
        return ""


def _parse_year(text: str) -> list[TennisMatch]:
    """Parse a Sackmann year CSV. Skips rows missing winner/loser/date."""
    matches: list[TennisMatch] = []
    if not text:
        return matches
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        date_raw = (row.get("tourney_date") or "").strip()
        winner = (row.get("winner_name") or "").strip()
        loser = (row.get("loser_name") or "").strip()
        if not (date_raw and winner and loser):
            continue
        # Sackmann date is YYYYMMDD
        if len(date_raw) == 8 and date_raw.isdigit():
            date_iso = f"{date_raw[:4]}-{date_raw[4:6]}-{date_raw[6:8]}"
        else:
            date_iso = date_raw
        matches.append(TennisMatch(
            date=date_iso,
            surface=(row.get("surface") or "").strip(),
            winner=winner,
            loser=loser,
            tourney_level=(row.get("tourney_level") or "").strip(),
        ))
    return matches


def fetch_atp_matches(years: list[int]) -> list[TennisMatch]:
    """Download ATP matches for a list of years. Concatenated and sorted by date."""
    out: list[TennisMatch] = []
    for y in years:
        url = f"{ATP_BASE}/atp_matches_{y}.csv"
        text = _fetch_csv(url)
        out.extend(_parse_year(text))
    out.sort(key=lambda m: m.date)
    logger.info("ATP matches loaded : %d (years %s)", len(out), years)
    return out


def fetch_wta_matches(years: list[int]) -> list[TennisMatch]:
    """Same as fetch_atp_matches but for WTA."""
    out: list[TennisMatch] = []
    for y in years:
        url = f"{WTA_BASE}/wta_matches_{y}.csv"
        text = _fetch_csv(url)
        out.extend(_parse_year(text))
    out.sort(key=lambda m: m.date)
    logger.info("WTA matches loaded : %d (years %s)", len(out), years)
    return out


def default_year_window() -> list[int]:
    """Last two complete years + current year. ELO only needs ~24 months for stable ratings."""
    now = datetime.now(timezone.utc)
    return [now.year - 2, now.year - 1, now.year]
