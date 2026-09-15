"""Unit tests for :mod:`modules.scheduling` -- the ``SKIP LOCKED`` claim query.

The claim query is the mechanism that lets N tester processes partition work
without a queue, so its SQL shape is worth asserting precisely. These tests
compile the statement against the PostgreSQL dialect through a stub session: no
database, but the SQL that would actually be sent is inspected. Concurrency
behaviour against a real server is covered in ``tests/integration``.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import Update

from core.models import DEFAULT_LEASE_SECONDS, Proxy
from modules.scheduling import claim_due_proxies

DIALECT = postgresql.dialect()


class _Scalars:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def unique(self) -> _Scalars:
        return self

    def all(self) -> list[Any]:
        return self._rows


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _Scalars:
        return _Scalars(self._rows)


class _StubSession:
    """Records the statement handed to ``execute`` and returns canned rows."""

    def __init__(self, rows: list[Any] | None = None) -> None:
        self.rows = rows or []
        self.statements: list[Any] = []

    async def execute(self, statement: Any, *_args: Any, **_kwargs: Any) -> _Result:
        self.statements.append(statement)
        return _Result(self.rows)

    @property
    def statement(self) -> Any:
        assert len(self.statements) == 1, f"expected 1 execute, got {len(self.statements)}"
        return self.statements[0]

    def sql(self) -> str:
        """Rendered SQL with runs of whitespace collapsed.

        SQLAlchemy emits newlines inside ``WITH ... AS\n(``, so assertions match
        on tokens rather than on exact formatting.
        """
        return re.sub(r"\s+", " ", str(self.statement.compile(dialect=DIALECT)))

    def cte_body(self) -> str:
        """The CTE's SELECT, with whitespace collapsed."""
        sql = self.sql()
        return sql.split("WITH claim_candidates AS (")[1].split(") UPDATE")[0].strip()

    def update_body(self) -> str:
        """Everything after the CTE."""
        return self.sql().split(") UPDATE")[1]

    def params(self) -> dict[str, Any]:
        compiled = self.statement.compile(dialect=DIALECT)
        return dict(compiled.params)


class _RecordingScalarsSession(_StubSession):
    """Records whether ``.unique()`` was called on the result scalars.

    A plain list stub cannot replicate SQLAlchemy's identity-based uniquing, so
    this asserts the *call* rather than a deduplication outcome.
    """

    def __init__(self) -> None:
        super().__init__([])
        self.unique_called = False

    async def execute(self, statement: Any, *_args: Any, **_kwargs: Any) -> Any:
        self.statements.append(statement)
        outer = self

        class _Unique:
            def unique(self) -> _Unique:
                outer.unique_called = True
                return self

            def all(self) -> list[Any]:
                return []

        class _Result:
            def scalars(self) -> _Unique:
                return _Unique()

        return _Result()


PINNED = datetime(2026, 9, 15, 12, 0, 0, tzinfo=UTC)


async def claim(**kwargs: Any) -> _StubSession:
    session = _StubSession(kwargs.pop("rows", None))
    await claim_due_proxies(session, now=kwargs.pop("now", PINNED), **kwargs)  # type: ignore[arg-type]
    return session


class TestValidation:
    @pytest.mark.parametrize("limit", [0, -1, -100])
    async def test_rejects_non_positive_limit(self, limit: int) -> None:
        with pytest.raises(ValueError, match="limit must be positive"):
            await claim(limit=limit)

    @pytest.mark.parametrize("lease", [0, -1.0, -300])
    async def test_rejects_non_positive_lease(self, lease: float) -> None:
        with pytest.raises(ValueError, match="lease_seconds must be positive"):
            await claim(lease_seconds=lease)

    async def test_rejects_a_naive_clock(self) -> None:
        # A naive datetime would compare against TIMESTAMPTZ unpredictably and
        # asyncpg rejects it outright.
        with pytest.raises(ValueError, match="timezone-aware"):
            await claim(now=datetime(2026, 9, 15, 12, 0, 0))

    async def test_accepts_a_non_utc_timezone(self) -> None:
        from zoneinfo import ZoneInfo

        tehran = PINNED.astimezone(ZoneInfo("Asia/Tehran"))
        assert tehran.tzinfo is not None
        session = await claim(now=tehran)
        # Same instant, so the same lease boundary.
        assert session.params()["last_test_started_at"] == PINNED

    async def test_validation_happens_before_any_query(self) -> None:
        session = _StubSession()
        with pytest.raises(ValueError, match="limit must be positive"):
            await claim_due_proxies(session, limit=0, now=PINNED)  # type: ignore[arg-type]
        assert session.statements == []


class TestSqlShape:
    async def test_is_a_single_update_statement(self) -> None:
        # One round trip: select-and-lock plus lease update must not be two
        # queries, or another worker can claim the same row in between.
        session = await claim()
        assert isinstance(session.statement, Update)

    async def test_uses_a_cte_named_claim_candidates(self) -> None:
        sql = (await claim()).sql()
        assert "WITH claim_candidates AS" in sql

    async def test_selects_only_the_id_in_the_cte(self) -> None:
        # Locking a row needs only its PK; selecting wide columns would make the
        # CTE carry data the UPDATE already returns.
        cte = (await claim()).cte_body()
        assert cte.startswith("SELECT proxies.id AS id FROM proxies")
        assert "proxies.server" not in cte
        assert "proxies.secret" not in cte

    async def test_locks_with_for_update_skip_locked(self) -> None:
        # The whole point. Plain FOR UPDATE would make a second worker *block*
        # for the duration of the first one's MTProto handshake instead of
        # taking different rows.
        sql = (await claim()).sql()
        assert "FOR UPDATE" in sql
        assert "SKIP LOCKED" in sql

    async def test_skip_locked_is_inside_the_cte_not_the_update(self) -> None:
        # UPDATE has no SKIP LOCKED; the locking read must be the CTE's SELECT.
        session = await claim()
        assert "SKIP LOCKED" in session.cte_body()
        assert "SKIP LOCKED" not in session.update_body()

    async def test_update_joins_the_cte_on_id(self) -> None:
        sql = (await claim()).sql()
        assert "UPDATE proxies SET" in sql
        assert "FROM claim_candidates" in sql
        assert "proxies.id = claim_candidates.id" in sql

    async def test_returns_the_claimed_rows(self) -> None:
        # Without RETURNING a claim would need a second SELECT to learn what it
        # got, reopening the race the CTE was written to close.
        sql = (await claim()).sql()
        assert "RETURNING" in sql
        for column in ("proxies.id", "proxies.server", "proxies.port", "proxies.secret"):
            assert column in sql

    async def test_orders_by_due_time_then_id(self) -> None:
        # The secondary `id` tiebreak makes the ordering total, so two workers
        # cannot both see the same "first" row when timestamps are equal.
        sql = (await claim()).sql()
        assert "ORDER BY proxies.next_test_at, proxies.id" in sql

    async def test_is_bound_to_the_claim_index(self) -> None:
        # ix_proxies_due is (next_test_at) WHERE is_active; the predicate and the
        # ordering must match it or the index is decorative.
        sql = (await claim()).sql()
        assert "proxies.is_active IS true" in sql
        assert "proxies.next_test_at <=" in sql

    async def test_treats_an_expired_lease_as_claimable(self) -> None:
        # This is the crash-recovery path: a worker killed with -9 never clears
        # its claim, so the lease must expire on its own.
        sql = (await claim()).sql()
        assert "proxies.test_lock_until IS NULL" in sql
        assert "proxies.test_lock_until <" in sql
        assert "OR" in sql


class TestLeaseSemantics:
    async def test_sets_the_lease_to_now_plus_lease_seconds(self) -> None:
        session = await claim(lease_seconds=120)
        assert session.params()["test_lock_until"] == PINNED + timedelta(seconds=120)

    async def test_default_lease_matches_the_constant(self) -> None:
        session = await claim()
        assert session.params()["test_lock_until"] == PINNED + timedelta(
            seconds=DEFAULT_LEASE_SECONDS
        )

    async def test_records_the_start_time(self) -> None:
        assert (await claim()).params()["last_test_started_at"] == PINNED

    async def test_increments_test_attempts_server_side(self) -> None:
        # A server-side increment, not a read-modify-write in Python: two workers
        # claiming the same row over time would otherwise lose an update, and the
        # counter is the trail left by a proxy that keeps killing workers.
        session = await claim()
        assert "test_attempts=(proxies.test_attempts + %(test_attempts_1)s)" in session.sql()
        assert session.params()["test_attempts_1"] == 1
        # The absolute value is never sent, which is what makes it atomic.
        assert "test_attempts=" in session.sql()
        assert "test_attempts" not in {key for key in session.params() if key == "test_attempts"}

    async def test_limit_is_a_bound_parameter(self) -> None:
        session = await claim(limit=7)
        assert "LIMIT %(param_1)s" in session.sql()
        assert session.params()["param_1"] == 7

    async def test_lease_until_is_a_bound_parameter(self) -> None:
        session = await claim(lease_seconds=42)
        assert "test_lock_until" in session.params()

    async def test_no_string_interpolation_of_caller_values(self) -> None:
        # A guard against the statement ever being built by concatenation.
        session = await claim(limit=25, lease_seconds=300)
        sql = session.sql()
        assert "300" not in sql  # lease length is a parameter, not a literal
        assert "LIMIT 25" not in sql
        assert "LIMIT %(param_1)s" in sql
        # Every caller-supplied value travels as a bound parameter.
        assert session.params()["param_1"] == 25
        assert "test_lock_until" in session.params()

    async def test_uses_populate_existing(self) -> None:
        # Rows may already be in the session identity map from an earlier query;
        # without this they would keep stale lease values after the UPDATE.
        statement = (await claim()).statement
        assert statement._execution_options.get("populate_existing") is True


class TestReturnValue:
    async def test_returns_the_claimed_rows_in_order(self) -> None:
        first = Proxy(server="a.example", port=1, secret="ee" + "01" * 15, fingerprint="a" * 64)
        second = Proxy(server="b.example", port=2, secret="ee" + "02" * 15, fingerprint="b" * 64)
        session = _StubSession([first, second])
        claimed = await claim_due_proxies(session, now=PINNED, limit=2)  # type: ignore[arg-type]
        assert claimed == [first, second]

    async def test_returns_an_empty_list_when_nothing_is_due(self) -> None:
        session = _StubSession([])
        claimed = await claim_due_proxies(session, now=PINNED)  # type: ignore[arg-type]
        assert claimed == []

    async def test_uniquifies_the_result_rows(self) -> None:
        # `.unique()` is what makes a row that reappears (identity map, joined
        # eager load) count once. Assert the call, since a stub cannot replicate
        # SQLAlchemy's identity-based uniquing.
        session = _RecordingScalarsSession()
        await claim_due_proxies(session, now=PINNED)  # type: ignore[arg-type]
        assert session.unique_called is True

    async def test_returned_rows_are_sorted_by_due_time(self) -> None:
        """The Python-side half of the ordering guarantee.

        ``UPDATE ... FROM claim_candidates ... RETURNING`` does not preserve the
        CTE's ORDER BY -- PostgreSQL returns rows in join order, which tracks heap
        layout. Selection stays fair (the CTE decides *which* rows are leased), but
        the list must be sorted here or its order varies with table contents.
        """
        from core.models import utcnow as _utcnow

        base = _utcnow() - timedelta(hours=2)
        rows = [
            Proxy(
                server="newest.example",
                port=1,
                secret="ee" + "03" * 15,
                fingerprint="c" * 64,
                next_test_at=base + timedelta(minutes=90),
            ),
            Proxy(
                server="oldest.example",
                port=2,
                secret="ee" + "01" * 15,
                fingerprint="a" * 64,
                next_test_at=base,
            ),
            Proxy(
                server="middle.example",
                port=3,
                secret="ee" + "02" * 15,
                fingerprint="b" * 64,
                next_test_at=base + timedelta(minutes=45),
            ),
        ]
        session = _StubSession(rows)
        claimed = await claim_due_proxies(session, now=PINNED, limit=3)  # type: ignore[arg-type]
        assert [proxy.server for proxy in claimed] == [
            "oldest.example",
            "middle.example",
            "newest.example",
        ]

    async def test_equal_due_times_are_broken_by_id(self) -> None:
        # Mirrors the SQL tiebreak so the total ordering is deterministic.
        from core.models import utcnow as _utcnow

        moment = _utcnow()
        rows = [
            Proxy(
                server="b.example",
                port=1,
                secret="ee" + "02" * 15,
                fingerprint="b" * 64,
                next_test_at=moment,
                id=2,
            ),
            Proxy(
                server="a.example",
                port=2,
                secret="ee" + "01" * 15,
                fingerprint="a" * 64,
                next_test_at=moment,
                id=1,
            ),
        ]
        session = _StubSession(rows)
        claimed = await claim_due_proxies(session, now=PINNED, limit=2)  # type: ignore[arg-type]
        assert [proxy.id for proxy in claimed] == [1, 2]

    async def test_defaults_are_usable_without_arguments(self) -> None:
        # `now` is injectable for tests, but production callers pass nothing.
        session = _StubSession([])
        await claim_due_proxies(session)  # type: ignore[arg-type]
        params = dict(session.statement.compile(dialect=DIALECT).params)
        lease = params["test_lock_until"]
        started = params["last_test_started_at"]
        assert lease - started == timedelta(seconds=DEFAULT_LEASE_SECONDS)
        assert started.tzinfo is not None

    async def test_logs_only_ids_never_the_secret(self, capsys: pytest.CaptureFixture[str]) -> None:
        # The claim query does RETURNING the secret (the tester needs it to
        # connect), so the protection is that the module logs ids only.
        from core.logger import configure_logging
        from tests.conftest import make_settings

        configure_logging(make_settings(log_format="json", log_level="DEBUG"), force=True)
        secret = "ee" + "01" * 15
        session = _StubSession(
            [Proxy(server="a.example", port=1, secret=secret, fingerprint="a" * 64)]
        )
        await claim_due_proxies(session, now=PINNED)  # type: ignore[arg-type]

        emitted = capsys.readouterr()
        assert "proxies_claimed" in emitted.err + emitted.out
        assert secret not in emitted.err + emitted.out
