"""Unit tests for publishable proxy selection. No database, no network."""

from __future__ import annotations

import ast
import json
import pathlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from core.identity import PROTOCOL_MTPROTO, ProxySecret, compute_fingerprint
from core.models import SCORING_VERSION_V1
from modules.discovery.models import SecretType
from modules.discovery.parser import parse_proxy_url
from modules.reporting.models import JSON_ITEM_KEYS, JSON_REPORT_KEYS, Report, ReportItem
from modules.reporting.policy import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    MAX_SUCCESS_AGE_HOURS,
    coerce_limit,
    label_freshness,
    require_aware,
    score_is_finite,
    success_cutoff,
)
from modules.reporting.selector import PublishCandidate, select_publishable
from modules.reporting.urls import canonical_tg_proxy_url
from modules.scoring.models import ScoreFreshness

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
REPORTING_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "modules" / "reporting"
DD_SECRET = "dd" + "ab" * 16
LEGACY_SECRET = "aa" * 16
EE_SECRET = "ee" + "11" * 16


def _fp(server: str, port: int, secret: str) -> str:
    return compute_fingerprint(server=server, port=port, secret=secret)


def _cand(
    proxy_id: int,
    *,
    score: str,
    server: str = "1.1.1.1",
    port: int = 443,
    secret: str = DD_SECRET,
    hours_ago: float = 0.5,
    score_id: int | None = None,
    is_active: bool = True,
    version: str = SCORING_VERSION_V1,
    samples: int = 10,
    success_hours_ago: float | None = None,
    last_meaningful_success: bool = True,
    latency_p50: str | None = "2100.000",
    fingerprint: str | None = None,
    calculated_at: datetime | None = None,
) -> PublishCandidate:
    scored = calculated_at if calculated_at is not None else NOW - timedelta(hours=hours_ago)
    success_at = NOW - timedelta(
        hours=success_hours_ago if success_hours_ago is not None else hours_ago
    )
    return PublishCandidate(
        proxy_id=proxy_id,
        is_active=is_active,
        server=server,
        port=port,
        secret=ProxySecret(secret),
        protocol=PROTOCOL_MTPROTO,
        fingerprint=fingerprint or _fp(server, port, secret),
        score_id=score_id if score_id is not None else proxy_id * 100,
        score=Decimal(score),
        calculated_at=scored,
        scoring_version=version,
        reliability_24h=Decimal("90.00") if samples else None,
        latency_p50_ms=None if latency_p50 is None else Decimal(latency_p50),
        latency_p95_ms=Decimal("2500.000") if samples else None,
        sample_count_24h=samples,
        last_meaningful_at=success_at,
        last_meaningful_success=last_meaningful_success,
    )


def _select(
    *rows: PublishCandidate,
    limit: int = 20,
    as_of: datetime = NOW,
    max_age: float = MAX_SUCCESS_AGE_HOURS,
) -> tuple[ReportItem, ...]:
    return select_publishable(
        rows,
        as_of=as_of,
        limit=limit,
        max_success_age_hours=max_age,
    )


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
        for path in REPORTING_SRC.glob("*.py"):
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
        tree = ast.parse((REPORTING_SRC / "selector.py").read_text(encoding="utf-8"))
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


