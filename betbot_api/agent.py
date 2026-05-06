"""
AI agent that orchestrates BetBot's MCP tools to produce expert recommendations.

Flow:
  1. Receives user filters from the dashboard / API caller
  2. Spawns a Claude agent connected to the betbot MCP server
  3. Agent reasons step-by-step: lists fixtures → predicts → checks edges → builds combos
  4. Returns the picks + a short rationale
  5. Persists the run in `agent_runs` for audit
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)

from betbot.config import load_settings
from betbot.db import Database

logger = logging.getLogger("betbot_api.agent")


SYSTEM_PROMPT = """You are BetBot, a rigorous quantitative football pronosticator.

Your strategy is multi-signal: you NEVER bet on Poisson probabilities alone.
Before recommending any bet, you cross-reference the model output with at
least two independent signals from the available MCP tools.

Available signals (use them, don't ignore them):

  • predict_match       — blended Dixon-Coles + xG + ELO probability
  • find_value_bets     — positive-edge bets after applying user filters
  • get_elo_rating      — long-term club strength (100 pts ≈ +12% win prob)
  • compare_elo         — Elo-implied no-loss probability for sanity check
  • get_xg_stats        — season xG / xGA / xPts (better than raw goals)
  • get_match_weather   — match-day weather (heavy rain → fewer goals)
  • get_team_injuries   — current injuries / suspensions (may not be configured)
  • build_parlay        — combine independent legs into ranked parlays
  • get_roi_stats       — your historical performance (calibration check)

Workflow when asked to recommend bets:

  1. Fetch events for the requested sport / today.
  2. Call `find_value_bets` with the user's filters as a starting set.
  3. For each candidate worth keeping, run AT LEAST ONE of:
       - get_elo_rating on both teams (sanity-check the model)
       - get_xg_stats on both teams (validate the form is real)
       - get_match_weather (only when it's likely outdoor & matters)
     Reject candidates where the model and the cross-checks disagree
     strongly (e.g. Poisson says 75% but Elo says 50% AND xG is in decline).
  4. Build parlays from survivors only — never combine bets from the same match.
  5. Return JSON ONLY:

     {
       "picks": [<bet objects, untouched fields from find_value_bets>],
       "parlays": [<parlay objects from build_parlay>],
       "rationale": "<3-5 short sentences. Cite the cross-checks.>"
     }

Hard rules — non-negotiable:

  • Never fabricate odds, probabilities, team names, or match results.
  • A pick is only valid if it survived a cross-check. State the cross-check
    used in the rationale.
  • Refuse parlays with two legs from the same match.
  • If no qualifying pick survives, return empty picks/parlays and explain why.
  • Keep rationale terse: facts and numbers, not marketing language.
  • If a tool returns {"ok": false}, simply don't use that signal — never
    pretend the data was available.
"""


def _picks_from_response(text: str) -> dict:
    """Parse the agent's JSON output. Tolerant of markdown fences."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        # Strip ```json ... ``` fence
        lines = cleaned.splitlines()
        cleaned = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # Find the first { ... } block as a last resort
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if 0 <= start < end:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                pass
    return {"picks": [], "parlays": [], "rationale": cleaned[:500]}


async def run_agent(filters: dict[str, Any], trigger: str = "api") -> dict:
    """
    Drive the Claude agent with the user's filters and persist the run.

    Returns:
        {
          "picks": [...],
          "parlays": [...],
          "rationale": "...",
          "n_tool_calls": int,
          "duration_ms": int,
          "model": str,
          "agent_run_id": int,
        }
    """
    s = load_settings()
    if not s.anthropic_api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY not set in .env — the AI agent is disabled. "
            "Other endpoints continue to work."
        )

    started = time.monotonic()
    options = ClaudeAgentOptions(
        system_prompt=SYSTEM_PROMPT,
        model=s.anthropic_model,
        mcp_servers={
            "betbot": {
                "command": "python",
                "args": ["-m", "betbot_mcp.server"],
            }
        },
        allowed_tools=[
            "mcp__betbot__list_sports",
            "mcp__betbot__fetch_events",
            "mcp__betbot__list_teams",
            "mcp__betbot__get_team_stats",
            "mcp__betbot__get_league_averages",
            "mcp__betbot__predict_match",
            "mcp__betbot__find_value_bets",
            "mcp__betbot__build_parlay",
            "mcp__betbot__get_roi_stats",
            # Phase 8 — external signals
            "mcp__betbot__get_elo_rating",
            "mcp__betbot__compare_elo",
            "mcp__betbot__get_xg_stats",
            "mcp__betbot__get_match_weather",
            "mcp__betbot__get_team_injuries",
            "mcp__betbot__search_team_news",
        ],
        permission_mode="acceptEdits",
    )

    # Build the user message from the filters
    user_msg = (
        "Recommend bets matching these filters:\n"
        + json.dumps(filters, indent=2)
        + "\n\nReturn ONLY the JSON object as specified in the system prompt."
    )

    text_chunks: list[str] = []
    n_tool_calls = 0
    cost_usd: float | None = None
    error: str | None = None

    try:
        async with ClaudeSDKClient(options=options) as client:
            await client.query(user_msg)
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for blk in msg.content:
                        if isinstance(blk, TextBlock):
                            text_chunks.append(blk.text)
                        elif isinstance(blk, ToolUseBlock):
                            n_tool_calls += 1
                elif isinstance(msg, ResultMessage):
                    cost_usd = msg.total_cost_usd
    except Exception as exc:
        error = str(exc)
        logger.exception("Agent failed")

    full_text = "\n".join(text_chunks).strip()
    parsed = _picks_from_response(full_text) if full_text else {"picks": [], "parlays": [], "rationale": ""}
    duration_ms = int((time.monotonic() - started) * 1000)

    db = Database(s.database_url)
    run_id = db.save_agent_run(
        trigger=trigger,
        filters=filters,
        model=s.anthropic_model,
        reasoning=full_text,
        picks=parsed.get("picks", []),
        n_tool_calls=n_tool_calls,
        duration_ms=duration_ms,
        cost_usd=cost_usd,
        status="error" if error else "ok",
        error=error,
    )

    return {
        "picks": parsed.get("picks", []),
        "parlays": parsed.get("parlays", []),
        "rationale": parsed.get("rationale", ""),
        "n_tool_calls": n_tool_calls,
        "duration_ms": duration_ms,
        "cost_usd": cost_usd,
        "model": s.anthropic_model,
        "agent_run_id": run_id,
        "error": error,
    }
