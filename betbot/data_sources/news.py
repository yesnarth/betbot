"""
Free-text web search for last-minute football news (injuries, coach sacking,
locker-room scandals) that the statistical models can't capture.

Backend: Tavily — https://tavily.com (free tier 1000 searches/month).
Set TAVILY_API_KEY in .env to activate.

Returns a small list of relevant snippets the AI agent can use to qualify or
veto a bet (e.g. "star striker injured 24h before match → tone down home win
probability").
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import TypedDict

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

logger = logging.getLogger("betbot.data_sources.news")

API_URL = "https://api.tavily.com/search"


class TavilyNotConfigured(RuntimeError):
    pass


class NewsHit(TypedDict):
    title: str
    url: str
    snippet: str
    published: str | None
    score: float


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(min=2, max=10),
    retry=retry_if_exception_type((requests.Timeout, requests.ConnectionError)),
    reraise=True,
)
def _post(payload: dict) -> dict:
    resp = requests.post(API_URL, json=payload, timeout=15)
    if resp.status_code == 401:
        raise TavilyNotConfigured("Bad TAVILY_API_KEY")
    resp.raise_for_status()
    return resp.json()


def search_team_news(
    team_name: str,
    days_back: int = 3,
    max_results: int = 5,
    extra_keywords: str = "injury OR suspended OR sacked OR coach",
) -> list[NewsHit]:
    """
    Search recent news about a team. Returns up to `max_results` hits ordered
    by Tavily relevance score.

    Raises TavilyNotConfigured if TAVILY_API_KEY isn't set — callers should
    catch this and proceed without news (the statistical model still works).
    """
    key = os.getenv("TAVILY_API_KEY", "").strip()
    if not key:
        raise TavilyNotConfigured(
            "TAVILY_API_KEY not set. Sign up at https://tavily.com (1000 searches/month free)."
        )

    payload = {
        "api_key": key,
        "query": f"{team_name} football {extra_keywords}",
        "search_depth": "basic",
        "max_results": max_results,
        "topic": "news",
        "days": days_back,
    }
    try:
        data = _post(payload)
    except TavilyNotConfigured:
        raise
    except Exception as exc:
        logger.warning("Tavily search failed for %s : %s", team_name, exc)
        return []

    hits: list[NewsHit] = []
    for r in data.get("results", []):
        hits.append(NewsHit(
            title=r.get("title", "").strip(),
            url=r.get("url", ""),
            snippet=(r.get("content") or "").strip()[:400],
            published=r.get("published_date"),
            score=float(r.get("score", 0.0)),
        ))
    return hits
