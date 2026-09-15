"""SQLAlchemy 2.x async ORM models for the proxy intelligence schema.

Four tables, four distinct jobs. Keeping them separate is the core of the design:

======================  =========================================================
Table                   Role
======================  =========================================================
``proxies``             **identity** -- one row per distinct configuration, keyed
                        by ``fingerprint``
``proxy_discoveries``   **provenance** -- where/when an identity was seen
``proxy_observations``  **measured behaviour** -- one row per test attempt,
                        successes *and* failures
``proxy_scores``        **calculated state** -- versioned snapshots derived from
                        observations
======================  =========================================================

Nothing here performs I/O at import time. Timestamps are ``TIMESTAMPTZ`` with
database-side defaults so four independent worker processes cannot disagree
because of clock skew. Integrity rules live in CHECK constraints and foreign keys
as well as in Python: the database protects itself.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    validates,
)
from sqlalchemy.types import TypeDecorator

from core.identity import (
    PROTOCOL_MTPROTO,
    SECRET_MAX_LENGTH,
    SERVER_MAX_LENGTH,
    ProxySecret,
    mask_secret,
)
from core.logger import DEFAULT_ERROR_MESSAGE_LIMIT

__all__ = [
    "DEFAULT_LEASE_SECONDS",
    "ERROR_MESSAGE_MAX_LENGTH",
    "RELIABILITY_MAX",
    "SCORE_MAX",
    "SCORE_MIN",
    "SCORING_VERSION_V1",
    "TESTER_VERSION_DEFAULT",
    "Base",
    "ErrorCategory",
    "Proxy",
    "ProxyDiscovery",
    "ProxyObservation",
    "ProxyScore",
    "SourceType",
    "masked_secret_text",
    "utcnow",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Bumped whenever the scoring formula changes, so an old score can be explained
#: rather than silently compared against a new one. Mandatory per the brief.
SCORING_VERSION_V1: Final = "v1"

#: Placeholder until Task 005 ships the real tester; recorded on every
#: observation so measurement methodology is always attributable.
TESTER_VERSION_DEFAULT: Final = "v0"

#: Default lease length for a claimed proxy. Must exceed the tester's overall
#: timeout so a slow-but-alive test is not double-claimed mid-flight.
DEFAULT_LEASE_SECONDS: Final = 300.0

SCORE_MIN: Final = Decimal("0")
SCORE_MAX: Final = Decimal("100")
RELIABILITY_MAX: Final = Decimal("100")

#: Mirrors ``core.logger.DEFAULT_ERROR_MESSAGE_LIMIT`` and the CHECK constraint.
ERROR_MESSAGE_MAX_LENGTH: Final = DEFAULT_ERROR_MESSAGE_LIMIT

#: Naming convention so Alembic can drop or alter constraints by name later.
#: Without it PostgreSQL invents names and autogenerate produces unusable diffs.
NAMING_CONVENTION: Final[dict[str, str]] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def masked_secret_text(value: Any) -> str:
    """Mask whatever form a secret happens to be in, without ever raising.

    ``repr`` runs in debuggers, in pytest failure output and inside log lines --
    the worst possible places for an exception. Accepting ``None``, ``str`` and
    :class:`~core.identity.ProxySecret` means it can be called on a pending
    object, a half-built one, or a loaded row with equal safety.
    """
    if value is None:
        return "-"
    if isinstance(value, ProxySecret):
        return value.masked
    if isinstance(value, str):
        return mask_secret(value)
    return "-"


def utcnow() -> datetime:
    """The only way this codebase produces a timestamp.

    Always timezone-aware UTC. asyncpg rejects naive datetimes for
    ``TIMESTAMPTZ``, and a naive local time would make observations from
    differently-configured hosts incomparable.
    """
    return datetime.now(UTC)


class ErrorCategory(StrEnum):
    """Normalised failure taxonomy for observations.

    Stored as ``VARCHAR`` rather than a PostgreSQL enum on purpose: the brief
    warns against overfitting the taxonomy, and a native enum makes every new
    category a migration. Task 005 refines this list against real Telethon
    behaviour.

    ``WRONG_SECRET`` is deliberately **absent**. MTProxy drops bad-secret payloads
    without RST or error, so a wrong secret is indistinguishable from a
    blackholed endpoint; claiming otherwise would fabricate a diagnosis. Those
    cases surface as :attr:`MT_PROTO_TIMEOUT` (see ``spike/AUDIT.md`` §3).

    The names from the original Task 002 outline map as follows, so Task 005 does
    not reinvent them: ``TCP_UNREACHABLE`` -> :attr:`TCP_ERROR`,
    ``MTPROXY_TIMEOUT`` -> :attr:`MT_PROTO_TIMEOUT`,
    ``MTPROXY_PROTOCOL_ERROR`` -> :attr:`PROTOCOL_ERROR`,
    ``TELEGRAM_CONNECTION_ERROR`` -> :attr:`TELEGRAM_RPC_ERROR`,
    ``INVALID_PROXY`` -> :attr:`PROTOCOL_ERROR`, ``UNKNOWN`` ->
    :attr:`UNKNOWN_ERROR`.
    """

    SUCCESS = "SUCCESS"
    DNS_ERROR = "DNS_ERROR"
    TCP_TIMEOUT = "TCP_TIMEOUT"
    TCP_REFUSED = "TCP_REFUSED"
    TCP_ERROR = "TCP_ERROR"
    MT_PROTO_TIMEOUT = "MT_PROTO_TIMEOUT"
    PROTOCOL_ERROR = "PROTOCOL_ERROR"
    TELEGRAM_RPC_ERROR = "TELEGRAM_RPC_ERROR"
    API_AUTH_ERROR = "API_AUTH_ERROR"
    UNKNOWN_ERROR = "UNKNOWN_ERROR"


class SourceType(StrEnum):
    """Where a discovery came from. ``VARCHAR`` for the same reason as above."""

    TELEGRAM_CHANNEL = "telegram_channel"
    HTTP_PAGE = "http_page"
    RAW_TEXT = "raw_text"
    MANUAL = "manual"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


class SecretText(TypeDecorator):
    """``TEXT`` that round-trips through :class:`~core.identity.ProxySecret`.

    On the way out of the database the value is wrapped, so the plaintext is only
    reachable via ``.reveal()`` and any accidental ``str()``/f-string/log line
    yields the masked form. On the way in, a plain ``str`` is accepted so callers
    need not construct the wrapper.
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        del dialect
        if value is None:
            return None
        if isinstance(value, ProxySecret):
            return value.reveal()
        if isinstance(value, str):
            return ProxySecret(value).reveal()
        msg = f"secret must be str or ProxySecret, got {type(value).__name__}"
        raise TypeError(msg)

    def process_result_value(self, value: Any, dialect: Any) -> ProxySecret | None:
        del dialect
        if value is None:
            return None
        return ProxySecret(value)