class TestSelection:
    def test_valid_recent_proxy_is_selected(self) -> None:
        items = _select(_cand(1, score="85.000", success_hours_ago=0.5))
        assert len(items) == 1
        assert items[0].proxy_id == 1
        assert items[0].scoring_version == SCORING_VERSION_V1
        assert items[0].freshness is ScoreFreshness.RECENT

    def test_no_successful_observation_is_excluded(self) -> None:
        items = _select(_cand(1, score="90.000", last_meaningful_success=False))
        assert items == ()

    def test_stale_success_is_excluded(self) -> None:
        items = _select(
            _cand(1, score="99.000", success_hours_ago=MAX_SUCCESS_AGE_HOURS + 0.01),
            _cand(2, score="10.000", server="1.0.0.1", success_hours_ago=1.0),
        )
        assert [item.proxy_id for item in items] == [2]

    def test_exact_success_boundary_is_included(self) -> None:
        items = _select(_cand(1, score="50.000", success_hours_ago=MAX_SUCCESS_AGE_HOURS))
        assert len(items) == 1

    def test_wrong_scoring_version_is_excluded(self) -> None:
        items = _select(
            _cand(1, score="99.000", version="v0"),
            _cand(2, score="10.000", server="8.8.8.8"),
        )
        assert [item.proxy_id for item in items] == [2]

    def test_empty_window_score_is_excluded(self) -> None:
        items = _select(_cand(1, score="0.000", samples=0, latency_p50=None))
        assert items == ()

    def test_inactive_proxy_is_excluded(self) -> None:
        items = _select(_cand(1, score="99.000", is_active=False))
        assert items == ()

    def test_nan_score_is_excluded(self) -> None:
        assert score_is_finite(Decimal("50.000"))
        assert not score_is_finite(Decimal("NaN"))
        assert not score_is_finite(Decimal("Infinity"))
        bad = replace(_cand(1, score="50.000"), score=Decimal("NaN"))
        assert _select(bad) == ()

    def test_historical_score_is_superseded_by_latest(self) -> None:
        items = _select(
            _cand(1, score="90.000", hours_ago=5.0, score_id=1, success_hours_ago=0.2),
            _cand(1, score="12.000", hours_ago=0.1, score_id=2, success_hours_ago=0.2),
        )
        assert len(items) == 1
        assert items[0].score == Decimal("12.000")

    def test_old_high_score_plus_recent_failure_is_excluded(self) -> None:
        items = _select(
            _cand(
                1,
                score="90.000",
                hours_ago=0.1,
                success_hours_ago=0.05,
                last_meaningful_success=False,
            )
        )
        assert items == ()

    def test_old_success_without_recent_verification_is_excluded(self) -> None:
        items = _select(_cand(1, score="90.000", success_hours_ago=12.0))
        assert items == ()

    def test_multiple_scores_do_not_duplicate_output(self) -> None:
        items = _select(
            _cand(1, score="40.000", score_id=1, success_hours_ago=0.4),
            _cand(1, score="80.000", score_id=2, success_hours_ago=0.4),
            _cand(1, score="70.000", score_id=3, success_hours_ago=0.4),
        )
        assert [item.proxy_id for item in items] == [1]
        assert items[0].score == Decimal("70.000")


class TestUnsupportedAndCancelled:
    def test_fake_tls_is_never_selected(self) -> None:
        items = _select(
            _cand(1, score="99.000", secret=EE_SECRET, server="8.8.4.4"),
            _cand(2, score="10.000", server="9.9.9.9"),
        )
        assert [item.proxy_id for item in items] == [2]
        assert all(item.secret_type != SecretType.FAKE_TLS.value for item in items)

    def test_cancelled_never_satisfies_recent_success(self) -> None:
        # CANCELLED is not a success; the selector only sees last_meaningful_success.
        items = _select(_cand(1, score="80.000", last_meaningful_success=False))
        assert items == ()


class TestTopN:
    def test_limit_one(self) -> None:
        items = _select(
            _cand(1, score="10.000", server="1.1.1.1"),
            _cand(2, score="90.000", server="1.0.0.1"),
            limit=1,
        )
        assert [item.proxy_id for item in items] == [2]

    def test_limit_n(self) -> None:
        rows = [
            _cand(i, score=str(i * 10), server=f"1.0.0.{i}", secret="dd" + f"{i:02x}" * 16)
            for i in range(1, 6)
        ]
        items = _select(*rows, limit=3)
        assert [item.proxy_id for item in items] == [5, 4, 3]

    def test_fewer_eligible_than_limit(self) -> None:
        items = _select(_cand(1, score="40.000"), limit=10)
        assert len(items) == 1

    def test_zero_eligible(self) -> None:
        assert _select() == ()

    def test_result_never_exceeds_limit(self) -> None:
        rows = [
            _cand(i, score="50.000", server=f"8.8.8.{i}", secret="dd" + f"{i:02x}" * 16)
            for i in range(1, 9)
        ]
        items = _select(*rows, limit=4)
        assert len(items) <= 4


