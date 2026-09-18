"""Add proxy_publications audit table.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-18

Append-only publish attempts. A successful post is unique per
``(proxy_id, channel_id)`` so a publisher tick cannot spam. Failures may
repeat. The channel message body (MTProto secret) is not stored.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "proxy_publications",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("proxy_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_id", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=True),
        sa.Column("error_message_safe", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "char_length(channel_id) > 0",
            name=op.f("ck_proxy_publications_channel_id_not_blank"),
        ),
        sa.CheckConstraint(
            "status IN ('success', 'failure')",
            name=op.f("ck_proxy_publications_status_known"),
        ),
        sa.CheckConstraint(
            "status <> 'success' OR telegram_message_id IS NOT NULL",
            name=op.f("ck_proxy_publications_success_needs_message_id"),
        ),
        sa.CheckConstraint(
            "status <> 'failure' OR error_message_safe IS NOT NULL",
            name=op.f("ck_proxy_publications_failure_needs_error"),
        ),
        sa.CheckConstraint(
            "error_message_safe IS NULL OR char_length(error_message_safe) <= 500",
            name=op.f("ck_proxy_publications_error_message_bounded"),
        ),
        sa.ForeignKeyConstraint(
            ["proxy_id"],
            ["proxies.id"],
            name=op.f("fk_proxy_publications_proxy_id_proxies"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_proxy_publications")),
    )
    op.create_index(
        "ix_proxy_publications_proxy_id", "proxy_publications", ["proxy_id"], unique=False
    )
    op.create_index(
        "ix_proxy_publications_created_at",
        "proxy_publications",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        "uq_proxy_publications_success",
        "proxy_publications",
        ["proxy_id", "channel_id"],
        unique=True,
        postgresql_where=sa.text("status = 'success'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_proxy_publications_success",
        table_name="proxy_publications",
        postgresql_where=sa.text("status = 'success'"),
    )
    op.drop_index("ix_proxy_publications_created_at", table_name="proxy_publications")
    op.drop_index("ix_proxy_publications_proxy_id", table_name="proxy_publications")
    op.drop_table("proxy_publications")
