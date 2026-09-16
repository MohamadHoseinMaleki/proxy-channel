"""Unit tests for ranking policy and latest-score selection. No database."""

from __future__ import annotations

import ast
import json
import pathlib
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from core.identity import ProxySecret
from core.models import SCORING_VERSION_V1
from modules.discovery.models import SecretType
from modules.ranking.models import ProxyListing, RankingPage
from modules.ranking.policy import (
    DEFAULT_LIMIT,
    MAX_AGE_HOURS,
    MAX_LIMIT,
    TYPE_UNKNOWN,
    classify_secret_type,
    coerce_limit,
    freshness_cutoff,
    require_aware,
)
from modules.ranking.selector import ScoreSnapshot, rank_snapshots

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
RANKING_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "modules" / "ranking"
SECRET = "dd" + "ab" * 16


def _snap(
    proxy_id: int,
    *,
    score: str,
    hours_ago: float = 0.5,
    score_id: int | None = None,
    is_active: bool = True,
    version: str = SCORING_VERSION_V1,
    samples: int = 10,
    server: str = "203.0.113.10",
    port: int = 443,
    secret_type: str = "dd",
    calculated_at: datetime | None = None,
    reliability_24h: str | None = "90.00",
) -> ScoreSnapshot:
    moment = calculated_at if calculated_at is not None else NOW - timedelta(hours=hours_ago)
    return ScoreSnapshot(
        proxy_id=proxy_id,
        is_active=is_active,
        server=server,
        port=port,
        secret_type=secret_type,
        score_id=score_id if score_id is not None else proxy_id * 100,
        score=Decimal(score),
        calculated_at=moment,
        scoring_version=version,
        reliability_1h=Decimal("100.00") if samples else None,
        reliability_6h=Decimal("95.00") if samples else None,
        reliability_24h=None if reliability_24h is None else Decimal(reliability_24h),
        latency_p50_ms=Decimal("2100.000") if samples else None,
        latency_p95_ms=Decimal("2500.000") if samples else None,
        sample_count_24h=samples,
    )


def _rank(*rows: ScoreSnapshot, limit: int = 20, as_of: datetime = NOW) -> tuple[ProxyListing, ...]:
    return rank_snapshots(rows, as_of=as_of, limit=limit, max_age_hours=MAX_AGE_HOURS)


