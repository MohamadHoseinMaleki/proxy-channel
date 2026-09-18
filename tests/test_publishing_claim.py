"""Unit tests for publication claim SQL. No database."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import Update

from modules.publishing.backoff import DEFAULT_LEASE_SECONDS
from modules.publishing.claim import claim_due_publications, recover_stale_publications

DIALECT = postgresql.dialect()
PINNED = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)
CHANNEL = "@proxy_channel"


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

    def all(self) -> list[Any]:
        return self._rows


class _StubSession:
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
        return re.sub(r"\s+", " ", str(self.statement.compile(dialect=DIALECT)))

    def cte_body(self) -> str:
        sql = self.sql()
        return sql.split("WITH claim_candidates AS (")[1].split(") UPDATE")[0].strip()

    def update_body(self) -> str:
        return self.sql().split(") UPDATE")[1]

    def params(self) -> dict[str, Any]:
        compiled = self.statement.compile(dialect=DIALECT)
        return dict(compiled.params)


async def claim(**kwargs: Any) -> _StubSession:
    session = _StubSession(kwargs.pop("rows", None))
    await claim_due_publications(
        session,  # type: ignore[arg-type]
        channel_id=kwargs.pop("channel_id", CHANNEL),
        proxy_ids=kwargs.pop("proxy_ids", [1, 2, 3]),
        now=kwargs.pop("now", PINNED),
        **kwargs,
    )
    return session


class TestValidation:
    async def test_rejects_non_positive_limit(self) -> None:
        with pytest.raises(ValueError, match="limit must be positive"):
            await claim(limit=0)

    async def test_rejects_non_positive_lease(self) -> None:
        with pytest.raises(ValueError, match="lease_seconds must be positive"):
            await claim(lease_seconds=0)

    async def test_rejects_a_naive_clock(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            await claim(now=datetime(2026, 9, 18, 12, 0, 0))

    async def test_empty_proxy_ids_does_not_query(self) -> None:
        session = _StubSession()
        claimed = await claim_due_publications(
            session,  # type: ignore[arg-type]
            channel_id=CHANNEL,
            proxy_ids=[],
            now=PINNED,
        )
        assert claimed == []
        assert session.statements == []


class TestSqlShape:
    async def test_is_a_single_update_statement(self) -> None:
        session = await claim()
        assert isinstance(session.statement, Update)

    async def test_uses_a_cte_named_claim_candidates(self) -> None:
        assert "WITH claim_candidates AS" in (await claim()).sql()

    async def test_locks_with_for_update_skip_locked(self) -> None:
        sql = (await claim()).sql()
        assert "FOR UPDATE" in sql
        assert "SKIP LOCKED" in sql

    async def test_skip_locked_is_inside_the_cte_not_the_update(self) -> None:
        session = await claim()
        assert "SKIP LOCKED" in session.cte_body()
        assert "SKIP LOCKED" not in session.update_body()

    async def test_only_pending_rows_are_selected(self) -> None:
        session = await claim()
        assert "proxy_publications.status =" in session.sql()
        assert "pending" in session.params().values()

    async def test_treats_an_expired_lease_as_claimable(self) -> None:
        sql = (await claim()).sql()
        assert "proxy_publications.lease_until IS NULL" in sql
        assert "proxy_publications.lease_until <" in sql

    async def test_orders_by_due_time_then_id(self) -> None:
        sql = (await claim()).sql()
        assert "ORDER BY proxy_publications.next_attempt_at, proxy_publications.id" in sql

    async def test_returns_the_claimed_rows(self) -> None:
        assert "RETURNING" in (await claim()).sql()

    async def test_sets_sending_and_lease(self) -> None:
        session = await claim(lease_seconds=90)
        sql = session.sql()
        assert "status" in sql
        assert session.params()["lease_until"] == PINNED + timedelta(seconds=90)
        assert session.params()["last_attempt_at"] == PINNED

    async def test_default_lease_matches_the_constant(self) -> None:
        session = await claim()
        assert session.params()["lease_until"] == PINNED + timedelta(seconds=DEFAULT_LEASE_SECONDS)

    async def test_no_string_interpolation_of_caller_values(self) -> None:
        session = await claim(limit=7, lease_seconds=42)
        sql = session.sql()
        assert "LIMIT 7" not in sql
        assert "42" not in sql


class TestRecoverSql:
    async def test_only_expired_sending_rows(self) -> None:
        session = _StubSession([])
        await recover_stale_publications(session, channel_id=CHANNEL, now=PINNED)  # type: ignore[arg-type]
        compiled = session.statement.compile(dialect=DIALECT)
        sql = re.sub(r"\s+", " ", str(compiled))
        params = dict(compiled.params)
        assert "proxy_publications.status =" in sql
        assert "sending" in params.values()
        assert "lease_until" in sql
        assert "pending" in params.values()

    async def test_recover_rejects_naive_clock(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            await recover_stale_publications(
                _StubSession(),  # type: ignore[arg-type]
                channel_id=CHANNEL,
                now=datetime(2026, 9, 18),
            )