# ---------------------------------------------------------------------------
# Declarative base
# ---------------------------------------------------------------------------


class Base(DeclarativeBase):
    """Declarative base with an explicit constraint-naming convention."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


# ---------------------------------------------------------------------------
# Proxy -- identity
# ---------------------------------------------------------------------------


class Proxy(Base):
    """One distinct, normalised MTProto proxy configuration.

    ``fingerprint`` is the identity: ``sha256`` over
    ``protocol + normalized server + port + secret`` (see
    :func:`core.identity.compute_fingerprint`). The UNIQUE index on it is what
    makes 10,000 copies of one configuration collapse to a single row.

    The secret is part of the identity because several distinct secrets routinely
    share one ``server:port``; hashing the endpoint alone would merge different
    proxies and lose candidates.

    Deletion policy: this row is normally never deleted. ``is_active`` is the
    soft-delete switch. Observations use ``ON DELETE RESTRICT`` so a hard delete
    is refused while measurement history exists -- history is the asset this
    platform is built on, and losing it silently would be the worst failure mode.
    """

    __tablename__ = "proxies"
    __table_args__ = (
        CheckConstraint("port >= 1 AND port <= 65535", name="port_range"),
        CheckConstraint("test_attempts >= 0", name="test_attempts_non_negative"),
        CheckConstraint(
            f"char_length(secret) > 0 AND char_length(secret) <= {SECRET_MAX_LENGTH}",
            name="secret_length",
        ),
        CheckConstraint("char_length(server) > 0", name="server_not_blank"),
        # The claim index: serves
        #   WHERE is_active AND next_test_at <= now() ORDER BY next_test_at
        # Partial on `is_active` so retired proxies cost nothing to index.
        #
        # `next_test_at` is NOT NULL by design (see the column comment). A
        # nullable version would need `ORDER BY next_test_at NULLS FIRST`, and a
        # plain ASC btree stores NULLs *last* -- neither a forward nor a backward
        # scan could serve that ordering, forcing an explicit NULLS FIRST index.
        # Defaulting to now() ("a new proxy is immediately due") removes the NULL
        # case, the special index, and a whole class of claim-query bugs.
        #
        # The lease predicate cannot be indexed at all: partial index predicates
        # must be IMMUTABLE and `now()` is only STABLE. It stays a residual
        # filter, which is cheap because the index already narrowed the rows.
        Index("ix_proxies_due", "next_test_at", postgresql_where=text("is_active")),
        # Reporting: "which proxies worked recently". Written on every success,
        # so it carries real churn; justified because top-N over the last 6h is a
        # stated MVP query.
        Index("ix_proxies_last_success_at", "last_success_at"),
        # Debugging / abuse investigation / future per-host throttling. `server`
        # never changes after insert, so this costs inserts only.
        Index("ix_proxies_server", "server"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    protocol: Mapped[str] = mapped_column(
        String(16), nullable=False, default=PROTOCOL_MTPROTO, server_default=PROTOCOL_MTPROTO
    )
    server: Mapped[str] = mapped_column(String(SERVER_MAX_LENGTH), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Credential-like. Wrapped so it cannot be printed by accident.
    secret: Mapped[ProxySecret] = mapped_column(SecretText(), nullable=False)
    #: 64-char sha256 hex. UNIQUE -- the identity of a proxy.
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )

    # --- lifecycle timestamps (all TIMESTAMPTZ, all UTC) ---
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    #: Refreshed by discovery on every sighting. Distinct from `last_tested_*`:
    # "seen in a channel" and "tested by us" are unrelated facts.
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # --- work scheduling / lease (see docs/DECISION_LOG.md D-024) ---
    #: When this proxy next becomes eligible for testing. NOT NULL, defaulting
    #: to now() so a freshly discovered proxy is immediately due and the claim
    #: query needs no NULL handling. Set forward by the tester after each attempt.
    next_test_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    #: Lease expiry. A row is claimable while this is NULL or in the past, which
    #: is what makes a crashed tester's claim self-heal instead of stranding the
    #: proxy forever.
    test_lock_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    test_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    last_test_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Canonical "last tested at". The earlier outline called this
    #: `last_tested_at`; one column, not two aliases for the same fact.
    last_test_finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Denormalised from the newest observation so a claim query can skip
    #: recently-broken proxies without joining. Nullable and advisory only --
    #: ``proxy_observations`` remains the source of truth.
    last_error_category: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # lazy="raise": any implicit load in async context fails loudly instead of
    # emitting surprise I/O. Callers must choose selectinload/joinedload.
    discoveries: Mapped[list[ProxyDiscovery]] = relationship(
        back_populates="proxy",
        lazy="raise",
        passive_deletes=True,
        cascade="all, delete-orphan",
    )
    observations: Mapped[list[ProxyObservation]] = relationship(
        back_populates="proxy",
        lazy="raise",
        passive_deletes=True,
    )
    scores: Mapped[list[ProxyScore]] = relationship(
        back_populates="proxy",
        lazy="raise",
        passive_deletes=True,
        cascade="all, delete-orphan",
    )

    @validates("secret")
    def _coerce_secret(self, _key: str, value: str | ProxySecret | None) -> ProxySecret | None:
        """Wrap a plaintext secret at assignment time.

        :class:`SecretText` only converts on a database round trip, so without
        this a freshly constructed -- and therefore most often printed -- ``Proxy``
        would hold a bare ``str``, defeating the wrapper exactly when it matters
        most. Fires on load too, where the value is already a ``ProxySecret``.
        """
        if value is None or isinstance(value, ProxySecret):
            return value
        return ProxySecret(value)

    def __repr__(self) -> str:
        # Never includes the secret -- only its masked form. Must not raise.
        return (
            f"<Proxy id={self.id} {self.protocol}://{self.server}:{self.port} "
            f"secret={masked_secret_text(self.secret)} active={self.is_active}>"
        )


# ---------------------------------------------------------------------------
# ProxyDiscovery -- provenance
# ---------------------------------------------------------------------------


class ProxyDiscovery(Base):
    """One sighting of one proxy by one source. Append-only event log.

    ``proxy_id`` is deliberately **not** unique: a proxy is routinely found by
    many channels and by the same channel many times.

    ``raw_reference`` stores a **sanitised** reference, never the raw link. A
    public ``tg://proxy?...&secret=...`` URL is a credential; duplicating it per
    discovery would multiply the secret across the table for no analytical gain,
    since the secret already lives once on the ``proxies`` row. Callers pass the
    link through :func:`core.logger.scrub_secrets` first.

    ``ON DELETE CASCADE``: provenance has no meaning without the identity it
    describes, and unlike observations it carries no measurement value.
    """

    __tablename__ = "proxy_discoveries"
    __table_args__ = (
        CheckConstraint("char_length(source_name) > 0", name="source_name_not_blank"),
        # Provenance lookups: "what did this proxy come from", "what did this
        # source yield", and time-range sweeps.
        Index("ix_proxy_discoveries_proxy_id", "proxy_id"),
        Index("ix_proxy_discoveries_discovered_at", "discovered_at"),
        Index("ix_proxy_discoveries_source_type", "source_type"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    proxy_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("proxies.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_name: Mapped[str] = mapped_column(String(255), nullable=False)
    #: Nullable: a manual import has no URL.
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    #: Sanitised. See the class docstring -- must not contain a secret.
    raw_reference: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    proxy: Mapped[Proxy] = relationship(back_populates="discoveries", lazy="raise")

    def __repr__(self) -> str:
        return (
            f"<ProxyDiscovery id={self.id} proxy_id={self.proxy_id} "
            f"{self.source_type}:{self.source_name}>"
        )


# ---------------------------------------------------------------------------
# ProxyObservation -- measured behaviour
# ---------------------------------------------------------------------------


class ProxyObservation(Base):
    """The outcome of one test attempt. The highest-volume table in the system.

    **Failures are as valuable as successes** and are never discarded: reliability
    is a ratio, so dropping failures would inflate every score.

    Three latency columns mirror the tester's three phases -- TCP reachability,
    MTProto transport, and end-to-end Telegram connectivity. TCP success says
    nothing about MTProto validity, which is why they are separate measurements
    rather than one number.

    Latencies are ``DOUBLE PRECISION`` rather than ``NUMERIC``: these are sensor
    readings at millions of rows, where storage and comparison speed matter more
    than exact decimal semantics. Sub-millisecond precision is real (a LAN
    handshake can be 0.4 ms), so an integer column would be lossy.

    ``error_message_safe`` is capped at the database level as well as by
    :func:`core.logger.safe_error_message`. Full tracebacks are never stored:
    they are large, they repeat, and they are the most likely place for a secret
    to hide.
    """

    __tablename__ = "proxy_observations"
    __table_args__ = (
        # A failure without a category is unusable for scoring; the database
        # refuses it rather than trusting every caller.
        CheckConstraint("success OR error_category IS NOT NULL", name="failure_needs_category"),
        CheckConstraint(
            "tcp_connect_ms IS NULL OR tcp_connect_ms >= 0", name="tcp_ms_non_negative"
        ),
        CheckConstraint(
            "mtproto_connect_ms IS NULL OR mtproto_connect_ms >= 0", name="mtproto_ms_non_negative"
        ),
        CheckConstraint(
            "total_latency_ms IS NULL OR total_latency_ms >= 0", name="total_ms_non_negative"
        ),
        CheckConstraint(
            "error_message_safe IS NULL "
            f"OR char_length(error_message_safe) <= {ERROR_MESSAGE_MAX_LENGTH}",
            name="error_message_bounded",
        ),
        # The scoring query: per-proxy window aggregation over 1h/6h/24h.
        # Plain ASC: PostgreSQL scans a btree backwards, so this also serves
        # `ORDER BY observed_at DESC` without a second index.
        Index("ix_proxy_observations_proxy_id_observed_at", "proxy_id", "observed_at"),
        # Retention sweeps (`DELETE WHERE observed_at < x`). The composite index
        # above cannot serve this because `proxy_id` leads it.
        Index("ix_proxy_observations_observed_at", "observed_at"),
        # Latency aggregation only ever reads successes. A partial index is
        # roughly half the size of the (success, observed_at) composite the brief
        # suggested, and serves the same query -- a boolean leading column buys
        # nothing that the filter cannot. See docs/DECISION_LOG.md D-025.
        Index(
            "ix_proxy_observations_success_observed_at",
            "observed_at",
            postgresql_where=text("success"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    proxy_id: Mapped[int] = mapped_column(
        BigInteger,
        # RESTRICT: deleting a proxy must not silently destroy measurement
        # history. Pruning is an explicit retention job, never a cascade.
        ForeignKey("proxies.id", ondelete="RESTRICT"),
        nullable=False,
    )
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)

    tcp_connect_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    mtproto_connect_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: Phase 3, end-to-end. The earlier outline called this `e2e_latency_ms`.
    total_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)

    #: One of :class:`ErrorCategory`. NULL on success.
    error_category: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: Scrubbed and length-capped. Never a traceback, never a secret.
    error_message_safe: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Which tester build produced this. Without it a methodology change looks
    #: like a proxy behaviour change.
    tester_version: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=TESTER_VERSION_DEFAULT,
        server_default=TESTER_VERSION_DEFAULT,
    )
    #: Where the test ran from. Latency is meaningless without a vantage point.
    test_location: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    proxy: Mapped[Proxy] = relationship(back_populates="observations", lazy="raise")

    def __repr__(self) -> str:
        return (
            f"<ProxyObservation id={self.id} proxy_id={self.proxy_id} "
            f"success={self.success} category={self.error_category or '-'} "
            f"total_ms={self.total_latency_ms}>"
        )


# ---------------------------------------------------------------------------
# ProxyScore -- calculated state
# ---------------------------------------------------------------------------


class ProxyScore(Base):
    """A versioned snapshot of a proxy's calculated quality. Append-only.

    A snapshot, **not** an authoritative current value. Scores are derived from
    observations and recomputed; storing history rather than one row per proxy
    means a scoring-algorithm change never destroys the ability to explain why a
    proxy ranked differently last week. ``scoring_version`` is mandatory for the
    same reason ``tester_version`` is on observations.

    ``reliability_*`` is NULL -- not zero -- when the matching sample count is
    zero, enforced by CHECK. "0% reliability" and "no data" are different facts
    and conflating them is exactly how a never-tested proxy ends up looking bad
    or, worse, how 1/1 success ends up looking perfect.

    ``NUMERIC`` is used here (unlike observations) because scores are ranked and
    compared; exact decimal semantics keep ordering deterministic.
    """

    __tablename__ = "proxy_scores"
    __table_args__ = (
        CheckConstraint("score >= 0 AND score <= 100", name="score_range"),
        CheckConstraint(
            "reliability_1h IS NULL OR (reliability_1h >= 0 AND reliability_1h <= 100)",
            name="reliability_1h_range",
        ),
        CheckConstraint(
            "reliability_6h IS NULL OR (reliability_6h >= 0 AND reliability_6h <= 100)",
            name="reliability_6h_range",
        ),
        CheckConstraint(
            "reliability_24h IS NULL OR (reliability_24h >= 0 AND reliability_24h <= 100)",
            name="reliability_24h_range",
        ),
        CheckConstraint("latency_p50_ms IS NULL OR latency_p50_ms >= 0", name="p50_non_negative"),
        CheckConstraint("latency_p95_ms IS NULL OR latency_p95_ms >= 0", name="p95_non_negative"),
        CheckConstraint(
            "latency_p95_ms IS NULL OR latency_p50_ms IS NULL OR latency_p95_ms >= latency_p50_ms",
            name="p95_at_least_p50",
        ),
        CheckConstraint("sample_count_1h >= 0", name="samples_1h_non_negative"),
        CheckConstraint("sample_count_6h >= 0", name="samples_6h_non_negative"),
        CheckConstraint("sample_count_24h >= 0", name="samples_24h_non_negative"),
        # No reliability without samples: blocks the "1/1 == 100%" trap at the
        # storage layer, independent of whatever the scorer computes.
        CheckConstraint(
            "sample_count_1h > 0 OR reliability_1h IS NULL", name="reliability_1h_needs_samples"
        ),
        CheckConstraint(
            "sample_count_6h > 0 OR reliability_6h IS NULL", name="reliability_6h_needs_samples"
        ),
        CheckConstraint(
            "sample_count_24h > 0 OR reliability_24h IS NULL", name="reliability_24h_needs_samples"
        ),
        CheckConstraint("char_length(scoring_version) > 0", name="scoring_version_not_blank"),
        # "Latest score per proxy" is the reporting hot path. DESC matches
        # `ORDER BY calculated_at DESC LIMIT 1` and `DISTINCT ON (proxy_id)`.
        Index("ix_proxy_scores_proxy_id_calculated_at", "proxy_id", "calculated_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    proxy_id: Mapped[int] = mapped_column(
        BigInteger,
        # CASCADE: scores are recomputable from observations, and observations are
        # RESTRICT-protected, so this can only fire on an already-sanitised proxy.
        ForeignKey("proxies.id", ondelete="CASCADE"),
        nullable=False,
    )
    calculated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    score: Mapped[Decimal] = mapped_column(Numeric(6, 3), nullable=False)

    reliability_1h: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    reliability_6h: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    reliability_24h: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)

    latency_p50_ms: Mapped[Decimal | None] = mapped_column(Numeric(10, 3), nullable=True)
    latency_p95_ms: Mapped[Decimal | None] = mapped_column(Numeric(10, 3), nullable=True)

    sample_count_1h: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    sample_count_6h: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    sample_count_24h: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )

    #: Mandatory. Explains why an old score differs from a new one.
    scoring_version: Mapped[str] = mapped_column(
        String(16), nullable=False, default=SCORING_VERSION_V1, server_default=SCORING_VERSION_V1
    )

    proxy: Mapped[Proxy] = relationship(back_populates="scores", lazy="raise")

    def __repr__(self) -> str:
        return (
            f"<ProxyScore id={self.id} proxy_id={self.proxy_id} score={self.score} "
            f"version={self.scoring_version} samples_24h={self.sample_count_24h}>"
        )
