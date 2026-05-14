"""
Idempotency helper for mutating endpoints.

Usage from a FastAPI handler:

    from betbot.idempotency import idempotent

    @app.post("/bankroll/deposit")
    def deposit(request: Request, body: BankrollMutation):
        cached = idempotent.lookup(
            key=request.headers.get("Idempotency-Key"),
            endpoint="bankroll/deposit",
            body=body.model_dump(),
        )
        if cached is not None:
            return cached.response  # 200 or whatever was cached

        # ... actually mutate ...
        result = do_deposit(body)
        idempotent.record(key, "bankroll/deposit", body, result, status_code=200)
        return result

If the same key is reused with a different body, lookup() raises
`IdempotencyConflict` (caller should map to HTTP 409).
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from betbot.database import session_scope
from betbot.orm_models import IdempotencyKey

logger = logging.getLogger("betbot.idempotency")


class IdempotencyConflict(RuntimeError):
    """Same key was previously used with a different request body."""


@dataclass(frozen=True)
class CachedResponse:
    response: Any  # already-decoded JSON object
    status_code: int


def _hash_body(body: Any) -> str:
    """Stable SHA-256 of a JSON-serializable body. Sort keys so dict order
    doesn't produce false-positive conflicts."""
    payload = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def lookup(
    key: str | None,
    endpoint: str,
    body: Any,
) -> CachedResponse | None:
    """
    Return a cached response if this key was already processed.

    - key None or empty  → no idempotency (returns None, caller proceeds normally)
    - key seen + body matches → returns CachedResponse (caller short-circuits)
    - key seen + body differs → raises IdempotencyConflict
    """
    if not key:
        return None
    request_hash = _hash_body(body)
    with session_scope() as s:
        row = s.execute(
            select(IdempotencyKey).where(
                IdempotencyKey.key == key,
                IdempotencyKey.endpoint == endpoint,
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        if row.request_hash != request_hash:
            raise IdempotencyConflict(
                f"Idempotency-Key '{key}' was previously used with a different request body."
            )
        try:
            response = json.loads(row.response_json)
        except Exception as exc:
            logger.error("Corrupt cached response for key %s : %s", key, exc)
            return None
        return CachedResponse(response=response, status_code=row.status_code)


def record(
    key: str | None,
    endpoint: str,
    body: Any,
    response: Any,
    status_code: int = 200,
) -> None:
    """
    Persist the (key, endpoint, body, response) tuple. No-op if key is empty.

    Idempotent itself: if the row already exists (race with a concurrent
    request that already inserted), we leave the existing row untouched —
    its response wins.
    """
    if not key:
        return
    request_hash = _hash_body(body)
    try:
        response_json = json.dumps(response, default=str)
    except Exception as exc:
        logger.error("Cannot serialize response for idempotency key %s : %s", key, exc)
        return

    with session_scope() as s:
        existing = s.get(IdempotencyKey, key)
        if existing is not None:
            return  # another request won the race; leave it alone
        s.add(IdempotencyKey(
            key=key,
            endpoint=endpoint,
            request_hash=request_hash,
            response_json=response_json,
            status_code=status_code,
        ))
