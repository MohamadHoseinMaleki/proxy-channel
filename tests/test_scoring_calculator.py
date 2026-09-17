"""Unit tests for the pure scoring function. No database, no network."""

from __future__ import annotations

import ast
import math
import pathlib
from dataclasses import MISSING, fields
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from core.models import SCORING_VERSION_V1, ErrorCategory
from modules.scoring.calculator import (
    CONFIDENCE_PRIOR_N,
    LAPLACE_ALPHA,
    LAPLACE_BETA,
    LATENCY_WEIGHT,
    LATENCY_WORST_MS,
    LOOKBACK_HOURS,
    RECENCY_HALF_LIFE_HOURS,
    RELIABILITY_WEIGHT,
    WINDOW_1H,
    WINDOW_6H,
    WINDOW_24H,
    score_observations,
)
from modules.scoring.models import ObservationInput, ScoreBreakdown, ScoreFreshness, ScoreStatus

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
SCORING_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "modules" / "scoring"


def _obs(
    hours_ago: float,
    *,
    success: bool,
    mtproto: float | None = None,
    tcp: float | None = None,
    total: float | None = None,
    category: str | None = None,
    now: datetime = NOW,
) -> ObservationInput:
    return ObservationInput(
        observed_at=now - timedelta(hours=hours_ago),
        success=success,
        mtproto_connect_ms=mtproto,
        tcp_connect_ms=tcp,
        total_latency_ms=total,
        error_category=category,
    )


def _n_success(
    count: int, *, hours_ago: float = 0.1, mtproto: float = 2100.0
) -> list[ObservationInput]:
    return [_obs(hours_ago + i * 0.01, success=True, mtproto=mtproto) for i in range(count)]


def _n_failure(
    count: int,
    *,
    hours_ago: float = 0.1,
    category: str = ErrorCategory.MT_PROTO_TIMEOUT,
) -> list[ObservationInput]:
    return [_obs(hours_ago + i * 0.01, success=False, category=category) for i in range(count)]


