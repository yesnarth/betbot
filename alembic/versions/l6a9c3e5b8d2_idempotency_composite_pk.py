"""idempotency_keys composite PK (key, endpoint) — keys scoped per endpoint.

Revision ID: l6a9c3e5b8d2
Revises: k5f8a1c4e7d2
Create Date: 2026-06-01

The table was created with PK(key) alone, but both idempotency.lookup() and
record() scope by (key, endpoint). That mismatch meant a key first seen on one
endpoint silently blocked the SAME key from being recorded on another endpoint
(record() short-circuited on the key-only primary key, so the second row was
never inserted). Make the schema match the code: a client key is unique PER
ENDPOINT, so the same key reused on /deposit and /withdraw stores two
independent rows.

Safe on existing data: every current row has a distinct `key` (the old PK),
so the composite (key, endpoint) is trivially unique too.
"""
from typing import Sequence, Union

from alembic import op


revision: str = "l6a9c3e5b8d2"
down_revision: Union[str, Sequence[str], None] = "k5f8a1c4e7d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("idempotency_keys_pkey", "idempotency_keys", type_="primary")
    op.create_primary_key(
        "idempotency_keys_pkey", "idempotency_keys", ["key", "endpoint"],
    )


def downgrade() -> None:
    op.drop_constraint("idempotency_keys_pkey", "idempotency_keys", type_="primary")
    op.create_primary_key(
        "idempotency_keys_pkey", "idempotency_keys", ["key"],
    )
