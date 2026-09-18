"""Persistent publication counters and per-process heartbeats.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-18

Atomic ``publication_counters`` (name, channel_id). Heartbeats key by
``worker_id`` (process identity) plus ``worker_type``. Existing 0005 rows
are rewritten; publication outbox rows are untouched.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "publication_counters",
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("channel_id", sa.String(length=255), server_default=sa.text("''"), nullable=False),
        sa.Column("value", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "char_length(name) > 0",
            name=op.f("ck_publication_counters_name_not_blank"),
        ),
        sa.CheckConstraint("value >= 0", name=op.f("ck_publication_counters_value_non_negative")),
        sa.PrimaryKeyConstraint("name", "channel_id", name=op.f("pk_publication_counters")),
    )

    op.add_column(
        "publisher_heartbeats", sa.Column("worker_id", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "publisher_heartbeats", sa.Column("worker_type", sa.String(length=64), nullable=True)
    )
    op.execute(
        sa.text(
            "UPDATE publisher_heartbeats SET "
            "worker_id = COALESCE(NULLIF(run_id, ''), worker_name), "
            "worker_type = worker_name"
        )
    )
    op.alter_column("publisher_heartbeats", "worker_id", nullable=False)
    op.alter_column("publisher_heartbeats", "worker_type", nullable=False)
    op.execute(
        sa.text(
            "ALTER TABLE publisher_heartbeats "
            "DROP CONSTRAINT pk_publisher_heartbeats, "
            "DROP CONSTRAINT ck_publisher_heartbeats_worker_name_not_blank, "
            "DROP COLUMN worker_name, "
            "DROP COLUMN run_id, "
            "ADD CONSTRAINT pk_publisher_heartbeats PRIMARY KEY (worker_id), "
            "ADD CONSTRAINT ck_publisher_heartbeats_worker_id_not_blank "
            "CHECK (char_length(worker_id) > 0), "
            "ADD CONSTRAINT ck_publisher_heartbeats_worker_type_not_blank "
            "CHECK (char_length(worker_type) > 0)"
        )
    )
    op.create_index(
        "ix_publisher_heartbeats_worker_type",
        "publisher_heartbeats",
        ["worker_type"],
    )


def downgrade() -> None:
    op.drop_index("ix_publisher_heartbeats_worker_type", table_name="publisher_heartbeats")
    op.add_column(
        "publisher_heartbeats", sa.Column("worker_name", sa.String(length=64), nullable=True)
    )
    op.add_column("publisher_heartbeats", sa.Column("run_id", sa.String(length=32), nullable=True))
    op.execute(
        sa.text("UPDATE publisher_heartbeats SET worker_name = worker_id, run_id = worker_id")
    )
    op.alter_column("publisher_heartbeats", "worker_name", nullable=False)
    op.execute(
        sa.text(
            "ALTER TABLE publisher_heartbeats "
            "DROP CONSTRAINT pk_publisher_heartbeats, "
            "DROP CONSTRAINT ck_publisher_heartbeats_worker_id_not_blank, "
            "DROP CONSTRAINT ck_publisher_heartbeats_worker_type_not_blank, "
            "DROP COLUMN worker_id, "
            "DROP COLUMN worker_type, "
            "ADD CONSTRAINT pk_publisher_heartbeats PRIMARY KEY (worker_name), "
            "ADD CONSTRAINT ck_publisher_heartbeats_worker_name_not_blank "
            "CHECK (char_length(worker_name) > 0)"
        )
    )
    op.drop_table("publication_counters")
