"""Unit tests for :mod:`core.models` -- schema shape, compiled DDL, secret type.

No PostgreSQL is required: these assert on the *declarations* by compiling DDL
with the PostgreSQL dialect. That catches naming-convention regressions, missing
constraints and wrong ON DELETE actions without a database, and runs in
milliseconds. The behaviour those declarations produce at runtime is covered by
``tests/integration``.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from core.identity import ProxySecret
from core.models import (
    ERROR_MESSAGE_MAX_LENGTH,
    SCORING_VERSION_V1,
    TESTER_VERSION_DEFAULT,
    Base,
    ErrorCategory,
    Proxy,
    ProxyDiscovery,
    ProxyObservation,
    ProxyScore,
    SecretText,
    SourceType,
    masked_secret_text,
    utcnow,
)
from tests.conftest import string_length, table_of

DIALECT = postgresql.dialect()


def ddl(table: object) -> str:
    """Render a table's ``CREATE TABLE`` as PostgreSQL would execute it."""
    return str(CreateTable(table).compile(dialect=DIALECT))  # type: ignore[arg-type]


def index_ddl(index: object) -> str:
    return str(CreateIndex(index).compile(dialect=DIALECT))  # type: ignore[arg-type]


def constraint_names(table: object) -> set[str]:
    return {c.name for c in table.constraints if c.name}  # type: ignore[attr-defined]


def index_by_name(table: object, name: str) -> object:
    for index in table.indexes:  # type: ignore[attr-defined]
        if index.name == name:
            return index
    msg = f"index {name!r} not found on {table.name!r}"  # type: ignore[attr-defined]
    raise AssertionError(msg)


class TestMetadata:
    def test_expected_tables_exist(self) -> None:
        assert set(Base.metadata.tables) == {
            "proxies",
            "proxy_discoveries",
            "proxy_observations",
            "proxy_scores",
        }

    def test_no_module_level_engine_is_imported(self) -> None:
        # Four separate processes must each build their own engine. A singleton
        # here would be a latent cross-loop binding bug.
        import core.models as models

        assert not any(
            name.startswith("engine") or name.startswith("Session")
            for name in vars(models)
            if not name.startswith("__")
        )

    def test_metadata_has_a_naming_convention(self) -> None:
        convention = Base.metadata.naming_convention
        for key in ("ix", "uq", "ck", "fk", "pk"):
            assert key in convention

    def test_every_constraint_is_explicitly_named(self) -> None:
        # Alembic can only drop or alter a constraint by name. An unnamed one
        # means PostgreSQL invents a name and autogenerate emits unusable diffs.
        for table in Base.metadata.tables.values():
            for constraint in table.constraints:
                # PrimaryKeyConstraint on a table always gets a name via the
                # convention; check it is not None.
                assert constraint.name, f"unnamed {type(constraint).__name__} on {table.name}"

    def test_constraint_names_follow_the_convention(self) -> None:
        names = constraint_names(Proxy.__table__)
        assert "pk_proxies" in names
        assert "uq_proxies_fingerprint" in names
        assert "ck_proxies_port_range" in names
        assert "fk_proxy_observations_proxy_id_proxies" in constraint_names(
            ProxyObservation.__table__
        )


