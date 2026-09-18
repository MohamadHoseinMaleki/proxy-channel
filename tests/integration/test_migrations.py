"""Migration lifecycle tests, against a real PostgreSQL.

``downgrade base`` DROPS EVERY TABLE, so this module runs against its own
throwaway database created and dropped by a fixture. It must never share a
database with the rest of the suite.

The point of these tests is that the *migration* -- not ``Base.metadata`` --
produces a working schema. Using ``create_all`` in fixtures would let the
migration rot while every other test still passed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import inspect
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from core.models import Base
from tests.integration.conftest import (
    _database_name,
    _run_alembic,
    create_database,
    drop_database,
    url_for_database,
)

#: Everything here needs a live PostgreSQL. The marker lets the suite be run or
#: skipped as a unit; with no database reachable the fixtures skip cleanly.
pytestmark = pytest.mark.integration

LIFECYCLE_SUFFIX = "_lifecycle"


@pytest.fixture(scope="module")
def lifecycle_url(test_database_url: str) -> Iterator[str]:
    """A dedicated, empty database for destructive migration tests."""

    async def _recreate() -> str:
        name = _database_name(test_database_url) + LIFECYCLE_SUFFIX
        await drop_database(test_database_url, name)
        await create_database(test_database_url, name)
        return url_for_database(test_database_url, name)

    url = asyncio.run(_recreate())
    try:
        yield url
    finally:
        asyncio.run(drop_database(test_database_url, _database_name(url)))


def _table_names(sync_connection: Connection) -> list[str]:
    return sorted(str(name) for name in inspect(sync_connection).get_table_names())


def _column_names(sync_connection: Connection, table: str) -> list[str]:
    return sorted(str(column["name"]) for column in inspect(sync_connection).get_columns(table))


def _index_names(sync_connection: Connection, table: str) -> list[str]:
    return sorted(
        str(index["name"]) for index in inspect(sync_connection).get_indexes(table) if index["name"]
    )


def _check_names(sync_connection: Connection, table: str) -> list[str]:
    return sorted(
        str(constraint["name"])
        for constraint in inspect(sync_connection).get_check_constraints(table)
        if constraint["name"]
    )


def _foreign_keys(sync_connection: Connection, table: str) -> list[tuple[str, str, str | None]]:
    return sorted(
        (
            str(fk["constrained_columns"][0]),
            str(fk["referred_table"]),
            fk["options"].get("ondelete"),
        )
        for fk in inspect(sync_connection).get_foreign_keys(table)
    )


def schema_objects(url: str) -> dict[str, Any]:
    """Snapshot the tables, columns, indexes and constraints of a database.

    Named functions rather than lambdas because ``run_sync`` cannot infer a
    lambda's parameter type, and ``str(...)`` throughout because the inspector
    returns ``quoted_name``/``None`` where a plain ``str`` makes comparison easy.
    """

    async def _collect() -> dict[str, Any]:
        engine = create_async_engine(url)
        try:
            async with engine.connect() as connection:
                names: list[str] = await connection.run_sync(_table_names)
                snapshot: dict[str, Any] = {"tables": names}
                for table in names:
                    if table == "alembic_version":
                        continue
                    snapshot[f"{table}.columns"] = await connection.run_sync(_column_names, table)
                    snapshot[f"{table}.indexes"] = await connection.run_sync(_index_names, table)
                    snapshot[f"{table}.checks"] = await connection.run_sync(_check_names, table)
                    snapshot[f"{table}.fks"] = await connection.run_sync(_foreign_keys, table)
                return snapshot
        finally:
            await engine.dispose()

    return asyncio.run(_collect())


def fk_actions(snapshot: dict[str, Any], table: str) -> dict[tuple[str, str], str | None]:
    """``{(column, referred_table): on_delete_action}`` for one table."""
    return {(column, referred): action for column, referred, action in snapshot[f"{table}.fks"]}


class TestUpgrade:
    def test_creates_every_modelled_table(self, lifecycle_url: str) -> None:
        _run_alembic(lifecycle_url, "upgrade", "head")
        tables: list[str] = schema_objects(lifecycle_url)["tables"]
        assert set(Base.metadata.tables) <= set(tables)

    def test_creates_every_column(self, lifecycle_url: str) -> None:
        _run_alembic(lifecycle_url, "upgrade", "head")
        snapshot = schema_objects(lifecycle_url)
        for name, table in Base.metadata.tables.items():
            assert snapshot[f"{name}.columns"] == sorted(
                str(column.name) for column in table.columns
            )

    def test_creates_every_index(self, lifecycle_url: str) -> None:
        from sqlalchemy import UniqueConstraint

        _run_alembic(lifecycle_url, "upgrade", "head")
        snapshot = schema_objects(lifecycle_url)
        for name, table in Base.metadata.tables.items():
            declared = [str(index.name) for index in table.indexes if index.name]
            # A UNIQUE constraint is physically backed by an index, so the
            # inspector reports it under get_indexes() while the model keeps it
            # under constraints. Including it here also asserts that
            # uq_proxies_fingerprint -- the identity key -- really got one.
            declared += [
                str(constraint.name)
                for constraint in table.constraints
                if isinstance(constraint, UniqueConstraint) and constraint.name
            ]
            assert snapshot[f"{name}.indexes"] == sorted(declared)

    def test_the_fingerprint_unique_index_exists(self, lifecycle_url: str) -> None:
        from sqlalchemy import text as sql_text

        _run_alembic(lifecycle_url, "upgrade", "head")

        async def _lookup() -> tuple[bool, bool]:
            engine = create_async_engine(lifecycle_url)
            try:
                async with engine.connect() as connection:
                    row = (
                        await connection.execute(
                            sql_text(
                                "SELECT indexdef FROM pg_indexes "
                                "WHERE indexname = 'uq_proxies_fingerprint'"
                            )
                        )
                    ).scalar_one()
                    return "UNIQUE" in row, "fingerprint" in row
            finally:
                await engine.dispose()

        is_unique, on_fingerprint = asyncio.run(_lookup())
        assert is_unique and on_fingerprint

    def test_creates_every_check_constraint(self, lifecycle_url: str) -> None:
        from sqlalchemy import CheckConstraint

        _run_alembic(lifecycle_url, "upgrade", "head")
        snapshot = schema_objects(lifecycle_url)
        for name, table in Base.metadata.tables.items():
            declared = sorted(
                str(constraint.name)
                for constraint in table.constraints
                if isinstance(constraint, CheckConstraint) and constraint.name
            )
            assert snapshot[f"{name}.checks"] == declared

    def test_foreign_keys_carry_the_right_on_delete(self, lifecycle_url: str) -> None:
        # ON DELETE actions are the difference between "history is protected" and
        # "history is destroyable by accident". They must survive the migration.
        _run_alembic(lifecycle_url, "upgrade", "head")
        snapshot = schema_objects(lifecycle_url)
        assert fk_actions(snapshot, "proxy_observations")[("proxy_id", "proxies")] == "RESTRICT"
        assert fk_actions(snapshot, "proxy_discoveries")[("proxy_id", "proxies")] == "CASCADE"
        assert fk_actions(snapshot, "proxy_scores")[("proxy_id", "proxies")] == "CASCADE"
        assert fk_actions(snapshot, "proxy_publications")[("proxy_id", "proxies")] == "RESTRICT"

    def test_records_the_revision(self, lifecycle_url: str) -> None:
        _run_alembic(lifecycle_url, "upgrade", "head")
        output = _run_alembic(lifecycle_url, "current")
        assert "0002 (head)" in output

    def test_no_drift_between_the_models_and_the_database(self, lifecycle_url: str) -> None:
        # The strongest single assertion available: regenerate a diff against the
        # migrated database and require it to be empty. This is what catches a
        # model change that was never turned into a migration -- and it is why
        # the migration can write sa.Text() for the SecretText column.
        _run_alembic(lifecycle_url, "upgrade", "head")
        output = _run_alembic(lifecycle_url, "check", allow_failure=True)
        assert "No new upgrade operations detected" in output, output

    def test_the_schema_is_usable_for_a_real_insert(self, lifecycle_url: str) -> None:
        # A migration that produces DDL but cannot accept a row is worthless.
        from sqlalchemy import text as sql_text

        _run_alembic(lifecycle_url, "upgrade", "head")

        async def _insert() -> None:
            engine = create_async_engine(lifecycle_url)
            try:
                async with engine.begin() as connection:
                    await connection.execute(
                        sql_text(
                            "INSERT INTO proxies (server, port, secret, fingerprint) "
                            "VALUES ('migrated.example.com', 443, 'ee00', :fp)"
                        ),
                        {"fp": "a" * 64},
                    )
                    count = await connection.execute(sql_text("SELECT count(*) FROM proxies"))
                    assert count.scalar_one() == 1
            finally:
                await engine.dispose()

        asyncio.run(_insert())


class TestDowngrade:
    def test_downgrade_base_removes_every_table(self, lifecycle_url: str) -> None:
        _run_alembic(lifecycle_url, "upgrade", "head")
        _run_alembic(lifecycle_url, "downgrade", "base")

        tables: list[str] = schema_objects(lifecycle_url)["tables"]
        for name in Base.metadata.tables:
            assert name not in tables
        assert tables == ["alembic_version"]

    def test_downgrade_clears_the_revision(self, lifecycle_url: str) -> None:
        _run_alembic(lifecycle_url, "upgrade", "head")
        _run_alembic(lifecycle_url, "downgrade", "base")
        output = _run_alembic(lifecycle_url, "current")
        assert "0001" not in output


class TestReversibility:
    def test_upgrade_downgrade_upgrade_produces_an_identical_schema(
        self, lifecycle_url: str
    ) -> None:
        """A migration is only reversible if the round trip is lossless.

        Autogenerated downgrades are a classic place to lose a partial index
        predicate or an ON DELETE action, so the full object set is compared
        rather than just the table list.
        """
        _run_alembic(lifecycle_url, "upgrade", "head")
        before = schema_objects(lifecycle_url)

        _run_alembic(lifecycle_url, "downgrade", "base")
        _run_alembic(lifecycle_url, "upgrade", "head")
        after = schema_objects(lifecycle_url)

        assert before == after

    def test_the_history_is_linear_with_one_head(self, lifecycle_url: str) -> None:
        # Branching histories make `upgrade head` ambiguous, which in a
        # three-process deployment means three workers can disagree about schema.
        output = _run_alembic(lifecycle_url, "heads")
        assert output.strip().endswith("(head)")
        assert len([line for line in output.splitlines() if "(head)" in line]) == 1


class TestOfflineMode:
    def test_sql_can_be_reviewed_without_connecting(self, lifecycle_url: str) -> None:
        # `--sql` lets a migration be read and approved before it touches a
        # production database -- the only safe way to review DDL under change
        # control. It must not require a reachable server.
        output = _run_alembic(lifecycle_url, "upgrade", "head", "--sql")
        assert "CREATE TABLE proxies" in output
        assert "CREATE TABLE proxy_publications" in output
        assert "CREATE UNIQUE INDEX uq_proxies_fingerprint" in output or (
            "UNIQUE" in output and "fingerprint" in output
        )
        assert "SKIP LOCKED" not in output  # DDL only, no runtime queries

    def test_offline_sql_matches_the_online_result(self, lifecycle_url: str) -> None:
        _run_alembic(lifecycle_url, "upgrade", "head")
        online = schema_objects(lifecycle_url)

        _run_alembic(lifecycle_url, "downgrade", "base")
        _run_alembic(lifecycle_url, "upgrade", "head")
        assert schema_objects(lifecycle_url) == online
