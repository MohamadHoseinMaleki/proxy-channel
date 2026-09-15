"""Integration tests for the schema against a real PostgreSQL.

These exist because a CHECK constraint, an ``ON DELETE`` action and a UNIQUE
index are *database* behaviour. Compiling DDL in a unit test proves the intent
was declared; only executing it proves PostgreSQL accepts and enforces it. Every
test here really runs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core.identity import ProxySecret, compute_fingerprint
from core.models import (
    SCORING_VERSION_V1,
    TESTER_VERSION_DEFAULT,
    ErrorCategory,
    Proxy,
    ProxyDiscovery,
    ProxyObservation,
    ProxyScore,
    SourceType,
    utcnow,
)
from tests.integration.conftest import make_proxy

#: Everything here needs a live PostgreSQL. The marker lets the suite be run or
#: skipped as a unit; with no database reachable the fixtures skip cleanly.
pytestmark = pytest.mark.integration

SECRET = "ee" + "a1" * 15


async def insert(session: AsyncSession, *rows: object) -> None:
    session.add_all(list(rows))
    await session.flush()


async def insert_expect_rejected(session: AsyncSession, row: object) -> str:
    """Add ``row``, assert the database refused it, and return the error text.

    Rolls back so the session stays usable, and so one rejected row cannot leave
    a transaction that poisons the rest of the test.
    """
    session.add(row)
    with pytest.raises(IntegrityError) as info:
        await session.flush()
    await session.rollback()
    return str(info.value)


class TestRoundTrip:
    async def test_a_proxy_survives_an_insert_and_select(self, session: AsyncSession) -> None:
        fingerprint = compute_fingerprint(server="proxy.example.com", port=443, secret=SECRET)
        await insert(session, make_proxy())
        await session.commit()

        loaded = (await session.execute(select(Proxy))).scalar_one()
        assert loaded.id is not None
        assert loaded.server == "proxy.example.com"
        assert loaded.port == 443
        assert loaded.fingerprint == fingerprint
        assert loaded.protocol == "mtproto"

    async def test_the_secret_comes_back_as_a_proxy_secret(self, session: AsyncSession) -> None:
        await insert(session, make_proxy())
        await session.commit()

        loaded = (await session.execute(select(Proxy))).scalar_one()
        # The whole point of SecretText: a loaded secret is never a bare str.
        assert isinstance(loaded.secret, ProxySecret)
        assert loaded.secret.reveal() == SECRET

    async def test_a_loaded_secret_cannot_be_printed(self, session: AsyncSession) -> None:
        await insert(session, make_proxy())
        await session.commit()

        loaded = (await session.execute(select(Proxy))).scalar_one()
        for rendered in (str(loaded.secret), repr(loaded), f"{loaded.secret}"):
            assert SECRET not in rendered

    async def test_the_secret_is_stored_verbatim_in_the_column(self, session: AsyncSession) -> None:
        # Stored, not hashed: the tester needs the real value to connect. The
        # fingerprint is the only one-way digest in the schema.
        await insert(session, make_proxy())
        await session.commit()
        stored = (await session.execute(text("SELECT secret FROM proxies"))).scalar_one()
        assert stored == SECRET

    async def test_numeric_scores_round_trip_exactly(self, session: AsyncSession) -> None:
        await insert(session, make_proxy())
        await session.flush()
        proxy_id = (await session.execute(select(Proxy.id))).scalar_one()
        await insert(
            session,
            ProxyScore(
                proxy_id=proxy_id,
                score=Decimal("87.125"),
                reliability_24h=Decimal("99.99"),
                latency_p50_ms=Decimal("123.456"),
                latency_p95_ms=Decimal("789.012"),
                sample_count_24h=10,
            ),
        )
        await session.commit()

        loaded = (await session.execute(select(ProxyScore))).scalar_one()
        assert loaded.score == Decimal("87.125")
        assert loaded.reliability_24h == Decimal("99.99")
        assert loaded.latency_p50_ms == Decimal("123.456")

    async def test_sub_millisecond_latency_survives(self, session: AsyncSession) -> None:
        # A LAN handshake really can be 0.4 ms; an integer column would lose it,
        # which is why latencies are DOUBLE PRECISION.
        await insert(session, make_proxy())
        await session.flush()
        proxy_id = (await session.execute(select(Proxy.id))).scalar_one()
        await insert(session, ProxyObservation(proxy_id=proxy_id, success=True, tcp_connect_ms=0.4))
        await session.commit()

        loaded = (await session.execute(select(ProxyObservation))).scalar_one()
        assert loaded.tcp_connect_ms == pytest.approx(0.4)

    async def test_timestamps_come_back_timezone_aware(self, session: AsyncSession) -> None:
        await insert(session, make_proxy())
        await session.commit()

        loaded = (await session.execute(select(Proxy))).scalar_one()
        for value in (
            loaded.created_at,
            loaded.updated_at,
            loaded.first_seen_at,
            loaded.next_test_at,
        ):
            assert value.tzinfo is not None, "naive datetime from a TIMESTAMPTZ column"
            assert value.utcoffset() == timedelta(0)


class TestServerSideDefaults:
    async def test_lifecycle_timestamps_are_filled_by_the_database(
        self, session: AsyncSession
    ) -> None:
        before = utcnow()
        await insert(session, make_proxy())
        await session.commit()
        after = utcnow()

        loaded = (await session.execute(select(Proxy))).scalar_one()
        for name in ("created_at", "updated_at", "first_seen_at"):
            value = getattr(loaded, name)
            assert before <= value <= after, f"{name} not defaulted to now()"

    async def test_a_new_proxy_is_immediately_due(self, session: AsyncSession) -> None:
        # next_test_at DEFAULT now() is what lets a freshly discovered proxy be
        # claimed without a special case, and what removes NULLS FIRST from the
        # claim index.
        await insert(session, make_proxy())
        await session.commit()

        due = (
            await session.execute(text("SELECT count(*) FROM proxies WHERE next_test_at <= now()"))
        ).scalar_one()
        assert due == 1

    async def test_next_test_at_is_never_null(self, session: AsyncSession) -> None:
        await insert(session, make_proxy())
        await session.commit()
        nulls = (
            await session.execute(text("SELECT count(*) FROM proxies WHERE next_test_at IS NULL"))
        ).scalar_one()
        assert nulls == 0

    async def test_is_active_defaults_true(self, session: AsyncSession) -> None:
        await insert(session, make_proxy())
        await session.commit()
        assert (await session.execute(select(Proxy.is_active))).scalar_one() is True

    async def test_test_attempts_defaults_to_zero(self, session: AsyncSession) -> None:
        await insert(session, make_proxy())
        await session.commit()
        assert (await session.execute(select(Proxy.test_attempts))).scalar_one() == 0

    async def test_lease_columns_start_unset(self, session: AsyncSession) -> None:
        await insert(session, make_proxy())
        await session.commit()
        loaded = (await session.execute(select(Proxy))).scalar_one()
        assert loaded.test_lock_until is None
        assert loaded.last_test_started_at is None
        assert loaded.last_test_finished_at is None
        assert loaded.last_success_at is None
        assert loaded.last_failure_at is None

    async def test_defaults_apply_for_a_raw_insert_too(self, session: AsyncSession) -> None:
        # server_default (not just a Python `default`) means any writer -- psql,
        # a future migration, a bulk COPY -- gets the same invariants.
        await session.execute(
            text(
                "INSERT INTO proxies (server, port, secret, fingerprint) "
                "VALUES ('raw.example.com', 443, :secret, :fp)"
            ),
            {"secret": SECRET, "fp": "0" * 64},
        )
        await session.commit()

        row = (
            await session.execute(
                text(
                    "SELECT protocol, is_active, test_attempts, "
                    "(next_test_at <= now()) AS due, created_at IS NOT NULL AS stamped "
                    "FROM proxies"
                )
            )
        ).one()
        assert row.protocol == "mtproto"
        assert row.is_active is True
        assert row.test_attempts == 0
        assert row.due is True
        assert row.stamped is True

    async def test_tester_and_scoring_versions_default(self, session: AsyncSession) -> None:
        await insert(session, make_proxy())
        await session.flush()
        proxy_id = (await session.execute(select(Proxy.id))).scalar_one()
        await insert(
            session,
            ProxyObservation(proxy_id=proxy_id, success=True),
            ProxyScore(proxy_id=proxy_id, score=Decimal("50")),
        )
        await session.commit()

        assert (await session.execute(select(ProxyObservation.tester_version))).scalar_one() == (
            TESTER_VERSION_DEFAULT
        )
        assert (await session.execute(select(ProxyScore.scoring_version))).scalar_one() == (
            SCORING_VERSION_V1
        )


class TestFingerprintUniqueness:
    async def test_duplicate_fingerprint_is_rejected(self, session: AsyncSession) -> None:
        await insert(session, make_proxy())
        await session.commit()

        error = await insert_expect_rejected(session, make_proxy())
        assert "uq_proxies_fingerprint" in error
        assert (await session.execute(select(func.count(Proxy.id)))).scalar_one() == 1

    async def test_cosmetic_variants_collide_on_purpose(self, session: AsyncSession) -> None:
        # This is the constraint doing its job: 10,000 sightings of one
        # configuration must collapse to one row.
        await insert(session, make_proxy(server="proxy.example.com"))
        await session.commit()

        duplicates = [
            make_proxy(server="PROXY.EXAMPLE.COM"),
            make_proxy(server="  proxy.example.com  "),
            make_proxy(server="proxy.example.com."),
            make_proxy(secret=f"  {SECRET}  "),
        ]
        for duplicate in duplicates:
            await insert_expect_rejected(session, duplicate)

        assert (await session.execute(select(func.count(Proxy.id)))).scalar_one() == 1

    async def test_a_different_secret_on_the_same_endpoint_is_a_new_row(
        self, session: AsyncSession
    ) -> None:
        # The reason the secret is part of the fingerprint: one host routinely
        # advertises several distinct secrets.
        await insert(session, make_proxy(secret="ee" + "11" * 15))
        await insert(session, make_proxy(secret="ee" + "22" * 15))
        await session.commit()

        assert (await session.execute(select(func.count(Proxy.id)))).scalar_one() == 2

    async def test_a_different_port_is_a_new_row(self, session: AsyncSession) -> None:
        await insert(session, make_proxy(port=443), make_proxy(port=8443))
        await session.commit()
        assert (await session.execute(select(func.count(Proxy.id)))).scalar_one() == 2


class TestProxyConstraints:
    # compute_fingerprint rejects these values in Python first -- correct, since
    # identity must be well-defined -- so the CHECK is exercised with an explicit
    # fingerprint to prove the database enforces it independently.

    @pytest.mark.parametrize("port", [0, -1, 65536, 99999])
    async def test_port_range_is_enforced(self, session: AsyncSession, port: int) -> None:
        error = await insert_expect_rejected(
            session, make_proxy(port=port, fingerprint=f"{port % 16:0>64x}"[:64])
        )
        assert "ck_proxies_port_range" in error

    @pytest.mark.parametrize("port", [1, 443, 65535])
    async def test_valid_ports_are_accepted(self, session: AsyncSession, port: int) -> None:
        await insert(session, make_proxy(port=port))
        await session.commit()
        assert (await session.execute(select(func.count(Proxy.id)))).scalar_one() == 1

    async def test_blank_server_is_rejected(self, session: AsyncSession) -> None:
        error = await insert_expect_rejected(session, make_proxy(server="", fingerprint="4" * 64))
        assert "ck_proxies_server_not_blank" in error

    async def test_overlong_secret_is_rejected_by_the_database(self, session: AsyncSession) -> None:
        # ProxySecret refuses this in Python, so the CHECK is proved with raw SQL:
        # the database protects itself independently of any application validation.
        with pytest.raises(IntegrityError) as info:
            await session.execute(
                text(
                    "INSERT INTO proxies (server, port, secret, fingerprint) "
                    "VALUES ('h.example.com', 443, :secret, :fp)"
                ),
                {"secret": "a" * 513, "fp": "1" * 64},
            )
        await session.rollback()
        assert "ck_proxies_secret_length" in str(info.value)

    async def test_maximum_length_secret_is_accepted(self, session: AsyncSession) -> None:
        from core.identity import SECRET_MAX_LENGTH

        secret = "a" * SECRET_MAX_LENGTH
        await session.execute(
            text(
                "INSERT INTO proxies (server, port, secret, fingerprint) "
                "VALUES ('h.example.com', 443, :secret, :fp)"
            ),
            {"secret": secret, "fp": "2" * 64},
        )
        await session.commit()
        assert (await session.execute(select(func.count(Proxy.id)))).scalar_one() == 1

    async def test_empty_secret_is_rejected_by_the_database(self, session: AsyncSession) -> None:
        with pytest.raises(IntegrityError) as info:
            await session.execute(
                text(
                    "INSERT INTO proxies (server, port, secret, fingerprint) "
                    "VALUES ('h.example.com', 443, '', :fp)"
                ),
                {"fp": "3" * 64},
            )
        await session.rollback()
        assert "ck_proxies_secret_length" in str(info.value)

    async def test_negative_test_attempts_is_rejected(self, session: AsyncSession) -> None:
        await insert(session, make_proxy())
        await session.commit()
        with pytest.raises(IntegrityError) as info:
            await session.execute(text("UPDATE proxies SET test_attempts = -1"))
            await session.commit()
        await session.rollback()
        assert "ck_proxies_test_attempts_non_negative" in str(info.value)

    async def test_protocol_accepts_an_unknown_value(self, session: AsyncSession) -> None:
        # Deliberate: no CHECK on protocol, so a future protocol is a code change
        # rather than a migration. The fingerprint still keeps identities apart.
        await insert(session, make_proxy(protocol="socks5"))
        await session.commit()
        assert (await session.execute(select(Proxy.protocol))).scalar_one() == "socks5"

    async def test_two_protocols_on_one_endpoint_are_different_identities(
        self, session: AsyncSession
    ) -> None:
        await insert(
            session, make_proxy(protocol="mtproto"), make_proxy(protocol="socks5", port=1080)
        )
        await session.commit()
        assert (await session.execute(select(func.count(Proxy.id)))).scalar_one() == 2


class TestObservationConstraints:
    async def _proxy_id(self, session: AsyncSession) -> int:
        await insert(session, make_proxy())
        await session.flush()
        return (await session.execute(select(Proxy.id))).scalar_one()

    async def test_a_failure_without_a_category_is_rejected(self, session: AsyncSession) -> None:
        proxy_id = await self._proxy_id(session)
        error = await insert_expect_rejected(
            session, ProxyObservation(proxy_id=proxy_id, success=False)
        )
        assert "ck_proxy_observations_failure_needs_category" in error

    async def test_a_failure_with_a_category_is_accepted(self, session: AsyncSession) -> None:
        proxy_id = await self._proxy_id(session)
        await insert(
            session,
            ProxyObservation(
                proxy_id=proxy_id, success=False, error_category=ErrorCategory.DNS_ERROR
            ),
        )
        await session.commit()
        assert (await session.execute(select(func.count(ProxyObservation.id)))).scalar_one() == 1

    async def test_a_success_needs_no_category(self, session: AsyncSession) -> None:
        proxy_id = await self._proxy_id(session)
        await insert(
            session,
            ProxyObservation(
                proxy_id=proxy_id, success=True, tcp_connect_ms=12.5, total_latency_ms=88.25
            ),
        )
        await session.commit()
        loaded = (await session.execute(select(ProxyObservation))).scalar_one()
        assert loaded.error_category is None

    async def test_both_outcomes_can_coexist_for_one_proxy(self, session: AsyncSession) -> None:
        # Failures are first-class rows: dropping them would inflate every score.
        proxy_id = await self._proxy_id(session)
        await insert(
            session,
            ProxyObservation(proxy_id=proxy_id, success=True),
            ProxyObservation(
                proxy_id=proxy_id, success=False, error_category=ErrorCategory.TCP_TIMEOUT
            ),
        )
        await session.commit()
        assert (await session.execute(select(func.count(ProxyObservation.id)))).scalar_one() == 2

    @pytest.mark.parametrize("column", ["tcp_connect_ms", "mtproto_connect_ms", "total_latency_ms"])
    async def test_negative_latency_is_rejected(self, session: AsyncSession, column: str) -> None:
        proxy_id = await self._proxy_id(session)
        error = await insert_expect_rejected(
            session, ProxyObservation(proxy_id=proxy_id, success=True, **{column: -0.001})
        )
        assert "ck_proxy_observations_" in error
        assert "non_negative" in error

    async def test_zero_latency_is_accepted(self, session: AsyncSession) -> None:
        proxy_id = await self._proxy_id(session)
        await insert(session, ProxyObservation(proxy_id=proxy_id, success=True, tcp_connect_ms=0.0))
        await session.commit()
        assert (await session.execute(select(ProxyObservation.tcp_connect_ms))).scalar_one() == 0.0

    async def test_overlong_error_message_is_rejected(self, session: AsyncSession) -> None:
        # Full tracebacks are large, repetitive and the likeliest place for a
        # secret to hide. The database enforces the cap, not just the app.
        proxy_id = await self._proxy_id(session)
        error = await insert_expect_rejected(
            session,
            ProxyObservation(
                proxy_id=proxy_id,
                success=False,
                error_category=ErrorCategory.UNKNOWN_ERROR,
                error_message_safe="x" * 501,
            ),
        )
        assert "ck_proxy_observations_error_message_bounded" in error

    async def test_a_message_at_the_limit_is_accepted(self, session: AsyncSession) -> None:
        from core.models import ERROR_MESSAGE_MAX_LENGTH

        proxy_id = await self._proxy_id(session)
        await insert(
            session,
            ProxyObservation(
                proxy_id=proxy_id,
                success=False,
                error_category=ErrorCategory.UNKNOWN_ERROR,
                error_message_safe="x" * ERROR_MESSAGE_MAX_LENGTH,
            ),
        )
        await session.commit()

    async def test_safe_error_message_output_always_fits(self, session: AsyncSession) -> None:
        # Proves the app-side helper and the DB-side cap agree, so a scrubbed
        # message can never be rejected on length.
        from core.logger import safe_error_message
        from core.models import ERROR_MESSAGE_MAX_LENGTH

        proxy_id = await self._proxy_id(session)
        message = safe_error_message(RuntimeError("y" * 10000))
        assert message is not None
        assert len(message) <= ERROR_MESSAGE_MAX_LENGTH
        await insert(
            session,
            ProxyObservation(
                proxy_id=proxy_id,
                success=False,
                error_category=ErrorCategory.UNKNOWN_ERROR,
                error_message_safe=message,
            ),
        )
        await session.commit()

    async def test_error_category_is_a_plain_string_column(self, session: AsyncSession) -> None:
        # No PostgreSQL enum: a new category must be a code change, not a
        # migration. So an unlisted value is storable by design.
        proxy_id = await self._proxy_id(session)
        await insert(
            session,
            ProxyObservation(proxy_id=proxy_id, success=False, error_category="SOMETHING_NEW"),
        )
        await session.commit()
        assert (await session.execute(select(ProxyObservation.error_category))).scalar_one() == (
            "SOMETHING_NEW"
        )

    async def test_observed_at_is_defaulted(self, session: AsyncSession) -> None:
        proxy_id = await self._proxy_id(session)
        await insert(session, ProxyObservation(proxy_id=proxy_id, success=True))
        await session.commit()
        loaded = (await session.execute(select(ProxyObservation))).scalar_one()
        assert loaded.observed_at.tzinfo is not None
        assert utcnow() - loaded.observed_at < timedelta(minutes=1)


class TestScoreConstraints:
    async def _proxy_id(self, session: AsyncSession) -> int:
        await insert(session, make_proxy())
        await session.flush()
        return (await session.execute(select(Proxy.id))).scalar_one()

    @pytest.mark.parametrize("score", [Decimal("-0.001"), Decimal("100.001"), Decimal("100.0011")])
    async def test_score_out_of_range_is_rejected(
        self, session: AsyncSession, score: Decimal
    ) -> None:
        proxy_id = await self._proxy_id(session)
        error = await insert_expect_rejected(session, ProxyScore(proxy_id=proxy_id, score=score))
        assert "ck_proxy_scores_score_range" in error

    async def test_a_score_beyond_the_column_precision_is_rejected(
        self, session: AsyncSession
    ) -> None:
        # NUMERIC(6,3) tops out at 999.999, so 1000 overflows the *type* before
        # the CHECK can fire -- and asyncpg reports it as a generic DBAPIError,
        # not the DataError one might expect. Two independent guards, different
        # exceptions: worth pinning so nobody "fixes" one and assumes the other
        # covers it.
        from sqlalchemy.exc import DBAPIError

        proxy_id = await self._proxy_id(session)
        session.add(ProxyScore(proxy_id=proxy_id, score=Decimal("1000")))
        with pytest.raises(DBAPIError, match="numeric field overflow"):
            await session.flush()
        await session.rollback()

    async def test_a_score_is_rounded_to_the_column_scale(self, session: AsyncSession) -> None:
        # NUMERIC(6,3) rounds rather than truncates. Pinning it means a scorer
        # that emits more precision than the column holds cannot silently change
        # ranking order.
        proxy_id = await self._proxy_id(session)
        await insert(session, ProxyScore(proxy_id=proxy_id, score=Decimal("50.12345")))
        await session.commit()
        assert (await session.execute(select(ProxyScore.score))).scalar_one() == Decimal("50.123")

    @pytest.mark.parametrize("score", [Decimal("0"), Decimal("100"), Decimal("50.5")])
    async def test_scores_at_the_boundaries_are_accepted(
        self, session: AsyncSession, score: Decimal
    ) -> None:
        proxy_id = await self._proxy_id(session)
        await insert(session, ProxyScore(proxy_id=proxy_id, score=score))
        await session.commit()

    @pytest.mark.parametrize("window", ["1h", "6h", "24h"])
    async def test_reliability_without_samples_is_rejected(
        self, session: AsyncSession, window: str
    ) -> None:
        # "No data" must be NULL, never 0. This is the storage-level guard
        # against a never-tested proxy acquiring a reliability figure.
        proxy_id = await self._proxy_id(session)
        error = await insert_expect_rejected(
            session,
            ProxyScore(
                proxy_id=proxy_id,
                score=Decimal("0"),
                **{f"reliability_{window}": Decimal("100"), f"sample_count_{window}": 0},
            ),
        )
        assert f"ck_proxy_scores_reliability_{window}_needs_samples" in error

    @pytest.mark.parametrize("window", ["1h", "6h", "24h"])
    async def test_null_reliability_with_no_samples_is_accepted(
        self, session: AsyncSession, window: str
    ) -> None:
        proxy_id = await self._proxy_id(session)
        await insert(session, ProxyScore(proxy_id=proxy_id, score=Decimal("0")))
        await session.commit()
        loaded = (await session.execute(select(ProxyScore))).scalar_one()
        assert getattr(loaded, f"reliability_{window}") is None
        assert getattr(loaded, f"sample_count_{window}") == 0

    @pytest.mark.parametrize("value", [Decimal("-0.01"), Decimal("100.01")])
    async def test_reliability_out_of_range_is_rejected(
        self, session: AsyncSession, value: Decimal
    ) -> None:
        proxy_id = await self._proxy_id(session)
        error = await insert_expect_rejected(
            session,
            ProxyScore(
                proxy_id=proxy_id,
                score=Decimal("0"),
                reliability_1h=value,
                sample_count_1h=5,
            ),
        )
        assert "ck_proxy_scores_reliability_1h_range" in error

    async def test_p95_below_p50_is_rejected(self, session: AsyncSession) -> None:
        proxy_id = await self._proxy_id(session)
        error = await insert_expect_rejected(
            session,
            ProxyScore(
                proxy_id=proxy_id,
                score=Decimal("50"),
                latency_p50_ms=Decimal("200"),
                latency_p95_ms=Decimal("100"),
            ),
        )
        assert "ck_proxy_scores_p95_at_least_p50" in error

    async def test_p95_equal_to_p50_is_accepted(self, session: AsyncSession) -> None:
        proxy_id = await self._proxy_id(session)
        await insert(
            session,
            ProxyScore(
                proxy_id=proxy_id,
                score=Decimal("50"),
                latency_p50_ms=Decimal("100"),
                latency_p95_ms=Decimal("100"),
            ),
        )
        await session.commit()

    async def test_percentiles_without_samples_are_accepted(self, session: AsyncSession) -> None:
        # p95 >= p50 only binds when both are present.
        proxy_id = await self._proxy_id(session)
        await insert(
            session, ProxyScore(proxy_id=proxy_id, score=Decimal("50"), latency_p95_ms=Decimal("9"))
        )
        await session.commit()

    async def test_negative_sample_count_is_rejected(self, session: AsyncSession) -> None:
        proxy_id = await self._proxy_id(session)
        error = await insert_expect_rejected(
            session, ProxyScore(proxy_id=proxy_id, score=Decimal("0"), sample_count_1h=-1)
        )
        assert "ck_proxy_scores_samples_1h_non_negative" in error

    async def test_history_is_append_only(self, session: AsyncSession) -> None:
        # Multiple snapshots per proxy: a scoring change must not destroy the
        # ability to explain why a ranking differed last week.
        proxy_id = await self._proxy_id(session)
        for value in ("10", "20", "30"):
            await insert(session, ProxyScore(proxy_id=proxy_id, score=Decimal(value)))
        await session.commit()

        scores = (
            (await session.execute(select(ProxyScore.score).order_by(ProxyScore.id)))
            .scalars()
            .all()
        )
        assert scores == [Decimal("10"), Decimal("20"), Decimal("30")]

    async def test_latest_score_per_proxy_uses_the_index(self, session: AsyncSession) -> None:
        proxy_id = await self._proxy_id(session)
        base = utcnow()
        for offset, value in ((0, "10"), (60, "20"), (120, "30")):
            await insert(
                session,
                ProxyScore(
                    proxy_id=proxy_id,
                    score=Decimal(value),
                    calculated_at=base + timedelta(seconds=offset),
                ),
            )
        await session.commit()

        latest = (
            await session.execute(
                text(
                    "SELECT DISTINCT ON (proxy_id) score FROM proxy_scores "
                    "ORDER BY proxy_id, calculated_at DESC"
                )
            )
        ).scalar_one()
        assert latest == Decimal("30")


class TestForeignKeys:
    async def test_an_observation_needs_a_real_proxy(self, session: AsyncSession) -> None:
        error = await insert_expect_rejected(
            session, ProxyObservation(proxy_id=999_999, success=True)
        )
        assert "fk_proxy_observations_proxy_id_proxies" in error

    async def test_a_discovery_needs_a_real_proxy(self, session: AsyncSession) -> None:
        error = await insert_expect_rejected(
            session,
            ProxyDiscovery(
                proxy_id=999_999,
                source_type=SourceType.TELEGRAM_CHANNEL,
                source_name="@chan",
            ),
        )
        assert "fk_proxy_discoveries_proxy_id_proxies" in error

    async def test_a_score_needs_a_real_proxy(self, session: AsyncSession) -> None:
        error = await insert_expect_rejected(
            session, ProxyScore(proxy_id=999_999, score=Decimal("1"))
        )
        assert "fk_proxy_scores_proxy_id_proxies" in error

    async def test_deleting_a_proxy_with_observations_is_refused(
        self, session: AsyncSession
    ) -> None:
        # The most consequential FK in the schema. Measurement history is the
        # asset this platform exists to build; a proxy delete must not destroy it
        # as a side effect.
        await insert(session, make_proxy())
        await session.flush()
        proxy_id = (await session.execute(select(Proxy.id))).scalar_one()
        await insert(session, ProxyObservation(proxy_id=proxy_id, success=True))
        await session.commit()

        with pytest.raises(IntegrityError) as info:
            await session.execute(text("DELETE FROM proxies"))
            await session.commit()
        await session.rollback()
        assert "fk_proxy_observations_proxy_id_proxies" in str(info.value)

        # Both rows survived.
        assert (await session.execute(select(func.count(Proxy.id)))).scalar_one() == 1
        assert (await session.execute(select(func.count(ProxyObservation.id)))).scalar_one() == 1

    async def test_deleting_a_proxy_cascades_to_discoveries(self, session: AsyncSession) -> None:
        await insert(session, make_proxy())
        await session.flush()
        proxy_id = (await session.execute(select(Proxy.id))).scalar_one()
        await insert(
            session,
            ProxyDiscovery(
                proxy_id=proxy_id, source_type=SourceType.TELEGRAM_CHANNEL, source_name="@chan"
            ),
        )
        await session.commit()

        await session.execute(text("DELETE FROM proxies"))
        await session.commit()
        assert (await session.execute(select(func.count(ProxyDiscovery.id)))).scalar_one() == 0

    async def test_deleting_a_proxy_cascades_to_scores(self, session: AsyncSession) -> None:
        await insert(session, make_proxy())
        await session.flush()
        proxy_id = (await session.execute(select(Proxy.id))).scalar_one()
        await insert(session, ProxyScore(proxy_id=proxy_id, score=Decimal("50")))
        await session.commit()

        await session.execute(text("DELETE FROM proxies"))
        await session.commit()
        assert (await session.execute(select(func.count(ProxyScore.id)))).scalar_one() == 0

    async def test_deleting_a_proxy_without_observations_succeeds(
        self, session: AsyncSession
    ) -> None:
        await insert(session, make_proxy())
        await session.commit()
        await session.execute(text("DELETE FROM proxies"))
        await session.commit()
        assert (await session.execute(select(func.count(Proxy.id)))).scalar_one() == 0

    async def test_soft_delete_is_the_normal_path(self, session: AsyncSession) -> None:
        # is_active is the intended retirement switch; hard deletes are for
        # sanitisation only, and RESTRICT makes that an explicit act.
        await insert(session, make_proxy())
        await session.flush()
        proxy_id = (await session.execute(select(Proxy.id))).scalar_one()
        await insert(session, ProxyObservation(proxy_id=proxy_id, success=True))
        await session.commit()

        await session.execute(text("UPDATE proxies SET is_active = false"))
        await session.commit()
        assert (await session.execute(select(func.count(ProxyObservation.id)))).scalar_one() == 1


class TestRelationships:
    @pytest.mark.parametrize("attribute", ["observations", "discoveries", "scores"])
    async def test_implicit_lazy_loading_raises(
        self, session: AsyncSession, attribute: str
    ) -> None:
        # lazy="raise": under asyncio an implicit load is a hidden round trip that
        # fails outright. Failing loudly here beats emitting surprise I/O there.
        from sqlalchemy.exc import InvalidRequestError

        await insert(session, make_proxy())
        await session.commit()

        loaded = (await session.execute(select(Proxy))).scalar_one()
        with pytest.raises(InvalidRequestError, match="lazy='raise'"):
            getattr(loaded, attribute)

    async def test_explicit_eager_loading_works(self, session: AsyncSession) -> None:
        await insert(session, make_proxy())
        await session.flush()
        proxy_id = (await session.execute(select(Proxy.id))).scalar_one()
        await insert(
            session,
            ProxyObservation(proxy_id=proxy_id, success=True),
            ProxyObservation(
                proxy_id=proxy_id, success=False, error_category=ErrorCategory.TCP_REFUSED
            ),
        )
        await session.commit()

        loaded = (
            await session.execute(select(Proxy).options(selectinload(Proxy.observations)))
        ).scalar_one()
        assert len(loaded.observations) == 2

    async def test_discoveries_and_scores_can_be_eager_loaded_together(
        self, session: AsyncSession
    ) -> None:
        await insert(session, make_proxy())
        await session.flush()
        proxy_id = (await session.execute(select(Proxy.id))).scalar_one()
        await insert(
            session,
            ProxyDiscovery(proxy_id=proxy_id, source_type=SourceType.MANUAL, source_name="seed"),
            ProxyScore(proxy_id=proxy_id, score=Decimal("42")),
        )
        await session.commit()

        loaded = (
            await session.execute(
                select(Proxy).options(selectinload(Proxy.discoveries), selectinload(Proxy.scores))
            )
        ).scalar_one()
        assert len(loaded.discoveries) == 1
        assert len(loaded.scores) == 1


class TestUpdatedTracking:
    async def test_updated_at_advances_on_an_orm_write(self, session: AsyncSession) -> None:
        await insert(session, make_proxy())
        await session.commit()
        original = (await session.execute(select(Proxy.updated_at))).scalar_one()

        proxy = (await session.execute(select(Proxy))).scalar_one()
        proxy.last_seen_at = utcnow() + timedelta(seconds=1)
        await session.commit()

        refreshed = (await session.execute(select(Proxy.updated_at))).scalar_one()
        assert refreshed > original

    async def test_updated_at_is_not_a_database_trigger(self, session: AsyncSession) -> None:
        # Documents a real limitation rather than hiding it. `onupdate=func.now()`
        # is applied by SQLAlchemy, so a raw UPDATE -- psql, a bulk COPY, a
        # maintenance script -- does not touch it. Every writer in this platform
        # goes through SQLAlchemy, so a trigger would be dead weight that also
        # surprises ad-hoc fixes by stamping them.
        await insert(session, make_proxy())
        await session.commit()
        original = (await session.execute(select(Proxy.updated_at))).scalar_one()

        await session.execute(text("UPDATE proxies SET is_active = false"))
        await session.commit()
        assert (await session.execute(select(Proxy.updated_at))).scalar_one() == original

    async def test_created_at_does_not_move_on_update(self, session: AsyncSession) -> None:
        await insert(session, make_proxy())
        await session.commit()
        original = (await session.execute(select(Proxy.created_at))).scalar_one()

        await session.execute(text("UPDATE proxies SET is_active = false"))
        await session.commit()
        assert (await session.execute(select(Proxy.created_at))).scalar_one() == original


class TestDiscoveryProvenance:
    async def test_several_sources_for_one_proxy(self, session: AsyncSession) -> None:
        await insert(session, make_proxy())
        await session.flush()
        proxy_id = (await session.execute(select(Proxy.id))).scalar_one()
        await insert(
            session,
            ProxyDiscovery(
                proxy_id=proxy_id, source_type=SourceType.TELEGRAM_CHANNEL, source_name="@a"
            ),
            ProxyDiscovery(
                proxy_id=proxy_id, source_type=SourceType.TELEGRAM_CHANNEL, source_name="@b"
            ),
            ProxyDiscovery(proxy_id=proxy_id, source_type=SourceType.MANUAL, source_name="seed"),
        )
        await session.commit()
        assert (await session.execute(select(func.count(ProxyDiscovery.id)))).scalar_one() == 3

    async def test_source_url_may_be_null(self, session: AsyncSession) -> None:
        # A manual import has no URL.
        await insert(session, make_proxy())
        await session.flush()
        proxy_id = (await session.execute(select(Proxy.id))).scalar_one()
        await insert(
            session,
            ProxyDiscovery(proxy_id=proxy_id, source_type=SourceType.MANUAL, source_name="seed"),
        )
        await session.commit()
        assert (await session.execute(select(ProxyDiscovery.source_url))).scalar_one() is None

    async def test_blank_source_name_is_rejected(self, session: AsyncSession) -> None:
        await insert(session, make_proxy())
        await session.flush()
        proxy_id = (await session.execute(select(Proxy.id))).scalar_one()
        error = await insert_expect_rejected(
            session,
            ProxyDiscovery(proxy_id=proxy_id, source_type=SourceType.MANUAL, source_name=""),
        )
        assert "ck_proxy_discoveries_source_name_not_blank" in error

    async def test_discovered_at_is_separate_from_created_at(self, session: AsyncSession) -> None:
        # "seen in a channel" and "row inserted by us" are unrelated facts.
        await insert(session, make_proxy())
        await session.flush()
        proxy_id = (await session.execute(select(Proxy.id))).scalar_one()
        seen = datetime(2020, 1, 1, tzinfo=UTC)
        await insert(
            session,
            ProxyDiscovery(
                proxy_id=proxy_id,
                source_type=SourceType.RAW_TEXT,
                source_name="dump",
                discovered_at=seen,
            ),
        )
        await session.commit()

        loaded = (await session.execute(select(ProxyDiscovery))).scalar_one()
        assert loaded.discovered_at == seen
        assert loaded.created_at > seen