class TestRanking:
    def test_higher_score_sorts_first(self) -> None:
        items = _select(
            _cand(1, score="10.000", server="1.1.1.1"),
            _cand(2, score="90.000", server="1.0.0.1"),
            _cand(3, score="50.000", server="8.8.8.8"),
        )
        assert [item.proxy_id for item in items] == [2, 3, 1]

    def test_equal_score_prefers_more_recent_success(self) -> None:
        items = _select(
            _cand(1, score="50.000", server="1.1.1.1", success_hours_ago=2.0),
            _cand(2, score="50.000", server="1.0.0.1", success_hours_ago=0.1),
        )
        assert [item.proxy_id for item in items] == [2, 1]

    def test_equal_score_and_success_prefers_lower_latency(self) -> None:
        same = 0.2
        items = _select(
            _cand(
                1,
                score="50.000",
                server="1.1.1.1",
                success_hours_ago=same,
                latency_p50="4000.000",
            ),
            _cand(
                2,
                score="50.000",
                server="1.0.0.1",
                success_hours_ago=same,
                latency_p50="2100.000",
            ),
        )
        assert [item.proxy_id for item in items] == [2, 1]

    def test_fingerprint_is_the_final_tie_breaker(self) -> None:
        same = 0.2
        left = _cand(
            1,
            score="50.000",
            server="1.1.1.1",
            secret=DD_SECRET,
            success_hours_ago=same,
            latency_p50="2100.000",
        )
        right = _cand(
            2,
            score="50.000",
            server="1.0.0.1",
            secret=LEGACY_SECRET,
            success_hours_ago=same,
            latency_p50="2100.000",
        )
        first = _select(left, right)
        second = _select(right, left)
        assert [item.proxy_id for item in first] == [item.proxy_id for item in second]
        fingerprints = [item.fingerprint for item in first]
        assert fingerprints == sorted(fingerprints)

    def test_missing_latency_sorts_after_measured_latency(self) -> None:
        items = _select(
            _cand(
                1,
                score="50.000",
                server="1.1.1.1",
                latency_p50=None,
                success_hours_ago=0.2,
            ),
            _cand(
                2,
                score="50.000",
                server="1.0.0.1",
                latency_p50="2100.000",
                success_hours_ago=0.2,
            ),
        )
        assert [item.proxy_id for item in items] == [2, 1]


class TestFreshnessLabels:
    def test_recent_aging_stale_labels(self) -> None:
        assert label_freshness(NOW - timedelta(hours=1), NOW) is ScoreFreshness.RECENT
        assert label_freshness(NOW - timedelta(hours=12), NOW) is ScoreFreshness.AGING
        assert label_freshness(NOW - timedelta(hours=25), NOW) is ScoreFreshness.STALE

    def test_naive_as_of_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            require_aware(datetime(2026, 9, 16, 12, 0))
        with pytest.raises(ValueError, match="timezone-aware"):
            select_publishable(
                [_cand(1, score="50.000")],
                as_of=datetime(2026, 9, 16, 12, 0),
                limit=10,
                max_success_age_hours=MAX_SUCCESS_AGE_HOURS,
            )

    def test_fixed_offset_as_of_is_accepted(self) -> None:
        tehran = timezone(timedelta(hours=3, minutes=30), name="Asia/Tehran")
        as_of = datetime(2026, 9, 16, 15, 30, tzinfo=tehran)
        items = select_publishable(
            [_cand(1, score="50.000", calculated_at=as_of - timedelta(hours=1))],
            as_of=as_of,
            limit=10,
            max_success_age_hours=MAX_SUCCESS_AGE_HOURS,
        )
        assert len(items) == 1

    def test_success_cutoff_is_six_hours(self) -> None:
        assert success_cutoff(NOW) == NOW - timedelta(hours=6)


