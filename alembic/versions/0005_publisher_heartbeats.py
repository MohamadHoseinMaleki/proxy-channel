"""Add publisher_heartbeats liveness table.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-18

One row per publisher worker name. Existence is not health; last_seen_at
age is. No secrets, no FK, no claim/retry columns.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "publisher_heartbeats",
        sa.Column("worker_name", sa.String(length=64), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("run_id", sa.String(length=32), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "char_length(worker_name) > 0",
            name=op.f("ck_publisher_heartbeats_worker_name_not_blank"),
        ),
        sa.PrimaryKeyConstraint("worker_name", name=op.f("pk_publisher_heartbeats")),
    )


def downgrade() -> None:
    op.drop_table("publisher_heartbeats")
