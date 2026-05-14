"""Agent runs — audit trail of AI invocations (list + drill-down)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from betbot.db import Database
from betbot_api.auth import require_auth
from betbot_api.deps import get_db

router = APIRouter(prefix="/agent", tags=["agent"])


@router.get("/runs")
def list_agent_runs(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    trigger: str | None = Query(default=None,
                                description="Filter: 'api', 'dashboard', or 'scheduled'"),
    db: Database = Depends(get_db),
    _: str = Depends(require_auth),
) -> list[dict]:
    """Audit trail: every AI-agent invocation with reasoning + cost + picks."""
    return db.list_agent_runs(limit=limit, offset=offset, trigger=trigger)


@router.get("/runs/{run_id}")
def get_agent_run(
    run_id: int,
    db: Database = Depends(get_db),
    _: str = Depends(require_auth),
) -> dict:
    """Full detail of a single agent run, including the reasoning trace."""
    row = db.get_agent_run(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Agent run #{run_id} not found")
    return row
