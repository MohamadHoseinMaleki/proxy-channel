"""Turn proxy_publications into a recoverable outbox.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-18

Task 013 stored append-only success/failure attempts. A crash between a
successful Bot API send and the audit commit could then double-post.

This revision:

* one row per ``(proxy_id, channel_id)`` (identity, not an attempt log)
* lifecycle ``pending`` / ``sending`` / ``published`` / ``failed``
* lease + ``next_attempt_at`` so a killed worker self-heals
* ``attempt_count`` for bounded retries

Existing ``success`` rows become ``published``. Existing ``failure`` rows
become ``pending`` so they may retry. Duplicate pairs keep a published
row if any, otherwise the newest id.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "proxy_publications",
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column(
        "proxy_publications",
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "proxy_publications",
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.add_column(
        "proxy_publications",
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
    )

    # Drop 0002 CHECKs *before* rewriting status. ``success`` → ``published``
    # would otherwise violate ``status IN ('success', 'failure')``.
    op.drop_constraint(
        op.f("ck_proxy_publications_status_known"), "proxy_publications", type_="check"
    )
    op.drop_constraint(
        op.f("ck_proxy_publications_success_needs_message_id"),
        "proxy_publications",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_proxy_publications_failure_needs_error"),
        "proxy_publications",
        type_="check",
    )

    op.execute(
        sa.text(
            """
            UPDATE proxy_publications
            SET attempt_count = 1,
                last_attempt_at = created_at,
                next_attempt_at = created_at
            WHERE attempt_count = 0
            """
        )
    )
    op.execute(
        sa.text("UPDATE proxy_publications SET status = 'published' WHERE status = 'success'")
    )
    op.execute(sa.text("UPDATE proxy_publications SET status = 'pending' WHERE status = 'failure'"))
    op.execute(
        sa.text(
            """
            DELETE FROM proxy_publications AS older
            USING proxy_publications AS newer
            WHERE older.proxy_id = newer.proxy_id
              AND older.channel_id = newer.channel_id
              AND older.id < newer.id
              AND NOT (
                    older.status = 'published'
                    AND newer.status <> 'published'
              )
            """
        )
    )
    op.execute(
        sa.text(
            """
            DELETE FROM proxy_publications AS extra
            USING proxy_publications AS kept
            WHERE extra.proxy_id = kept.proxy_id
              AND extra.channel_id = kept.channel_id
              AND extra.id <> kept.id
              AND kept.status = 'published'
              AND extra.status <> 'published'
            """
        )
    )
    op.create_check_constraint(
        op.f("ck_proxy_publications_status_known"),
        "proxy_publications",
        "status IN ('pending', 'sending', 'published', 'failed')",
    )
    op.create_check_constraint(
        op.f("ck_proxy_publications_published_needs_message_id"),
        "proxy_publications",
        "status <> 'published' OR telegram_message_id IS NOT NULL",
    )
    op.create_check_constraint(
        op.f("ck_proxy_publications_message_id_only_when_published"),
        "proxy_publications",
        "status = 'published' OR telegram_message_id IS NULL",
    )
    op.create_check_constraint(
        op.f("ck_proxy_publications_failed_needs_error"),
        "proxy_publications",
        "status <> 'failed' OR error_message_safe IS NOT NULL",
    )
    op.create_check_constraint(
        op.f("ck_proxy_publications_sending_needs_lease"),
        "proxy_publications",
        "status <> 'sending' OR lease_until IS NOT NULL",
    )
    op.create_check_constraint(
        op.f("ck_proxy_publications_attempt_count_non_negative"),
        "proxy_publications",
        "attempt_count >= 0",
    )

    op.drop_index(
        "uq_proxy_publications_success",
        table_name="proxy_publications",
        postgresql_where=sa.text("status = 'success'"),
    )
    op.create_unique_constraint(
        op.f("uq_proxy_publications_proxy_channel"),
        "proxy_publications",
        ["proxy_id", "channel_id"],
    )
    op.create_index(
        "ix_proxy_publications_due",
        "proxy_publications",
        ["next_attempt_at", "id"],
        unique=False,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_proxy_publications_sending_lease",
        "proxy_publications",
        ["lease_until"],
        unique=False,
        postgresql_where=sa.text("status = 'sending'"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_proxy_publications_sending_lease",
        table_name="proxy_publications",
        postgresql_where=sa.text("status = 'sending'"),
    )
    op.drop_index(
        "ix_proxy_publications_due",
        table_name="proxy_publications",
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.drop_constraint(
        op.f("uq_proxy_publications_proxy_channel"),
        "proxy_publications",
        type_="unique",
    )
    op.create_index(
        "uq_proxy_publications_success",
        "proxy_publications",
        ["proxy_id", "channel_id"],
        unique=True,
        postgresql_where=sa.text("status = 'success'"),
    )

    op.drop_constraint(
        op.f("ck_proxy_publications_attempt_count_non_negative"),
        "proxy_publications",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_proxy_publications_sending_needs_lease"),
        "proxy_publications",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_proxy_publications_failed_needs_error"),
        "proxy_publications",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_proxy_publications_message_id_only_when_published"),
        "proxy_publications",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_proxy_publications_published_needs_message_id"),
        "proxy_publications",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_proxy_publications_status_known"), "proxy_publications", type_="check"
    )

    op.execute(
        sa.text("UPDATE proxy_publications SET status = 'success' WHERE status = 'published'")
    )
    op.execute(
        sa.text(
            "UPDATE proxy_publications SET status = 'failure' "
            "WHERE status IN ('pending', 'sending', 'failed')"
        )
    )
    op.execute(
        sa.text(
            "UPDATE proxy_publications SET telegram_message_id = NULL "
            "WHERE status = 'failure' AND telegram_message_id IS NOT NULL"
        )
    )
    op.execute(
        sa.text(
            "UPDATE proxy_publications SET error_message_safe = 'downgraded' "
            "WHERE status = 'failure' AND error_message_safe IS NULL"
        )
    )

    op.create_check_constraint(
        op.f("ck_proxy_publications_status_known"),
        "proxy_publications",
        "status IN ('success', 'failure')",
    )
    op.create_check_constraint(
        op.f("ck_proxy_publications_success_needs_message_id"),
        "proxy_publications",
        "status <> 'success' OR telegram_message_id IS NOT NULL",
    )
    op.create_check_constraint(
        op.f("ck_proxy_publications_failure_needs_error"),
        "proxy_publications",
        "status <> 'failure' OR error_message_safe IS NOT NULL",
    )

    op.drop_column("proxy_publications", "lease_until")
    op.drop_column("proxy_publications", "next_attempt_at")
    op.drop_column("proxy_publications", "last_attempt_at")
    op.drop_column("proxy_publications", "attempt_count")