class TestProxyColumns:
    def test_required_columns(self) -> None:
        expected = {
            "id",
            "protocol",
            "server",
            "port",
            "secret",
            "fingerprint",
            "is_active",
            "created_at",
            "updated_at",
            "first_seen_at",
            "last_seen_at",
            "last_success_at",
            "last_failure_at",
            "next_test_at",
            "test_lock_until",
            "test_attempts",
            "last_test_started_at",
            "last_test_finished_at",
            "last_error_category",
        }
        assert set(Proxy.__table__.columns.keys()) == expected

    def test_uses_last_test_finished_at_not_last_tested_at(self) -> None:
        # One canonical name for the fact, not two aliases that can disagree.
        assert "last_test_finished_at" in Proxy.__table__.columns
        assert "last_tested_at" not in Proxy.__table__.columns

    def test_fingerprint_is_unique_and_64_chars(self) -> None:
        column = Proxy.__table__.c.fingerprint
        assert column.unique is True
        assert column.nullable is False
        assert isinstance(column.type, String)
        assert string_length(Proxy.__table__.c.fingerprint) == 64

    def test_secret_uses_the_secret_text_decorator(self) -> None:
        assert isinstance(Proxy.__table__.c.secret.type, SecretText)
        assert Proxy.__table__.c.secret.nullable is False

    def test_next_test_at_is_not_null_with_a_now_default(self) -> None:
        # NOT NULL is what lets ix_proxies_due skip a NULLS FIRST variant.
        column = Proxy.__table__.c.next_test_at
        assert column.nullable is False
        assert column.server_default is not None
        assert "now()" in str(column.server_default.arg)

    def test_every_datetime_column_is_timestamptz(self) -> None:
        # A naive DATETIME would make observations from differently configured
        # hosts incomparable, and asyncpg rejects naive values for TIMESTAMPTZ.
        from sqlalchemy import DateTime

        for table in Base.metadata.tables.values():
            for column in table.columns:
                if isinstance(column.type, DateTime):
                    assert column.type.timezone is True, f"{table.name}.{column.name}"

    def test_id_is_bigint(self) -> None:
        # Observations grow without bound; INT4 would exhaust at ~2.1e9 rows.
        from sqlalchemy import BigInteger

        assert isinstance(Proxy.__table__.c.id.type, BigInteger)
        assert isinstance(ProxyObservation.__table__.c.id.type, BigInteger)

    def test_is_active_defaults_true(self) -> None:
        column = Proxy.__table__.c.is_active
        assert column.default.arg is True
        assert str(column.server_default.arg).lower() == "true"

    def test_test_attempts_defaults_to_zero(self) -> None:
        assert Proxy.__table__.c.test_attempts.default.arg == 0
        assert str(Proxy.__table__.c.test_attempts.server_default.arg) == "0"

    def test_protocol_defaults_to_mtproto(self) -> None:
        assert Proxy.__table__.c.protocol.default.arg == "mtproto"


class TestProxyDDL:
    def test_creates_the_expected_constraints(self) -> None:
        rendered = ddl(Proxy.__table__)
        assert "port >= 1 AND port <= 65535" in rendered
        assert "test_attempts >= 0" in rendered
        assert "char_length(server) > 0" in rendered
        assert "UNIQUE (fingerprint)" in rendered

    def test_secret_length_is_bounded(self) -> None:
        from core.identity import SECRET_MAX_LENGTH

        rendered = ddl(Proxy.__table__)
        assert f"char_length(secret) <= {SECRET_MAX_LENGTH}" in rendered
        assert "char_length(secret) > 0" in rendered

    def test_protocol_has_no_check_constraint(self) -> None:
        # Deliberate: a closed allowlist would make every future protocol a
        # migration. The fingerprint already covers protocol correctness.
        rendered = ddl(Proxy.__table__)
        assert "protocol" in rendered
        assert not re.search(r"CHECK\s*\([^)]*protocol", rendered, re.IGNORECASE)

    def test_updated_at_refreshes_on_update(self) -> None:
        assert Proxy.__table__.c.updated_at.onupdate is not None


class TestClaimIndex:
    def test_partial_index_on_next_test_at_where_is_active(self) -> None:
        index = index_by_name(Proxy.__table__, "ix_proxies_due")
        rendered = index_ddl(index)
        assert "ON proxies" in rendered
        assert "next_test_at" in rendered
        assert "WHERE is_active" in rendered

    def test_index_is_not_unique(self) -> None:
        index = index_by_name(Proxy.__table__, "ix_proxies_due")
        assert index.unique is not True  # type: ignore[attr-defined]

    def test_reporting_and_debug_indexes_exist(self) -> None:
        names = {i.name for i in table_of(Proxy).indexes}
        assert {"ix_proxies_due", "ix_proxies_last_success_at", "ix_proxies_server"} <= names

    def test_lease_predicate_is_not_part_of_any_index(self) -> None:
        # Partial index predicates must be IMMUTABLE and now() is only STABLE, so
        # `WHERE test_lock_until IS NULL OR test_lock_until < now()` cannot be
        # indexed. Assert it stayed out rather than shipping an index that
        # PostgreSQL would reject at CREATE time.
        for index in table_of(Proxy).indexes:
            rendered = index_ddl(index)
            assert "test_lock_until" not in rendered