class TestPurity:
    def test_pure_modules_have_no_io_imports(self) -> None:
        forbidden = {
            "sqlalchemy",
            "asyncpg",
            "telethon",
            "httpx",
            "socket",
            "aiohttp",
            "requests",
            "random",
            "secrets",
        }
        for path in RANKING_SRC.glob("*.py"):
            if path.name == "service.py":
                continue
            names: set[str] = set()
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    names.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    names.add(node.module.split(".")[0])
            found = forbidden & names
            assert not found, f"{path.name} imports {sorted(found)}"

    def test_selector_does_not_read_the_wall_clock(self) -> None:
        tree = ast.parse((RANKING_SRC / "selector.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == "utcnow":
                pytest.fail("selector must not call utcnow")
            if isinstance(node, ast.Attribute) and node.attr in {"now", "utcnow"}:
                pytest.fail("selector must not call datetime.now / utcnow")


class TestLimitPolicy:
    def test_default_and_max(self) -> None:
        assert coerce_limit(None) == DEFAULT_LIMIT == 20
        assert coerce_limit(1) == 1
        assert coerce_limit(MAX_LIMIT) == 100

    def test_invalid_limit_is_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"1\.\.100"):
            coerce_limit(0)
        with pytest.raises(ValueError, match=r"1\.\.100"):
            coerce_limit(-1)
        with pytest.raises(ValueError, match=r"1\.\.100"):
            coerce_limit(101)
        with pytest.raises(TypeError):
            coerce_limit(True)


class TestEmptyAndSingle:
    def test_empty_ranking(self) -> None:
        assert _rank() == ()

    def test_one_proxy(self) -> None:
        items = _rank(_snap(1, score="85.000"))
        assert len(items) == 1
        assert items[0].proxy_id == 1
        assert items[0].score == Decimal("85.000")
        assert items[0].scoring_version == SCORING_VERSION_V1


class TestOrdering:
    def test_score_descending(self) -> None:
        items = _rank(
            _snap(1, score="10.000"),
            _snap(2, score="90.000"),
            _snap(3, score="50.000"),
        )
        assert [item.proxy_id for item in items] == [2, 3, 1]

    def test_ties_break_on_scored_at_then_proxy_id(self) -> None:
        same = NOW - timedelta(hours=1)
        items = _rank(
            _snap(30, score="50.000", calculated_at=same),
            _snap(10, score="50.000", calculated_at=same),
            _snap(20, score="50.000", calculated_at=same + timedelta(minutes=1)),
        )
        assert [item.proxy_id for item in items] == [20, 10, 30]

    def test_identical_score_and_timestamp_is_stable_across_calls(self) -> None:
        same = NOW - timedelta(minutes=5)
        rows = (
            _snap(7, score="40.000", calculated_at=same),
            _snap(3, score="40.000", calculated_at=same),
            _snap(9, score="40.000", calculated_at=same),
        )
        first = _rank(*rows)
        second = _rank(*reversed(rows))
        assert [item.proxy_id for item in first] == [3, 7, 9]
        assert [item.proxy_id for item in second] == [item.proxy_id for item in first]


class TestLatestSnapshot:
    def test_old_snapshot_is_ignored_in_favour_of_the_new_one(self) -> None:
        items = _rank(
            _snap(1, score="40.000", hours_ago=5.0, score_id=1),
            _snap(1, score="85.000", hours_ago=0.5, score_id=2),
        )
        assert len(items) == 1
        assert items[0].score == Decimal("85.000")
        assert items[0].proxy_id == 1

    def test_same_timestamp_prefers_higher_score_id(self) -> None:
        moment = NOW - timedelta(hours=1)
        items = _rank(
            _snap(1, score="10.000", calculated_at=moment, score_id=1),
            _snap(1, score="70.000", calculated_at=moment, score_id=2),
        )
        assert items[0].score == Decimal("70.000")

    def test_wrong_scoring_version_is_excluded(self) -> None:
        items = _rank(
            _snap(1, score="99.000", version="v0"),
            _snap(2, score="10.000"),
        )
        assert [item.proxy_id for item in items] == [2]

    def test_inactive_proxy_is_excluded(self) -> None:
        items = _rank(
            _snap(1, score="99.000", is_active=False),
            _snap(2, score="10.000"),
        )
        assert [item.proxy_id for item in items] == [2]

    def test_no_score_proxy_is_absent(self) -> None:
        items = _rank(_snap(2, score="10.000"))
        assert [item.proxy_id for item in items] == [2]

    def test_empty_window_is_not_serviceable(self) -> None:
        items = _rank(
            _snap(1, score="0.000", samples=0, reliability_24h=None),
            _snap(2, score="10.000", samples=4),
        )
        assert [item.proxy_id for item in items] == [2]

    def test_failed_only_history_is_eligible_but_ranks_low(self) -> None:
        dead = _snap(1, score="2.000", samples=10, reliability_24h="0.00")
        live = _snap(2, score="40.000", samples=10)
        items = _rank(dead, live)
        assert [item.proxy_id for item in items] == [2, 1]


class TestFreshness:
    def test_stale_score_is_excluded(self) -> None:
        items = _rank(
            _snap(1, score="99.000", hours_ago=MAX_AGE_HOURS + 0.01),
            _snap(2, score="10.000", hours_ago=1.0),
        )
        assert [item.proxy_id for item in items] == [2]

    def test_exact_freshness_boundary_is_included(self) -> None:
        items = _rank(_snap(1, score="50.000", hours_ago=MAX_AGE_HOURS))
        assert len(items) == 1

    def test_just_newer_than_boundary_is_included(self) -> None:
        items = _rank(_snap(1, score="50.000", hours_ago=MAX_AGE_HOURS - 0.001))
        assert len(items) == 1

    def test_future_timestamp_is_treated_as_fresh(self) -> None:
        items = _rank(_snap(1, score="50.000", hours_ago=-1.0))
        assert len(items) == 1

    def test_naive_as_of_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            rank_snapshots(
                [_snap(1, score="50.000")],
                as_of=datetime(2026, 9, 16, 12, 0),
                limit=10,
                max_age_hours=MAX_AGE_HOURS,
            )

    def test_fixed_offset_as_of_is_accepted(self) -> None:
        tehran = timezone(timedelta(hours=3, minutes=30), name="Asia/Tehran")
        as_of = datetime(2026, 9, 16, 15, 30, tzinfo=tehran)
        items = rank_snapshots(
            [_snap(1, score="50.000", calculated_at=as_of - timedelta(hours=1))],
            as_of=as_of,
            limit=10,
            max_age_hours=MAX_AGE_HOURS,
        )
        assert len(items) == 1

    def test_empty_latest_is_not_replaced_by_an_older_good_snapshot(self) -> None:
        items = _rank(
            _snap(1, score="90.000", hours_ago=2.0, score_id=1, samples=10),
            _snap(
                1,
                score="0.000",
                hours_ago=0.1,
                score_id=2,
                samples=0,
                reliability_24h=None,
            ),
        )
        assert items == ()


class TestLimit:
    def test_bounded_limit(self) -> None:
        rows = [_snap(i, score=str(i)) for i in range(1, 8)]
        items = _rank(*rows, limit=3)
        assert [item.proxy_id for item in items] == [7, 6, 5]


class TestSecretSafety:
    def test_listing_repr_and_json_never_contain_the_secret(self) -> None:
        listing = _rank(_snap(1, score="50.000"))[0]
        page = RankingPage(
            items=(listing,),
            as_of=NOW,
            limit=20,
            max_age_hours=MAX_AGE_HOURS,
            scoring_version=SCORING_VERSION_V1,
        )
        rendered = [
            repr(listing),
            str(listing),
            repr(page),
            json.dumps(listing.to_public_dict()),
            json.dumps(page.to_public_dict()),
        ]
        for text in rendered:
            assert SECRET not in text
            assert "secret" not in text.lower() or "secret_type" in text
            assert "fingerprint" not in text
            assert "tg://" not in text

    def test_public_dict_keys_are_the_serving_contract(self) -> None:
        listing = _rank(_snap(1, score="50.125"))[0]
        payload = listing.to_public_dict()
        assert set(payload) == {
            "proxy_id",
            "server",
            "port",
            "secret_type",
            "score",
            "reliability_1h",
            "reliability_6h",
            "reliability_24h",
            "latency_p50_ms",
            "latency_p95_ms",
            "sample_count_24h",
            "scoring_version",
            "scored_at",
        }
        assert "secret" not in payload
        assert payload["score"] == "50.125"

    def test_classify_secret_type_does_not_echo_plaintext(self) -> None:
        wrapped = ProxySecret(SECRET)
        assert classify_secret_type(wrapped) == SecretType.SECURE_RANDOMIZED.value
        assert SECRET not in repr(wrapped)
        assert classify_secret_type(ProxySecret("ee" + "11" * 16)) == SecretType.FAKE_TLS.value
        assert classify_secret_type(ProxySecret("aa" * 16)) == SecretType.LEGACY.value
        assert classify_secret_type(ProxySecret("ff" + "00" * 8)) == TYPE_UNKNOWN


class TestCutoffHelper:
    def test_freshness_cutoff_is_24h(self) -> None:
        assert freshness_cutoff(NOW) == NOW - timedelta(hours=24)
        with pytest.raises(ValueError, match="timezone-aware"):
            require_aware(datetime(2026, 9, 16, 12, 0))