class TestPurity:
    def test_calculator_source_has_no_io_imports(self) -> None:
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
        for path in SCORING_SRC.glob("*.py"):
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

    def test_calculator_does_not_read_the_wall_clock(self) -> None:
        tree = ast.parse((SCORING_SRC / "calculator.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == "utcnow":
                pytest.fail("calculator must not call utcnow")
            if isinstance(node, ast.Attribute) and node.attr in {"now", "utcnow"}:
                pytest.fail("calculator must not call datetime.now / utcnow")

    def test_score_breakdown_requires_calculated_at(self) -> None:
        field = next(item for item in fields(ScoreBreakdown) if item.name == "calculated_at")
        assert field.default is MISSING
        assert field.default_factory is MISSING


class TestEmptyData:
    def test_zero_observations(self) -> None:
        result = score_observations(1, [], now=NOW)
        assert result.status is ScoreStatus.NO_OBSERVATIONS_IN_WINDOW
        assert result.score == Decimal("0.000")
        assert result.reliability_score == Decimal("0.000")
        assert result.latency_score == Decimal("0.000")
        assert result.confidence_score == Decimal("0.000")
        assert result.confidence_factor == 0.0
        assert result.observation_count == 0
        assert result.successful_count == 0
        assert result.reliability_1h is None
        assert result.reliability_6h is None
        assert result.reliability_24h is None
        assert result.sample_count_1h == 0
        assert result.sample_count_24h == 0
        assert result.latency_p50_ms is None
        assert result.latency_p95_ms is None
        assert result.mean_mtproto_ms is None
        assert result.last_success_at is None
        assert result.freshness is ScoreFreshness.STALE
        assert result.scoring_version == SCORING_VERSION_V1

    def test_all_observations_older_than_lookback(self) -> None:
        old = [_obs(LOOKBACK_HOURS + 1.0, success=True, mtproto=2000.0) for _ in range(10)]
        result = score_observations(2, old, now=NOW)
        assert result.status is ScoreStatus.NO_OBSERVATIONS_IN_WINDOW
        assert result.score == Decimal("0.000")
        assert result.observation_count == 0


class TestPerfectUnstableDead:
    def test_perfect_ten_of_ten_outranks_unstable_and_dead(self) -> None:
        perfect = score_observations(1, _n_success(10), now=NOW)
        unstable = score_observations(2, _n_success(5) + _n_failure(5, hours_ago=0.2), now=NOW)
        dead = score_observations(3, _n_failure(10), now=NOW)
        assert perfect.score > unstable.score > dead.score
        assert perfect.successful_count == 10
        assert unstable.successful_count == 5
        assert dead.successful_count == 0
        assert dead.latency_score == Decimal("0.000")
        assert dead.latency_p50_ms is None
        assert dead.freshness is ScoreFreshness.STALE
        assert perfect.freshness is ScoreFreshness.RECENT

    def test_ten_of_ten_raw_window_reliability_is_100(self) -> None:
        result = score_observations(1, _n_success(10), now=NOW)
        assert result.reliability_1h == Decimal("100.000")
        assert result.reliability_24h == Decimal("100.000")
        assert result.sample_count_1h == 10


class TestOneSuccessProblem:
    def test_one_of_one_has_less_confidence_than_one_hundred_of_one_hundred(self) -> None:
        one = score_observations(1, _n_success(1), now=NOW)
        many = score_observations(2, _n_success(100), now=NOW)
        assert one.confidence_factor < many.confidence_factor
        assert one.confidence_score < many.confidence_score
        assert one.score < many.score
        expected_one = 1 / (1 + CONFIDENCE_PRIOR_N)
        expected_many = 100 / (100 + CONFIDENCE_PRIOR_N)
        assert one.confidence_factor == pytest.approx(expected_one)
        assert many.confidence_factor == pytest.approx(expected_many)

    def test_one_of_one_window_rate_is_100_but_final_score_is_not(self) -> None:
        result = score_observations(1, _n_success(1), now=NOW)
        assert result.reliability_1h == Decimal("100.000")
        assert result.sample_count_1h == 1
        assert result.score < Decimal("20.000")

    def test_one_success_and_one_failure_share_confidence(self) -> None:
        one_ok = score_observations(1, _n_success(1), now=NOW)
        one_fail = score_observations(2, _n_failure(1), now=NOW)
        assert one_ok.confidence_factor == pytest.approx(1 / (1 + CONFIDENCE_PRIOR_N))
        assert one_fail.confidence_factor == one_ok.confidence_factor
        assert one_ok.score > one_fail.score

    def test_fifty_fifty_matches_one_hundred_confidence_but_not_score(self) -> None:
        mixed = score_observations(1, _n_success(50) + _n_failure(50, hours_ago=0.5), now=NOW)
        perfect = score_observations(2, _n_success(100), now=NOW)
        assert mixed.confidence_factor == pytest.approx(100 / (100 + CONFIDENCE_PRIOR_N))
        assert perfect.confidence_factor == mixed.confidence_factor
        assert mixed.score < perfect.score

    def test_one_hundred_plus_one_outranks_the_inverse(self) -> None:
        mostly_ok = score_observations(1, _n_success(100) + _n_failure(1, hours_ago=0.5), now=NOW)
        mostly_dead = score_observations(2, _n_success(1) + _n_failure(100, hours_ago=0.5), now=NOW)
        expected = 101 / (101 + CONFIDENCE_PRIOR_N)
        assert mostly_ok.confidence_factor == pytest.approx(expected)
        assert mostly_dead.confidence_factor == pytest.approx(expected)
        assert mostly_ok.score > mostly_dead.score


class TestRecency:
    def test_recent_failures_hurt_more_than_old_failures(self) -> None:
        recent_fail = _n_success(5, hours_ago=5.0) + _n_failure(5, hours_ago=0.1)
        recent_ok = _n_failure(5, hours_ago=5.0) + _n_success(5, hours_ago=0.1, mtproto=2100.0)
        worse = score_observations(1, recent_fail, now=NOW)
        better = score_observations(2, recent_ok, now=NOW)
        assert better.score > worse.score
        assert better.weighted_success_rate > worse.weighted_success_rate

    def test_half_life_weights_are_applied_by_the_calculator(self) -> None:
        # One success at age H plus one failure at age 0:
        # weighted_success_rate = w(H) / (w(H) + 1), with true half-life 6 h.
        expected = {
            0.0: 1.0 / 2.0,
            RECENCY_HALF_LIFE_HOURS: 0.5 / 1.5,
            12.0: 0.25 / 1.25,
            LOOKBACK_HOURS: 0.0625 / 1.0625,
        }
        assert math.exp(-math.log(2.0) * 6.0 / 6.0) == pytest.approx(0.5)
        assert math.exp(-math.log(2.0) * 12.0 / 6.0) == pytest.approx(0.25)
        assert math.exp(-math.log(2.0) * 24.0 / 6.0) == pytest.approx(0.0625)
        for hours, rate in expected.items():
            items = [
                _obs(hours, success=True, mtproto=2100.0),
                _obs(0.0, success=False, category=ErrorCategory.MT_PROTO_TIMEOUT),
            ]
            result = score_observations(1, items, now=NOW)
            assert result.weighted_success_rate == pytest.approx(rate), hours


class TestLatency:
    def test_low_latency_scores_higher_than_high_latency(self) -> None:
        fast = score_observations(1, _n_success(10, mtproto=2100.0), now=NOW)
        slow = score_observations(2, _n_success(10, mtproto=7000.0), now=NOW)
        assert fast.latency_score > slow.latency_score
        assert fast.score > slow.score

    def test_failed_observations_are_not_latency_samples(self) -> None:
        mixed = [
            *_n_success(3, mtproto=2100.0),
            _obs(0.05, success=False, mtproto=50.0, tcp=10.0, category="TCP_TIMEOUT"),
            _obs(0.04, success=False, total=5000.0, category="MT_PROTO_TIMEOUT"),
        ]
        result = score_observations(1, mixed, now=NOW)
        assert result.latency_p50_ms == Decimal("2100.000")
        assert result.latency_p95_ms == Decimal("2100.000")
        assert result.successful_count == 3

    def test_missing_success_latency_yields_zero_latency_score(self) -> None:
        items = [_obs(0.1, success=True, mtproto=None) for _ in range(8)]
        result = score_observations(1, items, now=NOW)
        assert result.latency_score == Decimal("0.000")
        assert result.latency_p50_ms is None
        assert result.successful_count == 8
        assert result.score > Decimal("0.000")

    def test_zero_latency_is_a_perfect_latency_score(self) -> None:
        result = score_observations(1, _n_success(10, mtproto=0.0), now=NOW)
        assert result.latency_score == Decimal("100.000")

    def test_latency_at_or_above_worst_bound_is_zero(self) -> None:
        above = score_observations(1, _n_success(10, mtproto=LATENCY_WORST_MS + 500.0), now=NOW)
        exact = score_observations(2, _n_success(10, mtproto=LATENCY_WORST_MS), now=NOW)
        assert above.latency_score == Decimal("0.000")
        assert exact.latency_score == Decimal("0.000")

    def test_tcp_connect_ms_is_not_used_for_latency_score(self) -> None:
        only_tcp = [_obs(0.1, success=True, mtproto=None, tcp=5.0) for _ in range(8)]
        result = score_observations(1, only_tcp, now=NOW)
        assert result.latency_score == Decimal("0.000")
        assert result.latency_p50_ms is None


class TestFailureCategories:
    def test_failure_counts_are_deterministic_and_sorted(self) -> None:
        items = (
            _n_failure(3, category=ErrorCategory.TCP_REFUSED)
            + _n_failure(2, hours_ago=0.2, category=ErrorCategory.MT_PROTO_TIMEOUT)
            + _n_failure(1, hours_ago=0.3, category=ErrorCategory.SSRF_BLOCKED)
            + _n_success(1, hours_ago=0.4, mtproto=2100.0)
        )
        result = score_observations(1, items, now=NOW)
        assert result.failure_counts == (
            (ErrorCategory.MT_PROTO_TIMEOUT, 2),
            (ErrorCategory.SSRF_BLOCKED, 1),
            (ErrorCategory.TCP_REFUSED, 3),
        )

    def test_unsupported_transport_is_just_a_failure(self) -> None:
        items = _n_failure(6, category=ErrorCategory.UNSUPPORTED_TRANSPORT)
        result = score_observations(1, items, now=NOW)
        assert result.successful_count == 0
        assert result.latency_p50_ms is None
        assert result.freshness is ScoreFreshness.STALE
        assert result.failure_counts == ((ErrorCategory.UNSUPPORTED_TRANSPORT, 6),)
        # Not ranked as verified: score stays below a single recent GetConfig success.
        verified = score_observations(2, _n_success(1, mtproto=2100.0), now=NOW)
        assert result.score < verified.score

    def test_cancelled_is_not_a_success_or_a_latency_sample(self) -> None:
        items = _n_failure(4, category=ErrorCategory.CANCELLED) + _n_success(1, mtproto=2100.0)
        result = score_observations(1, items, now=NOW)
        assert result.successful_count == 1
        assert result.failure_counts == ((ErrorCategory.CANCELLED, 4),)
        assert result.latency_p50_ms == Decimal("2100.000")

    def test_missing_category_is_counted_as_unknown(self) -> None:
        items = [_obs(0.1, success=False, category=None)]
        result = score_observations(1, items, now=NOW)
        assert result.failure_counts == (("UNKNOWN_ERROR", 1),)


class TestDeterminismAndOrder:
    def test_same_observations_same_score(self) -> None:
        items = _n_success(4) + _n_failure(3)
        first = score_observations(9, items, now=NOW)
        second = score_observations(9, items, now=NOW)
        assert first == second

    def test_input_order_does_not_change_the_score(self) -> None:
        items = (
            _n_success(5, mtproto=2200.0)
            + _n_failure(5)
            + _n_success(3, hours_ago=3.0, mtproto=4000.0)
        )
        shuffled = list(reversed(items))
        left = score_observations(1, items, now=NOW)
        right = score_observations(1, shuffled, now=NOW)
        assert left.score == right.score
        assert left.reliability_score == right.reliability_score
        assert left.latency_p50_ms == right.latency_p50_ms
        assert left.latency_p95_ms == right.latency_p95_ms
        assert left.failure_counts == right.failure_counts
        assert left.weighted_success_rate == right.weighted_success_rate


class TestBoundaries:
    def test_naive_now_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            score_observations(1, _n_success(1), now=datetime(2026, 9, 16, 12, 0))

    def test_non_utc_fixed_offset_is_accepted(self) -> None:
        # D-033: do not require IANA tzdata (missing on Windows).
        tehran = timezone(timedelta(hours=3, minutes=30), name="Asia/Tehran")
        now = datetime(2026, 9, 16, 15, 30, tzinfo=tehran)
        items = [
            ObservationInput(
                observed_at=now - timedelta(minutes=5),
                success=True,
                mtproto_connect_ms=2100.0,
            )
        ]
        result = score_observations(1, items, now=now)
        assert result.observation_count == 1

    def test_future_observation_is_clamped_to_full_weight(self) -> None:
        future = [_obs(-1.0, success=True, mtproto=2100.0)]
        present = [_obs(0.0, success=True, mtproto=2100.0)]
        a = score_observations(1, future, now=NOW)
        b = score_observations(1, present, now=NOW)
        assert a.score == b.score
        assert a.weighted_success_rate == b.weighted_success_rate

    def test_exactly_lookback_boundary_is_included(self) -> None:
        items = [_obs(LOOKBACK_HOURS, success=True, mtproto=2100.0)]
        result = score_observations(1, items, now=NOW)
        assert result.observation_count == 1

    def test_single_observation_percentiles_are_equal(self) -> None:
        result = score_observations(1, _n_success(1, mtproto=3210.5), now=NOW)
        assert result.latency_p50_ms == Decimal("3210.500")
        assert result.latency_p95_ms == Decimal("3210.500")

    def test_score_stays_within_0_100(self) -> None:
        cases = [
            [],
            _n_success(1, mtproto=0.0),
            _n_success(100, mtproto=0.0),
            _n_failure(100),
            _n_success(5, mtproto=LATENCY_WORST_MS * 4),
        ]
        for items in cases:
            result = score_observations(1, items, now=NOW)
            assert Decimal("0.000") <= result.score <= Decimal("100.000")
            assert Decimal("0.000") <= result.reliability_score <= Decimal("100.000")
            assert Decimal("0.000") <= result.latency_score <= Decimal("100.000")

    def test_weights_sum_to_one(self) -> None:
        assert pytest.approx(1.0) == RELIABILITY_WEIGHT + LATENCY_WEIGHT

    def test_laplace_prior_is_uniform(self) -> None:
        assert LAPLACE_ALPHA == LAPLACE_BETA == 1.0

    def test_repr_does_not_look_like_a_secret(self) -> None:
        item = _obs(0.1, success=True, mtproto=2100.0)
        result = score_observations(1, [item], now=NOW)
        for rendered in (repr(item), repr(result)):
            assert "secret" not in rendered.lower()
            assert "ee" not in rendered


class TestWindows:
    def test_1h_6h_24h_boundaries_are_inclusive(self) -> None:
        items = [
            _obs(0.5, success=True, mtproto=2100.0),
            _obs(WINDOW_1H, success=True, mtproto=2100.0),
            _obs(3.0, success=False, category=ErrorCategory.TCP_REFUSED),
            _obs(WINDOW_6H, success=True, mtproto=2100.0),
            _obs(12.0, success=False, category=ErrorCategory.MT_PROTO_TIMEOUT),
            _obs(WINDOW_24H, success=True, mtproto=2100.0),
            _obs(WINDOW_24H + 0.01, success=True, mtproto=2100.0),
        ]
        result = score_observations(1, items, now=NOW)
        assert result.sample_count_1h == 2
        assert result.sample_count_6h == 4
        assert result.sample_count_24h == 6
        assert result.observation_count == 6
        assert result.reliability_1h == Decimal("100.000")
        assert result.reliability_6h == Decimal("75.000")
        assert result.reliability_24h == Decimal("66.667")

    def test_empty_inner_window_is_null_not_zero(self) -> None:
        items = [_obs(12.0, success=True, mtproto=2100.0)]
        result = score_observations(1, items, now=NOW)
        assert result.sample_count_1h == 0
        assert result.reliability_1h is None
        assert result.sample_count_6h == 0
        assert result.reliability_6h is None
        assert result.sample_count_24h == 1
        assert result.reliability_24h == Decimal("100.000")
        assert result.freshness is ScoreFreshness.AGING


class TestPercentilesAndMean:
    def test_linear_interpolation_percentiles_are_order_independent(self) -> None:
        latencies = [5000.0, 1000.0, 4000.0, 2000.0, 3000.0]
        items = [_obs(0.01 * i, success=True, mtproto=ms) for i, ms in enumerate(latencies)]
        result = score_observations(1, items, now=NOW)
        # n=5, rank_p50 = 0.5*(n-1)=2 → 3000; rank_p95=0.95*4=3.8 → 4000*0.2+5000*0.8
        assert result.latency_p50_ms == Decimal("3000.000")
        assert result.latency_p95_ms == Decimal("4800.000")
        shuffled = list(reversed(items))
        again = score_observations(1, shuffled, now=NOW)
        assert again.latency_p50_ms == result.latency_p50_ms
        assert again.latency_p95_ms == result.latency_p95_ms

    def test_mean_mtproto_ignores_tcp_and_failures(self) -> None:
        items = [
            _obs(0.0, success=True, mtproto=2000.0, tcp=5.0),
            _obs(0.0, success=True, mtproto=4000.0, tcp=5.0),
            _obs(0.0, success=False, mtproto=50.0, category=ErrorCategory.TCP_TIMEOUT),
        ]
        result = score_observations(1, items, now=NOW)
        assert result.mean_mtproto_ms == Decimal("3000.000")


class TestFreshness:
    def test_recent_aging_and_stale(self) -> None:
        recent = score_observations(1, [_obs(1.0, success=True, mtproto=2100.0)], now=NOW)
        aging = score_observations(2, [_obs(12.0, success=True, mtproto=2100.0)], now=NOW)
        stale = score_observations(3, _n_failure(3), now=NOW)
        assert recent.freshness is ScoreFreshness.RECENT
        assert aging.freshness is ScoreFreshness.AGING
        assert stale.freshness is ScoreFreshness.STALE
        assert recent.last_success_at == NOW - timedelta(hours=1.0)
        assert aging.last_success_at == NOW - timedelta(hours=12.0)
        assert stale.last_success_at is None


class TestWorkedExampleAndMonotonicity:
    def test_ten_recent_successes_at_2100ms_match_the_documented_v1_example(self) -> None:
        items = [_obs(0.0, success=True, mtproto=2100.0) for _ in range(10)]
        result = score_observations(1, items, now=NOW)
        reliability = 100.0 * (10.0 + 1.0) / (10.0 + 2.0)
        latency = 100.0 * (1.0 - 2100.0 / 8000.0)
        combined = 0.75 * reliability + 0.25 * latency
        expected = combined * (10.0 / 20.0)
        assert result.reliability_score == Decimal("91.667")
        assert result.latency_score == Decimal("73.750")
        assert result.confidence_factor == pytest.approx(0.5)
        assert expected == pytest.approx(43.59375)
        assert result.score == Decimal("43.594")

    def test_more_identical_successes_do_not_lower_the_score(self) -> None:
        fewer = score_observations(1, _n_success(5, hours_ago=0.0, mtproto=2100.0), now=NOW)
        more = score_observations(2, _n_success(20, hours_ago=0.0, mtproto=2100.0), now=NOW)
        assert more.score > fewer.score
        assert more.confidence_factor > fewer.confidence_factor