class TestForeignKeys:
    @pytest.mark.parametrize(
        ("model", "expected"),
        [
            (ProxyDiscovery, "CASCADE"),
            (ProxyObservation, "RESTRICT"),
            (ProxyScore, "CASCADE"),
        ],
    )
    def test_on_delete_action(self, model: object, expected: str) -> None:
        table = model.__table__  # type: ignore[attr-defined]
        fks = [fk for fk in table.foreign_keys if fk.column.table.name == "proxies"]
        assert len(fks) == 1
        assert fks[0].ondelete == expected

    def test_observations_are_restrict_so_history_survives(self) -> None:
        # The single most consequential FK decision in the schema: measurement
        # history is the asset, and a proxy delete must not destroy it silently.
        rendered = ddl(ProxyObservation.__table__)
        assert "ON DELETE RESTRICT" in rendered

    def test_discoveries_cascade(self) -> None:
        assert "ON DELETE CASCADE" in ddl(ProxyDiscovery.__table__)

    def test_proxy_deletes_its_own_discoveries_and_scores_in_orm(self) -> None:
        # passive_deletes=True means the database does the work; the ORM cascade
        # keeps in-session semantics consistent.
        for attribute in ("discoveries", "scores"):
            relationship = Proxy.__mapper__.relationships[attribute]
            assert relationship.passive_deletes is True
            assert "delete" in relationship.cascade

    def test_observations_are_not_cascaded_in_the_orm(self) -> None:
        relationship = Proxy.__mapper__.relationships["observations"]
        assert "delete" not in relationship.cascade
        assert relationship.passive_deletes is True

    def test_all_relationships_refuse_lazy_loading(self) -> None:
        # lazy="raise": under asyncio an implicit lazy load is a hidden round
        # trip that fails outright. Callers must choose selectinload/joinedload.
        for model in (Proxy, ProxyDiscovery, ProxyObservation, ProxyScore):
            for relationship in model.__mapper__.relationships.values():
                assert relationship.lazy == "raise", f"{model.__name__}.{relationship.key}"


class TestObservationConstraints:
    def test_failure_requires_a_category(self) -> None:
        assert "success OR error_category IS NOT NULL" in ddl(ProxyObservation.__table__)

    def test_column_is_named_success_not_is_success(self) -> None:
        assert "success" in ProxyObservation.__table__.columns
        assert "is_success" not in ProxyObservation.__table__.columns

    @pytest.mark.parametrize("column", ["tcp_connect_ms", "mtproto_connect_ms", "total_latency_ms"])
    def test_latencies_are_float_and_bounded(self, column: str) -> None:
        from sqlalchemy import Float

        field = ProxyObservation.__table__.c[column]
        assert isinstance(field.type, Float)
        assert field.nullable is True
        assert f"{column} IS NULL OR {column} >= 0" in ddl(ProxyObservation.__table__)

    def test_error_message_is_bounded_in_the_database(self) -> None:
        rendered = ddl(ProxyObservation.__table__)
        assert f"char_length(error_message_safe) <= {ERROR_MESSAGE_MAX_LENGTH}" in rendered

    def test_error_category_is_varchar_not_a_native_enum(self) -> None:
        # A PostgreSQL enum makes every new category a migration, and the
        # taxonomy will evolve as Task 005 meets real Telethon behaviour.
        column = ProxyObservation.__table__.c.error_category
        assert isinstance(column.type, String)
        assert column.type.length == 32
        assert "CREATE TYPE" not in ddl(ProxyObservation.__table__)

    def test_tester_version_is_recorded(self) -> None:
        column = ProxyObservation.__table__.c.tester_version
        assert column.default.arg == TESTER_VERSION_DEFAULT
        assert column.nullable is False

    def test_success_index_is_partial_not_composite(self) -> None:
        # Deviation from the suggested (success, observed_at) composite: a
        # boolean leading column roughly doubles the index while serving exactly
        # the same query, since the filter value is a constant.
        index = index_by_name(
            ProxyObservation.__table__, "ix_proxy_observations_success_observed_at"
        )
        rendered = index_ddl(index)
        assert "WHERE success" in rendered
        # `success` appears in the predicate only, never in the key columns.
        key_part = rendered.split("(")[1].split(")")[0]
        assert "success" not in key_part
        assert "observed_at" in key_part

    def test_scoring_and_retention_indexes_exist(self) -> None:
        names = {i.name for i in table_of(ProxyObservation).indexes}
        assert {
            "ix_proxy_observations_proxy_id_observed_at",
            "ix_proxy_observations_observed_at",
            "ix_proxy_observations_success_observed_at",
        } <= names

    def test_composite_index_leads_with_proxy_id(self) -> None:
        index = index_by_name(
            ProxyObservation.__table__, "ix_proxy_observations_proxy_id_observed_at"
        )
        names = [c.name for c in index.columns]  # type: ignore[attr-defined]
        assert names == ["proxy_id", "observed_at"]


