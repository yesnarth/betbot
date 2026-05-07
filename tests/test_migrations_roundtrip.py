"""
Smoke test for Alembic migrations: every revision must apply cleanly,
then downgrade cleanly back to the previous one.

⚠ DESTRUCTIVE: this test runs `alembic downgrade base` which wipes EVERY
table. It refuses to run against any DATABASE_URL that doesn't have a clear
"test" marker, to prevent accidentally destroying production data. Set:

    BETBOT_TEST_DATABASE_URL=postgresql://...test_db

to opt in. We checked this against `BETBOT_TEST_DATABASE_URL` (NOT
`DATABASE_URL`) — same env var format but a clearly-distinct name, so the
prod credentials never trigger this path.

When the env var is unset the tests are skipped — keeps CI fast and prod safe.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _test_db_url() -> str:
    """The dedicated test DB URL — NEVER falls back to DATABASE_URL."""
    return os.getenv("BETBOT_TEST_DATABASE_URL", "").strip()


def _is_safe_test_db() -> bool:
    url = _test_db_url()
    if not url.startswith(("postgresql://", "postgresql+")):
        return False
    # Final safeguard: refuse if the URL doesn't have "test" in it. A test DB
    # should always be named distinctively (e.g. "betbot_test", "test_db").
    # This makes it physically impossible to point this test at prod by
    # accident, no matter how the env vars are wired.
    return "test" in url.lower()


pytestmark = pytest.mark.skipif(
    not _is_safe_test_db(),
    reason="Migration roundtrip needs BETBOT_TEST_DATABASE_URL pointing at a "
           "Postgres DB whose URL contains 'test' (NEVER use the prod DATABASE_URL).",
)


def _alembic(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    """Run an alembic CLI command at the repo root, returning the process result.

    Forces DATABASE_URL to BETBOT_TEST_DATABASE_URL so alembic can never touch
    the production DB even if the surrounding shell has DATABASE_URL set.
    """
    full_env = {**os.environ, **(env or {})}
    full_env["DATABASE_URL"] = _test_db_url()  # override prod URL if any
    return subprocess.run(
        ["alembic", *args],
        cwd=str(PROJECT_ROOT),
        env=full_env,
        capture_output=True,
        text=True,
    )


def _list_revisions() -> list[str]:
    """Return revision IDs sorted from oldest to newest (head)."""
    out = _alembic("history", "--rev-range", "base:head")
    revisions: list[str] = []
    for line in out.stdout.splitlines():
        line = line.strip()
        # Lines look like: "<revision> -> <next>, <message>" or
        # "<rev> (head), <message>". We only need the first token after stripping.
        if " -> " in line:
            tail = line.split(" -> ", 1)[1]
            rev = tail.split(",", 1)[0].strip()
            if rev and rev not in ("head",):
                revisions.append(rev)
    return list(reversed(revisions))


def test_each_revision_is_reachable_and_reversible():
    """Walk forward to head, then back to base, then back to head.

    This exercises every up- and downgrade transition. If any migration is
    broken (e.g. drops a column it can't recreate), the test fails.
    """
    # 1. Confirm we can reach head (no missing migration)
    up = _alembic("upgrade", "head")
    assert up.returncode == 0, f"upgrade head failed:\n{up.stdout}\n{up.stderr}"

    # 2. Walk back to base — every downgrade must succeed
    down = _alembic("downgrade", "base")
    assert down.returncode == 0, f"downgrade base failed:\n{down.stdout}\n{down.stderr}"

    # 3. Re-upgrade — confirms upgrades from a wiped DB still work after downgrades
    up2 = _alembic("upgrade", "head")
    assert up2.returncode == 0, f"re-upgrade head failed:\n{up2.stdout}\n{up2.stderr}"


def test_current_revision_matches_orm_metadata():
    """After upgrade head, alembic check should confirm the schema matches
    the SQLAlchemy ORM metadata (no missing or extra columns)."""
    _alembic("upgrade", "head")
    check = _alembic("check")
    # `alembic check` exits 0 when the DB is up-to-date AND the metadata matches.
    # It exits non-zero with a hint when metadata diverges (e.g. ORM has a
    # column the DB doesn't).
    assert check.returncode == 0, (
        f"alembic check found a divergence:\n{check.stdout}\n{check.stderr}"
    )
