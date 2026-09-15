"""Initial proxy intelligence schema.

Revision ID: 0001
Revises:
Create Date: 2026-09-15

Four tables, four distinct responsibilities -- see docs/DATABASE.md:

    proxies            identity      one row per distinct configuration
    proxy_discoveries  provenance    where/when an identity was seen
    proxy_observations measurement   one row per test attempt, pass or fail
    proxy_scores       derived state versioned snapshots

Design notes that are easy to undo by accident and therefore written down here:

* Every timestamp is ``TIMESTAMPTZ``. Four independent worker processes on
  different hosts must produce comparable times, and ``server_default=now()``
  makes the database clock authoritative rather than the application clock.

* ``uq_proxies_fingerprint`` is the load-bearing constraint. It is what makes
  10,000 sightings of one configuration collapse into one row. The fingerprint
  covers protocol+server+port+secret -- NOT server+port alone, because several
  distinct MTProto secrets routinely share one endpoint.

* ``proxy_observations.proxy_id`` is ``ON DELETE RESTRICT`` while
  ``proxy_discoveries`` and ``proxy_scores`` are ``ON DELETE CASCADE``.
  Measurement history is the asset this platform exists to build; it must not be
  destroyable as a side effect. Scores and provenance are derivable or
  meaningless without the identity, so they cascade.

* ``secret`` is stored as plain TEXT, deliberately. The tester needs the real
  value to connect, and application-level encryption without a key-management
  strategy would be theatre. Protection is: the ``SecretText`` type decorator
  wraps reads in ``ProxySecret`` so ``str()``/``repr()``/f-strings yield a masked
  form; the log scrubber; and never selecting the column into a report.
  This migration writes ``sa.Text()`` rather than ``core.models.SecretText()``
  because ``SecretText.impl`` IS ``Text`` -- the DDL is identical, and a frozen
  migration should not depend on application imports.

Indexes, each tied to a concrete query:

* ``ix_proxies_due`` ``(next_test_at) WHERE is_active``
  Serves the tester claim query
  ``WHERE is_active AND next_test_at <= now() ORDER BY next_test_at LIMIT n
  FOR UPDATE SKIP LOCKED``. Partial, because retired proxies should cost nothing
  to index. ``next_test_at`` is NOT NULL precisely so this index needs no
  ``NULLS FIRST`` variant -- a nullable column would force one, since a plain ASC
  btree stores NULLs last and cannot serve that ordering in either scan
  direction. The lease predicate cannot be part of the index at all: partial
  index predicates must be IMMUTABLE and ``now()`` is only STABLE.

* ``ix_proxies_last_success_at`` -- reporting ("what worked in the last 6h").
  Carries real write churn because it updates on every success; accepted because
  top-N reporting is a stated MVP requirement.

* ``ix_proxies_server`` -- debugging, abuse investigation, future per-host
  throttling. ``server`` never changes after insert, so this costs inserts only.

* ``ix_proxy_observations_proxy_id_observed_at`` -- the scoring query, which
  aggregates per proxy over 1h/6h/24h windows. The hottest index in the system.

* ``ix_proxy_observations_observed_at`` -- retention sweeps
  (``DELETE WHERE observed_at < x``). The composite above cannot serve this
  because ``proxy_id`` leads it.

* ``ix_proxy_observations_success_observed_at`` ``(observed_at) WHERE success``
  Latency aggregation only ever reads successes. This deviates from the
  suggested ``(success, observed_at)`` composite: a boolean leading column
  roughly doubles the index size while serving exactly the same query, since the
  filter is a constant. See docs/DECISION_LOG.md D-025.

* ``ix_proxy_scores_proxy_id_calculated_at`` -- "latest score per proxy", the
  reporting hot path (``DISTINCT ON (proxy_id) ... ORDER BY proxy_id,
  calculated_at DESC``). A plain ASC btree also serves the DESC ordering via a
  backward scan, so no second index is needed.

* The three ``ix_proxy_discoveries_*`` indexes serve provenance lookups by
  proxy, by time range, and by source type.

Constraints do real work here rather than duplicating Pydantic:

* ``ck_proxies_port_range`` -- a port outside 1..65535 is not connectable.
* ``ck_proxy_observations_failure_needs_category`` -- a failure with no category
  is unusable for scoring, so the database refuses to store one.
* ``ck_proxy_observations_error_message_bounded`` -- caps persisted error text at
  500 characters. Full tracebacks are large, repetitive, and the most likely
  place for a secret to hide.
* ``ck_proxy_scores_reliability_*_needs_samples`` -- reliability must be NULL,
  not zero, when there are no samples. "No data" and "0% success" are different
  facts, and conflating them is how a never-tested proxy acquires a score.
* ``ck_proxy_scores_p95_at_least_p50`` -- percentiles that violate their own
  ordering mean the scorer is broken.
* latency columns are ``>= 0`` everywhere.

``protocol`` deliberately has NO CHECK constraint. The MVP only supports MTProto,
but the brief asks for extensibility without building a multi-protocol platform;
a closed allowlist would make every future protocol a migration. Correctness is
still protected because ``protocol`` is an input to the fingerprint, so a wrong
value yields a different identity rather than a collision.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "proxies",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("protocol", sa.String(length=16), server_default="mtproto", nullable=False),
        sa.Column("server", sa.String(length=255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("secret", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "next_test_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("test_lock_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("test_attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_test_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_test_finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_category", sa.String(length=32), nullable=True),
        sa.CheckConstraint(
            "char_length(secret) > 0 AND char_length(secret) <= 512",
            name=op.f("ck_proxies_secret_length"),
        ),
        sa.CheckConstraint("char_length(server) > 0", name=op.f("ck_proxies_server_not_blank")),
        sa.CheckConstraint("port >= 1 AND port <= 65535", name=op.f("ck_proxies_port_range")),
        sa.CheckConstraint(
            "test_attempts >= 0", name=op.f("ck_proxies_test_attempts_non_negative")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_proxies")),
        sa.UniqueConstraint("fingerprint", name=op.f("uq_proxies_fingerprint")),
    )
    op.create_index(
        "ix_proxies_due",
        "proxies",
        ["next_test_at"],
        unique=False,
        postgresql_where=sa.text("is_active"),
    )
    op.create_index("ix_proxies_last_success_at", "proxies", ["last_success_at"], unique=False)
    op.create_index("ix_proxies_server", "proxies", ["server"], unique=False)
    op.create_table(
        "proxy_discoveries",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("proxy_id", sa.BigInteger(), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_name", sa.String(length=255), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column(
            "discovered_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("raw_reference", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "char_length(source_name) > 0", name=op.f("ck_proxy_discoveries_source_name_not_blank")
        ),
        sa.ForeignKeyConstraint(
            ["proxy_id"],
            ["proxies.id"],
            name=op.f("fk_proxy_discoveries_proxy_id_proxies"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_proxy_discoveries")),
    )
    op.create_index(
        "ix_proxy_discoveries_discovered_at", "proxy_discoveries", ["discovered_at"], unique=False
    )
    op.create_index(
        "ix_proxy_discoveries_proxy_id", "proxy_discoveries", ["proxy_id"], unique=False
    )
    op.create_index(
        "ix_proxy_discoveries_source_type", "proxy_discoveries", ["source_type"], unique=False
    )
    op.create_table(
        "proxy_observations",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("proxy_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "observed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("tcp_connect_ms", sa.Float(), nullable=True),
        sa.Column("mtproto_connect_ms", sa.Float(), nullable=True),
        sa.Column("total_latency_ms", sa.Float(), nullable=True),
        sa.Column("error_category", sa.String(length=32), nullable=True),
        sa.Column("error_message_safe", sa.Text(), nullable=True),
        sa.Column("tester_version", sa.String(length=32), server_default="v0", nullable=False),
        sa.Column("test_location", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "error_message_safe IS NULL OR char_length(error_message_safe) <= 500",
            name=op.f("ck_proxy_observations_error_message_bounded"),
        ),
        sa.CheckConstraint(
            "mtproto_connect_ms IS NULL OR mtproto_connect_ms >= 0",
            name=op.f("ck_proxy_observations_mtproto_ms_non_negative"),
        ),
        sa.CheckConstraint(
            "success OR error_category IS NOT NULL",
            name=op.f("ck_proxy_observations_failure_needs_category"),
        ),
        sa.CheckConstraint(
            "tcp_connect_ms IS NULL OR tcp_connect_ms >= 0",
            name=op.f("ck_proxy_observations_tcp_ms_non_negative"),
        ),
        sa.CheckConstraint(
            "total_latency_ms IS NULL OR total_latency_ms >= 0",
            name=op.f("ck_proxy_observations_total_ms_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["proxy_id"],
            ["proxies.id"],
            name=op.f("fk_proxy_observations_proxy_id_proxies"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_proxy_observations")),
    )
    op.create_index(
        "ix_proxy_observations_observed_at", "proxy_observations", ["observed_at"], unique=False
    )
    op.create_index(
        "ix_proxy_observations_proxy_id_observed_at",
        "proxy_observations",
        ["proxy_id", "observed_at"],
        unique=False,
    )
    op.create_index(
        "ix_proxy_observations_success_observed_at",
        "proxy_observations",
        ["observed_at"],
        unique=False,
        postgresql_where=sa.text("success"),
    )
    op.create_table(
        "proxy_scores",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("proxy_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "calculated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("score", sa.Numeric(precision=6, scale=3), nullable=False),
        sa.Column("reliability_1h", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("reliability_6h", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("reliability_24h", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("latency_p50_ms", sa.Numeric(precision=10, scale=3), nullable=True),
        sa.Column("latency_p95_ms", sa.Numeric(precision=10, scale=3), nullable=True),
        sa.Column("sample_count_1h", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("sample_count_6h", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("sample_count_24h", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("scoring_version", sa.String(length=16), server_default="v1", nullable=False),
        sa.CheckConstraint(
            "char_length(scoring_version) > 0",
            name=op.f("ck_proxy_scores_scoring_version_not_blank"),
        ),
        sa.CheckConstraint(
            "latency_p50_ms IS NULL OR latency_p50_ms >= 0",
            name=op.f("ck_proxy_scores_p50_non_negative"),
        ),
        sa.CheckConstraint(
            "latency_p95_ms IS NULL OR latency_p50_ms IS NULL OR latency_p95_ms >= latency_p50_ms",
            name=op.f("ck_proxy_scores_p95_at_least_p50"),
        ),
        sa.CheckConstraint(
            "latency_p95_ms IS NULL OR latency_p95_ms >= 0",
            name=op.f("ck_proxy_scores_p95_non_negative"),
        ),
        sa.CheckConstraint(
            "reliability_1h IS NULL OR (reliability_1h >= 0 AND reliability_1h <= 100)",
            name=op.f("ck_proxy_scores_reliability_1h_range"),
        ),
        sa.CheckConstraint(
            "reliability_24h IS NULL OR (reliability_24h >= 0 AND reliability_24h <= 100)",
            name=op.f("ck_proxy_scores_reliability_24h_range"),
        ),
        sa.CheckConstraint(
            "reliability_6h IS NULL OR (reliability_6h >= 0 AND reliability_6h <= 100)",
            name=op.f("ck_proxy_scores_reliability_6h_range"),
        ),
        sa.CheckConstraint(
            "sample_count_1h > 0 OR reliability_1h IS NULL",
            name=op.f("ck_proxy_scores_reliability_1h_needs_samples"),
        ),
        sa.CheckConstraint(
            "sample_count_1h >= 0", name=op.f("ck_proxy_scores_samples_1h_non_negative")
        ),
        sa.CheckConstraint(
            "sample_count_24h > 0 OR reliability_24h IS NULL",
            name=op.f("ck_proxy_scores_reliability_24h_needs_samples"),
        ),
        sa.CheckConstraint(
            "sample_count_24h >= 0", name=op.f("ck_proxy_scores_samples_24h_non_negative")
        ),
        sa.CheckConstraint(
            "sample_count_6h > 0 OR reliability_6h IS NULL",
            name=op.f("ck_proxy_scores_reliability_6h_needs_samples"),
        ),
        sa.CheckConstraint(
            "sample_count_6h >= 0", name=op.f("ck_proxy_scores_samples_6h_non_negative")
        ),
        sa.CheckConstraint("score >= 0 AND score <= 100", name=op.f("ck_proxy_scores_score_range")),
        sa.ForeignKeyConstraint(
            ["proxy_id"],
            ["proxies.id"],
            name=op.f("fk_proxy_scores_proxy_id_proxies"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_proxy_scores")),
    )
    op.create_index(
        "ix_proxy_scores_proxy_id_calculated_at",
        "proxy_scores",
        ["proxy_id", "calculated_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_proxy_scores_proxy_id_calculated_at", table_name="proxy_scores")
    op.drop_table("proxy_scores")
    op.drop_index(
        "ix_proxy_observations_success_observed_at",
        table_name="proxy_observations",
        postgresql_where=sa.text("success"),
    )
    op.drop_index("ix_proxy_observations_proxy_id_observed_at", table_name="proxy_observations")
    op.drop_index("ix_proxy_observations_observed_at", table_name="proxy_observations")
    op.drop_table("proxy_observations")
    op.drop_index("ix_proxy_discoveries_source_type", table_name="proxy_discoveries")
    op.drop_index("ix_proxy_discoveries_proxy_id", table_name="proxy_discoveries")
    op.drop_index("ix_proxy_discoveries_discovered_at", table_name="proxy_discoveries")
    op.drop_table("proxy_discoveries")
    op.drop_index("ix_proxies_server", table_name="proxies")
    op.drop_index("ix_proxies_last_success_at", table_name="proxies")
    op.drop_index("ix_proxies_due", table_name="proxies", postgresql_where=sa.text("is_active"))
    op.drop_table("proxies")