class TestScoreConstraints:
    def test_score_is_numeric_not_float(self) -> None:
        # Scores are ranked and compared; exact decimal semantics keep ordering
        # deterministic where float accumulation would not.
        column = ProxyScore.__table__.c.score
        assert isinstance(column.type, Numeric)
        assert column.type.precision == 6
        assert column.type.scale == 3
        assert "score >= 0 AND score <= 100" in ddl(ProxyScore.__table__)

    @pytest.mark.parametrize("window", ["1h", "6h", "24h"])
    def test_reliability_needs_samples(self, window: str) -> None:
        # Blocks the "1/1 == 100%" and "no data == 0%" traps at storage level.
        rendered = ddl(ProxyScore.__table__)
        assert f"sample_count_{window} > 0 OR reliability_{window} IS NULL" in rendered
        assert f"reliability_{window} >= 0 AND reliability_{window} <= 100" in rendered
        assert f"sample_count_{window} >= 0" in rendered

    def test_p95_must_be_at_least_p50(self) -> None:
        assert "latency_p95_ms >= latency_p50_ms" in ddl(ProxyScore.__table__)

    def test_scoring_version_is_mandatory(self) -> None:
        column = ProxyScore.__table__.c.scoring_version
        assert column.nullable is False
        assert column.default.arg == SCORING_VERSION_V1
        assert "char_length(scoring_version) > 0" in ddl(ProxyScore.__table__)

    def test_latest_score_index(self) -> None:
        index = index_by_name(ProxyScore.__table__, "ix_proxy_scores_proxy_id_calculated_at")
        names = [c.name for c in index.columns]  # type: ignore[attr-defined]
        assert names == ["proxy_id", "calculated_at"]

    def test_scores_are_append_only_snapshots(self) -> None:
        # No UNIQUE on proxy_id: this is history, not one authoritative current
        # value. A unique constraint here would make every rescore overwrite the
        # previous one and destroy the ability to explain a ranking change.
        assert not ProxyScore.__table__.c.proxy_id.unique
        assert not any(
            isinstance(c, UniqueConstraint) and [col.name for col in c.columns] == ["proxy_id"]
            for c in table_of(ProxyScore).constraints
        )

    def test_score_bounds_constants_match_the_constraint(self) -> None:
        from core.models import RELIABILITY_MAX, SCORE_MAX, SCORE_MIN

        assert (
            Decimal("0"),
            Decimal("100"),
            Decimal("100"),
        ) == (SCORE_MIN, SCORE_MAX, RELIABILITY_MAX)


class TestSecretText:
    def test_impl_is_text(self) -> None:
        # The class attribute is the type; the instance attribute is an instance
        # of it, because TypeDecorator.__init__ materialises `impl`.
        assert SecretText.impl is Text
        assert isinstance(SecretText().impl, Text)

    def test_renders_as_plain_text_in_ddl(self) -> None:
        # This is what makes the migration able to write sa.Text() and still
        # produce zero autogenerate drift.
        from sqlalchemy import Column, MetaData, Table

        table = Table("t", MetaData(), Column("secret", SecretText()))
        rendered = ddl(table)
        assert "secret TEXT" in rendered
        assert "SECRETTEXT" not in rendered.upper().replace("SECRET TEXT", "")

    def test_cache_ok_is_set(self) -> None:
        # Without it SQLAlchemy emits a warning and refuses to cache statement
        # compilations, which costs real throughput on the hot claim query.
        assert SecretText.cache_ok is True

    def test_binds_a_plain_string(self) -> None:
        secret = "ee" + "a1" * 15
        assert SecretText().process_bind_param(secret, DIALECT) == secret

    def test_binds_a_proxy_secret_to_its_plaintext(self) -> None:
        # The transport needs the real value to connect, so the column stores it.
        secret = ProxySecret("ee" + "a1" * 15)
        assert SecretText().process_bind_param(secret, DIALECT) == secret.reveal()

    def test_bind_strips_whitespace(self) -> None:
        assert SecretText().process_bind_param("  ee" + "a1" * 15 + " \n", DIALECT) == (
            "ee" + "a1" * 15
        )

    def test_bind_rejects_other_types(self) -> None:
        with pytest.raises(TypeError, match="must be str or ProxySecret"):
            SecretText().process_bind_param(1234, DIALECT)
        with pytest.raises(TypeError, match="must be str or ProxySecret"):
            SecretText().process_bind_param(b"\xee", DIALECT)

    def test_bind_passes_none_through(self) -> None:
        assert SecretText().process_bind_param(None, DIALECT) is None

    def test_result_is_wrapped_in_proxy_secret(self) -> None:
        secret = "ee" + "a1" * 15
        loaded = SecretText().process_result_value(secret, DIALECT)
        assert isinstance(loaded, ProxySecret)
        assert loaded.reveal() == secret

    def test_result_value_cannot_be_printed(self) -> None:
        secret = "ee" + "a1" * 15
        loaded = SecretText().process_result_value(secret, DIALECT)
        assert secret not in str(loaded)
        assert secret not in repr(loaded)
        assert secret not in f"{loaded}"

    def test_result_passes_none_through(self) -> None:
        assert SecretText().process_result_value(None, DIALECT) is None

    def test_round_trip(self) -> None:
        secret = "ee" + "c3" * 15
        stored = SecretText().process_bind_param(ProxySecret(secret), DIALECT)
        loaded = SecretText().process_result_value(stored, DIALECT)
        assert loaded == ProxySecret(secret)
        assert loaded.reveal() == secret