class TestUrlsAndSerialization:
    def test_canonical_url_round_trips_through_the_parser(self) -> None:
        url = canonical_tg_proxy_url(server="1.1.1.1", port=443, secret=DD_SECRET)
        parsed = parse_proxy_url(url)
        assert parsed.server == "1.1.1.1"
        assert parsed.port == 443
        assert parsed.secret.reveal() == DD_SECRET
        assert parsed.secret_type is SecretType.SECURE_RANDOMIZED

    def test_ipv6_and_legacy_secret_round_trip(self) -> None:
        url = canonical_tg_proxy_url(server="2001:4860:4860::8888", port=443, secret=LEGACY_SECRET)
        parsed = parse_proxy_url(url)
        assert parsed.server == "2001:4860:4860::8888"
        assert parsed.secret.reveal() == LEGACY_SECRET
        assert parsed.secret_type is SecretType.LEGACY

    def test_json_schema_and_empty_report(self) -> None:
        empty = Report(
            items=(),
            generated_at=NOW,
            limit=20,
            max_success_age_hours=6.0,
            scoring_version=SCORING_VERSION_V1,
        )
        payload = empty.to_json_dict()
        assert tuple(payload) == JSON_REPORT_KEYS
        assert payload["count"] == 0
        assert payload["proxies"] == []
        assert payload["generated_at"].endswith("+00:00") or payload["generated_at"].endswith("Z")
        assert empty.to_txt() == ""
        assert "secret" not in repr(empty)

    def test_json_contains_secret_txt_is_urls_repr_is_masked(self) -> None:
        items = _select(_cand(1, score="50.125", server="8.8.8.8"))
        report = Report(
            items=items,
            generated_at=NOW,
            limit=20,
            max_success_age_hours=6.0,
            scoring_version=SCORING_VERSION_V1,
        )
        payload = report.to_json_dict()
        item = payload["proxies"][0]
        assert tuple(item) == JSON_ITEM_KEYS
        assert item["secret"] == DD_SECRET
        assert item["score"] == "50.125"
        assert item["server"] == "8.8.8.8"
        assert "id" not in item
        assert "sa_" not in json.dumps(payload)
        rendered = repr(report) + repr(items[0])
        assert DD_SECRET not in rendered
        txt = report.to_txt()
        assert txt.endswith("\n")
        lines = txt.strip().split("\n")
        assert len(lines) == 1
        parsed = parse_proxy_url(lines[0])
        assert parsed.secret.reveal() == DD_SECRET
        assert parsed.fingerprint == items[0].fingerprint

    def test_json_and_txt_contain_the_same_proxy_set(self) -> None:
        items = _select(
            _cand(1, score="10.000", server="1.1.1.1"),
            _cand(2, score="90.000", server="1.0.0.1", secret=LEGACY_SECRET),
        )
        report = Report(
            items=items,
            generated_at=NOW,
            limit=20,
            max_success_age_hours=6.0,
            scoring_version=SCORING_VERSION_V1,
        )
        json_keys = {
            (row["server"], row["port"], row["secret"]) for row in report.to_json_dict()["proxies"]
        }
        txt_keys = set()
        for line in report.to_txt().splitlines():
            parsed = parse_proxy_url(line)
            txt_keys.add((parsed.server, parsed.port, parsed.secret.reveal()))
        assert json_keys == txt_keys
        assert report.to_json_dict()["count"] == len(items)

    def test_invariants_on_every_selected_item(self) -> None:
        items = _select(
            _cand(1, score="80.000", server="1.1.1.1"),
            _cand(2, score="20.000", server="1.0.0.1", secret=EE_SECRET),
            _cand(3, score="60.000", server="8.8.8.8", last_meaningful_success=False),
            _cand(4, score="40.000", server="9.9.9.9", success_hours_ago=12.0),
            _cand(5, score="70.000", server="8.8.4.4", secret=LEGACY_SECRET),
            limit=10,
        )
        assert len(items) <= 10
        assert [item.proxy_id for item in items] == [1, 5]
        for item in items:
            assert item.scoring_version == SCORING_VERSION_V1
            assert item.freshness is not ScoreFreshness.STALE
            assert item.secret_type != SecretType.FAKE_TLS.value
            assert item.last_success_at >= NOW - timedelta(hours=MAX_SUCCESS_AGE_HOURS)
            parsed = parse_proxy_url(item.url)
            assert parsed.fingerprint == item.fingerprint
