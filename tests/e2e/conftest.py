"""
Shared safety gate for tests/e2e/.

Every test in this folder is destructive — autouse fixtures wipe the
bankroll ledger, predictions, or run `alembic downgrade base`. To prevent
the suite from ever touching a production database, we require an explicit
`BETBOT_TEST_DATABASE_URL` whose URL contains the literal string "test".

If the variable is missing or doesn't pass the check, every test in this
folder is skipped — so `pytest tests/` is always safe by default. Opt in
with:

    BETBOT_TEST_DATABASE_URL=postgresql://user:pwd@host:5432/betbot_test pytest tests/e2e
"""
from __future__ import annotations

import os
import pytest


def _is_safe_test_db() -> bool:
    url = os.getenv("BETBOT_TEST_DATABASE_URL", "").strip()
    if not url.startswith(("postgresql://", "postgresql+")):
        return False
    return "test" in url.lower()


# Applied to every test in the folder — keeps individual files clean.
collect_ignore_glob: list[str] = []

if not _is_safe_test_db():
    pytest.skip(
        "tests/e2e require BETBOT_TEST_DATABASE_URL pointing at a Postgres DB "
        "whose URL contains 'test' (autouse fixtures are destructive).",
        allow_module_level=True,
    )