class TestEnums:
    def test_error_categories_are_stable_strings(self) -> None:
        for member in ErrorCategory:
            assert isinstance(member.value, str)
            assert member.value == member.name

    def test_wrong_secret_is_deliberately_absent(self) -> None:
        # MTProxy drops bad-secret payloads without RST or error, so a wrong
        # secret is indistinguishable from a blackholed endpoint. Claiming to
        # detect it would fabricate a diagnosis.
        assert "WRONG_SECRET" not in ErrorCategory.__members__
        assert all("SECRET" not in name for name in ErrorCategory.__members__)

    def test_expected_error_categories(self) -> None:
        assert set(ErrorCategory.__members__) == {
            "SUCCESS",
            "DNS_ERROR",
            "TCP_TIMEOUT",
            "TCP_REFUSED",
            "TCP_ERROR",
            "MT_PROTO_TIMEOUT",
            "PROTOCOL_ERROR",
            "TELEGRAM_RPC_ERROR",
            "API_AUTH_ERROR",
            "UNKNOWN_ERROR",
        }

    def test_error_category_values_fit_the_column(self) -> None:
        width = string_length(ProxyObservation.__table__.c.error_category)
        for member in ErrorCategory:
            assert len(member.value) <= width

    def test_source_types(self) -> None:
        assert set(SourceType.__members__) == {
            "TELEGRAM_CHANNEL",
            "HTTP_PAGE",
            "RAW_TEXT",
            "MANUAL",
            "UNKNOWN",
        }
        assert all(member.value == member.name.lower() for member in SourceType)

    def test_source_type_values_fit_the_column(self) -> None:
        width = string_length(ProxyDiscovery.__table__.c.source_type)
        for member in SourceType:
            assert len(member.value) <= width

    def test_enums_are_str_subclasses_for_easy_comparison(self) -> None:
        # Assigned through a `str` variable on purpose: mypy narrows the literal
        # member type and would call the comparison non-overlapping, while at
        # runtime StrEnum equality with a plain string is the whole point.
        category: str = ErrorCategory.DNS_ERROR
        source: str = SourceType.TELEGRAM_CHANNEL
        assert category == "DNS_ERROR"
        assert source == "telegram_channel"
        assert isinstance(ErrorCategory.DNS_ERROR, str)


class TestUtcNow:
    def test_is_timezone_aware_utc(self) -> None:
        value = utcnow()
        assert value.tzinfo is not None
        assert value.utcoffset() == datetime.now(UTC).utcoffset()
        assert value.tzinfo == UTC

    def test_is_monotonic_enough_to_order(self) -> None:
        assert utcnow() <= utcnow()


