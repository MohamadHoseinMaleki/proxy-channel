"""Add publication_schedules cadence table.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-18

One row per Telegram channel recording when the publisher last inserted a
*new* outbox row. Claim/retry of existing pending rows is unchanged (0003).
No secrets.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "publication_schedules",
        sa.Column("channel_id", sa.String(length=255), nullable=False),
        sa.Column("last_scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "char_length(channel_id) > 0",
            name=op.f("ck_publication_schedules_channel_id_not_blank"),
        ),
        sa.PrimaryKeyConstraint("channel_id", name=op.f("pk_publication_schedules")),
    )


def downgrade() -> None:
    op.drop_table("publication_schedules")
