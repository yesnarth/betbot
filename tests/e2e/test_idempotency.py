"""
Tests for the idempotency helper module.

⚠ DESTRUCTIVE: autouse fixture wipes the idempotency_keys table between
tests. Safety gate enforced by tests/e2e/conftest.py.

These tests cover the contract the helper guarantees, NOT the FastAPI
wiring (that's an integration concern). The helper itself is independent
of HTTP — same logic applies to any retry source (scripts, worker jobs).
"""
from __future__ import annotations

import os
import pytest


@pytest.fixture(autouse=True)
def _reset_idempotency(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", os.getenv("BETBOT_TEST_DATABASE_URL", ""))
    from betbot.database import session_scope, reset_engine
    from betbot.orm_models import IdempotencyKey
    reset_engine()
    with session_scope() as s:
        s.query(IdempotencyKey).delete()
    yield
    reset_engine()


def test_lookup_returns_none_for_unseen_key():
    from betbot.idempotency import lookup
    assert lookup("never-seen", "bankroll/deposit", {"amount": 50}) is None


def test_lookup_returns_none_when_key_is_empty():
    """No key = no idempotency. Caller proceeds normally on every call."""
    from betbot.idempotency import lookup
    assert lookup(None, "bankroll/deposit", {"amount": 50}) is None
    assert lookup("", "bankroll/deposit", {"amount": 50}) is None


def test_record_then_lookup_replays_response():
    from betbot.idempotency import record, lookup

    body = {"amount": 50.0, "note": "monthly recharge"}
    response = {"balance": 150.0, "available": 150.0}
    record("client-uuid-abc", "bankroll/deposit", body, response, status_code=200)

    cached = lookup("client-uuid-abc", "bankroll/deposit", body)
    assert cached is not None
    assert cached.status_code == 200
    assert cached.response == response


def test_same_key_different_body_raises_conflict():
    """Reusing a key with a DIFFERENT body is almost always a client bug —
    surface it loudly rather than silently returning the wrong cached value."""
    from betbot.idempotency import record, lookup, IdempotencyConflict

    record("dup-key", "bankroll/deposit", {"amount": 50}, {"balance": 100}, 200)

    with pytest.raises(IdempotencyConflict):
        lookup("dup-key", "bankroll/deposit", {"amount": 999})


def test_dict_key_order_does_not_affect_match():
    """JSON serialization with sort_keys must mean {a,b} == {b,a} for the
    purposes of body comparison."""
    from betbot.idempotency import record, lookup

    record("ord-key", "bankroll/deposit",
           {"amount": 50, "note": "x"}, {"balance": 100}, 200)

    cached = lookup("ord-key", "bankroll/deposit",
                    {"note": "x", "amount": 50})  # reversed order
    assert cached is not None  # no false conflict


def test_same_key_different_endpoints_are_independent():
    """A key is scoped per endpoint — replaying /deposit doesn't replay
    /withdraw and vice versa."""
    from betbot.idempotency import record, lookup

    record("shared-key", "bankroll/deposit", {"amount": 50}, {"balance": 150}, 200)
    record("shared-key", "bankroll/withdraw", {"amount": 30}, {"balance": 120}, 200)

    dep = lookup("shared-key", "bankroll/deposit", {"amount": 50})
    wd = lookup("shared-key", "bankroll/withdraw", {"amount": 30})
    assert dep.response == {"balance": 150}
    assert wd.response == {"balance": 120}


def test_record_is_idempotent_on_race():
    """Second record() with same key is a no-op — the first writer wins.
    Simulates the race where two concurrent requests both miss the lookup
    and try to insert."""
    from betbot.idempotency import record, lookup

    record("race-key", "bankroll/deposit", {"amount": 50}, {"balance": 100}, 200)
    # Second insert with a DIFFERENT response — must not overwrite
    record("race-key", "bankroll/deposit", {"amount": 50}, {"balance": 999}, 200)

    cached = lookup("race-key", "bankroll/deposit", {"amount": 50})
    assert cached.response == {"balance": 100}  # first writer wins


def test_record_with_empty_key_is_noop():
    """No key = no persistence. Helper must not crash."""
    from betbot.idempotency import record
    from betbot.database import session_scope
    from betbot.orm_models import IdempotencyKey

    record(None, "bankroll/deposit", {"amount": 50}, {"balance": 100}, 200)
    record("", "bankroll/deposit", {"amount": 50}, {"balance": 100}, 200)

    with session_scope() as s:
        assert s.query(IdempotencyKey).count() == 0