class TestSecretCoercion:
    """``Proxy.secret`` must never be a bare printable ``str`` in Python.

    ``SecretText`` only converts on a database round trip, so without the
    validator a freshly constructed Proxy -- the one most likely to be printed
    while debugging -- would hold plaintext.
    """

    def test_assignment_of_a_str_is_wrapped(self) -> None:
        secret = "ee" + "a1" * 15
        proxy = Proxy(server="h", port=443, secret=secret, fingerprint="f" * 64)
        assert isinstance(proxy.secret, ProxySecret)
        assert proxy.secret.reveal() == secret

    def test_assignment_of_a_proxy_secret_is_preserved(self) -> None:
        wrapped = ProxySecret("ee" + "a1" * 15)
        proxy = Proxy(server="h", port=443, secret=wrapped, fingerprint="f" * 64)
        assert proxy.secret is wrapped

    def test_reassignment_is_also_wrapped(self) -> None:
        proxy = Proxy(server="h", port=443, secret="ee" + "a1" * 15, fingerprint="f" * 64)
        # The attribute is annotated ProxySecret, so mypy rejects a bare str --
        # which is exactly why the validator exists: untyped callers (JSON, bulk
        # loaders, scripts) do pass strings, and the coercion catches them.
        proxy.secret = "ee" + "b2" * 15  # type: ignore[assignment]
        assert isinstance(proxy.secret, ProxySecret)

    def test_none_stays_none(self) -> None:
        proxy = Proxy(server="h", port=443, fingerprint="f" * 64)
        assert proxy.secret is None

    def test_rejects_an_empty_secret_at_assignment(self) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            Proxy(server="h", port=443, secret="   ", fingerprint="f" * 64)


class TestMaskedSecretText:
    @pytest.mark.parametrize("value", [None, 42, b"raw", object()])
    def test_never_raises_for_unexpected_input(self, value: object) -> None:
        # repr runs in debuggers and pytest output -- the worst place to throw.
        assert isinstance(masked_secret_text(value), str)

    def test_masks_a_proxy_secret(self) -> None:
        secret = "ee" + "a1" * 15
        assert masked_secret_text(ProxySecret(secret)) == ProxySecret(secret).masked
        assert secret not in masked_secret_text(ProxySecret(secret))

    def test_masks_a_raw_str(self) -> None:
        secret = "ee" + "a1" * 15
        assert secret not in masked_secret_text(secret)

    def test_none_renders_as_a_dash(self) -> None:
        assert masked_secret_text(None) == "-"


class TestReprs:
    def test_proxy_repr_masks_the_secret(self) -> None:
        secret = "ee" + "a1" * 15
        proxy = Proxy(
            server="proxy.example.com",
            port=443,
            secret=ProxySecret(secret),
            fingerprint="f" * 64,
        )
        rendered = repr(proxy)
        assert secret not in rendered
        assert "proxy.example.com" in rendered
        assert "443" in rendered

    def test_proxy_repr_before_secret_is_set(self) -> None:
        # id/secret may be None during construction; repr must not raise.
        assert "secret=-" in repr(Proxy(server="h", port=1, fingerprint="f" * 64))

    def test_observation_repr_has_no_secret_or_message(self) -> None:
        observation = ProxyObservation(proxy_id=1, success=False, error_category="DNS_ERROR")
        rendered = repr(observation)
        assert "DNS_ERROR" in rendered
        assert "success=False" in rendered

    def test_score_repr_includes_version(self) -> None:
        score = ProxyScore(proxy_id=1, score=Decimal("12.5"), scoring_version=SCORING_VERSION_V1)
        assert SCORING_VERSION_V1 in repr(score)

    def test_discovery_repr(self) -> None:
        discovery = ProxyDiscovery(
            proxy_id=1, source_type=SourceType.TELEGRAM_CHANNEL, source_name="@chan"
        )
        assert "@chan" in repr(discovery)


class TestDiscoveryColumns:
    def test_columns(self) -> None:
        assert set(ProxyDiscovery.__table__.columns.keys()) == {
            "id",
            "proxy_id",
            "source_type",
            "source_name",
            "source_url",
            "discovered_at",
            "raw_reference",
            "created_at",
        }

    def test_source_url_is_nullable(self) -> None:
        # A manual import has no URL.
        assert ProxyDiscovery.__table__.c.source_url.nullable is True

    def test_source_name_is_not_blank(self) -> None:
        assert "char_length(source_name) > 0" in ddl(ProxyDiscovery.__table__)

    def test_provenance_indexes(self) -> None:
        names = {i.name for i in table_of(ProxyDiscovery).indexes}
        assert {
            "ix_proxy_discoveries_proxy_id",
            "ix_proxy_discoveries_discovered_at",
            "ix_proxy_discoveries_source_type",
        } <= names
