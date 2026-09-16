"""Unit tests for the read-only ranking HTTP transport. No PostgreSQL."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from core.models import SCORING_VERSION_V1
from modules.api.app import PROCESS_NAME, REQUEST_ID_HEADER, create_app, resolve_request_id
from modules.ranking.models import ProxyListing, RankingPage
from modules.ranking.policy import DEFAULT_LIMIT, MAX_LIMIT
from tests.conftest import make_settings

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
SECRET = "dd" + "ab" * 16


def _listing(*, proxy_id: int = 1, score: str = "85.000") -> ProxyListing:
    return ProxyListing(
        proxy_id=proxy_id,
        server="203.0.113.10",
        port=443,
        secret_type="dd",
        score=Decimal(score),
        reliability_1h=Decimal("100.00"),
        reliability_6h=Decimal("95.00"),
        reliability_24h=Decimal("90.00"),
        latency_p50_ms=Decimal("2100.000"),
        latency_p95_ms=Decimal("2500.000"),
        sample_count_24h=8,
        scoring_version=SCORING_VERSION_V1,
        scored_at=NOW,
    )


def _page(*items: ProxyListing, limit: int = 20) -> RankingPage:
    return RankingPage(
        items=items,
        as_of=NOW,
        limit=limit,
        max_age_hours=24.0,
        scoring_version=SCORING_VERSION_V1,
    )


@asynccontextmanager
async def api_client(database: Any | None = None) -> AsyncIterator[tuple[AsyncClient, Any]]:
    db = database
    if db is None:
        db = MagicMock()
        db.is_reachable = AsyncMock(return_value=True)
        db.dispose = AsyncMock()
    app = create_app(settings=make_settings(), database=db)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, db


class TestHealth:
    @pytest.mark.asyncio
    async def test_healthz_is_ok_without_touching_the_database(self) -> None:
        async with api_client() as (client, db):
            response = await client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        db.is_reachable.assert_not_called()
        assert response.headers.get("cache-control") == "no-store"

    @pytest.mark.asyncio
    async def test_healthz_contains_no_proxy_fields(self) -> None:
        async with api_client() as (client, _db):
            body = (await client.get("/healthz")).json()
        assert "items" not in body
        assert "secret" not in json.dumps(body).lower()


class TestReady:
    @pytest.mark.asyncio
    async def test_readyz_ok_when_database_answers(self) -> None:
        async with api_client() as (client, db):
            response = await client.get("/readyz")
        assert response.status_code == 200
        assert response.json() == {"status": "ready"}
        db.is_reachable.assert_awaited()

    @pytest.mark.asyncio
    async def test_readyz_unavailable_when_database_is_down(self) -> None:
        db = MagicMock()
        db.is_reachable = AsyncMock(return_value=False)
        db.dispose = AsyncMock()
        async with api_client(db) as (client, _db):
            response = await client.get("/readyz")
        assert response.status_code == 503
        assert response.json() == {"status": "not_ready"}
        assert "sql" not in json.dumps(response.json()).lower()
        assert SECRET not in response.text

    @pytest.mark.asyncio
    async def test_empty_ranking_is_not_a_readiness_failure(self) -> None:
        with patch(
            "modules.api.app.RankingService.list_top",
            new_callable=AsyncMock,
            return_value=_page(),
        ):
            async with api_client() as (client, _db):
                ready = await client.get("/readyz")
                ranked = await client.get("/v1/proxies")
        assert ready.status_code == 200
        assert ranked.status_code == 200
        assert ranked.json()["items"] == []


class TestRankingEndpoint:
    @pytest.mark.asyncio
    async def test_empty_result(self) -> None:
        with patch(
            "modules.api.app.RankingService.list_top",
            new_callable=AsyncMock,
            return_value=_page(),
        ) as mocked:
            async with api_client() as (client, _db):
                response = await client.get("/v1/proxies")
        assert response.status_code == 200
        body = response.json()
        assert body["items"] == []
        assert body["count"] == 0
        assert body["limit"] == DEFAULT_LIMIT
        assert body["scoring_version"] == SCORING_VERSION_V1
        assert "as_of" not in body
        mocked.assert_awaited()
        called = mocked.await_args
        assert called is not None
        assert called.kwargs["limit"] == DEFAULT_LIMIT

    @pytest.mark.asyncio
    async def test_one_and_many_preserve_order(self) -> None:
        page = _page(_listing(proxy_id=2, score="90.000"), _listing(proxy_id=1, score="10.000"))
        with patch(
            "modules.api.app.RankingService.list_top",
            new_callable=AsyncMock,
            return_value=page,
        ):
            async with api_client() as (client, _db):
                body = (await client.get("/v1/proxies")).json()
        assert [item["proxy_id"] for item in body["items"]] == [2, 1]
        assert [item["score"] for item in body["items"]] == ["90.000", "10.000"]

    @pytest.mark.asyncio
    async def test_limit_is_forwarded(self) -> None:
        with patch(
            "modules.api.app.RankingService.list_top",
            new_callable=AsyncMock,
            return_value=_page(limit=1),
        ) as mocked:
            async with api_client() as (client, _db):
                await client.get("/v1/proxies", params={"limit": 1})
                await client.get("/v1/proxies", params={"limit": MAX_LIMIT})
        limits = [call.kwargs["limit"] for call in mocked.await_args_list]
        assert limits == [1, MAX_LIMIT]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("limit", [0, -1, 101, "nope", 1.5])
    async def test_invalid_limit_is_400(self, limit: object) -> None:
        async with api_client() as (client, _db):
            response = await client.get("/v1/proxies", params={"limit": str(limit)})
        assert response.status_code == 400
        assert response.json() == {"detail": "invalid request"}
        assert SECRET not in response.text
        assert "traceback" not in response.text.lower()

    @pytest.mark.asyncio
    async def test_as_of_is_not_a_public_parameter(self) -> None:
        with patch(
            "modules.api.app.RankingService.list_top",
            new_callable=AsyncMock,
            return_value=_page(),
        ) as mocked:
            async with api_client() as (client, _db):
                response = await client.get(
                    "/v1/proxies", params={"as_of": "2020-01-01T00:00:00+00:00"}
                )
        assert response.status_code == 200
        called = mocked.await_args
        assert called is not None
        assert "as_of" not in called.kwargs

    @pytest.mark.asyncio
    async def test_cache_control_on_ranking(self) -> None:
        with patch(
            "modules.api.app.RankingService.list_top",
            new_callable=AsyncMock,
            return_value=_page(),
        ):
            async with api_client() as (client, _db):
                response = await client.get("/v1/proxies")
        assert response.headers.get("cache-control") == "public, max-age=30"


class TestSecurity:
    @pytest.mark.asyncio
    async def test_secret_never_appears_in_body_headers_or_logs(
        self, json_logs: pytest.CaptureFixture[str]
    ) -> None:
        page = _page(_listing())
        with patch(
            "modules.api.app.RankingService.list_top",
            new_callable=AsyncMock,
            return_value=page,
        ):
            async with api_client() as (client, _db):
                response = await client.get("/v1/proxies")
        payload = response.text + json.dumps(dict(response.headers)) + json_logs.readouterr().out
        assert SECRET not in payload
        assert "tg://" not in payload
        assert "secret=" not in payload
        body = response.json()["items"][0]
        assert "secret" not in body
        assert "fingerprint" not in body

    @pytest.mark.asyncio
    async def test_service_failure_is_generic_and_secret_free(
        self, json_logs: pytest.CaptureFixture[str]
    ) -> None:
        boom = RuntimeError(f"select failed secret={SECRET} tg://proxy?secret={SECRET}")
        with patch(
            "modules.api.app.RankingService.list_top",
            new_callable=AsyncMock,
            side_effect=boom,
        ):
            async with api_client() as (client, _db):
                response = await client.get("/v1/proxies")
        assert response.status_code == 500
        assert response.json() == {"detail": "internal error"}
        rendered = response.text + json_logs.readouterr().out
        assert SECRET not in rendered
        assert "traceback" not in response.text.lower()
        assert "sqlalchemy" not in response.text.lower()


class TestRequestId:
    def test_generated_and_valid_ids(self) -> None:
        minted = resolve_request_id(None)
        assert minted.isalnum()
        assert resolve_request_id("abc-123") == "abc-123"
        assert resolve_request_id("  ok_id  ") == "ok_id"

    def test_oversized_and_unsafe_ids_are_replaced(self) -> None:
        huge = "a" * 65
        assert resolve_request_id(huge) != huge
        injected = "abc\nSTAT-200"
        assert "\n" not in resolve_request_id(injected)

    @pytest.mark.asyncio
    async def test_header_round_trip(self) -> None:
        async with api_client() as (client, _db):
            generated = await client.get("/healthz")
            echoed = await client.get("/healthz", headers={REQUEST_ID_HEADER: "req-42"})
        assert generated.headers[REQUEST_ID_HEADER]
        assert echoed.headers[REQUEST_ID_HEADER] == "req-42"

    @pytest.mark.asyncio
    async def test_oversized_header_is_replaced(self) -> None:
        async with api_client() as (client, _db):
            response = await client.get("/healthz", headers={REQUEST_ID_HEADER: "x" * 200})
        assert response.headers[REQUEST_ID_HEADER] != "x" * 200
        assert len(response.headers[REQUEST_ID_HEADER]) <= 64


class TestGenericErrors:
    @pytest.mark.asyncio
    async def test_unknown_path_is_404(self) -> None:
        async with api_client() as (client, _db):
            response = await client.get("/no-such-route")
        assert response.status_code == 404
        assert response.json() == {"detail": "not found"}

    @pytest.mark.asyncio
    async def test_wrong_method_is_405(self) -> None:
        async with api_client() as (client, _db):
            response = await client.post("/v1/proxies")
        assert response.status_code == 405
        assert response.json() == {"detail": "method not allowed"}


class TestOpenAPI:
    @pytest.mark.asyncio
    async def test_schema_has_no_secrets_or_orm_models(self) -> None:
        async with api_client() as (client, _db):
            spec = (await client.get("/openapi.json")).json()
        dumped = json.dumps(spec)
        assert "database_url" not in dumped
        assert "ProxyScore" not in dumped
        assert "ProxyObservation" not in dumped
        assert "tg://" not in dumped
        assert "reveal" not in dumped
        schemas = spec["components"]["schemas"]
        listing = schemas["ProxyListingResponse"]["properties"]
        assert "secret" not in listing
        assert "fingerprint" not in listing
        assert "as_of" not in schemas["RankingResponse"]["properties"]
        assert "/v1/proxies" in spec["paths"]
        params = spec["paths"]["/v1/proxies"]["get"].get("parameters", [])
        assert all(item.get("name") != "as_of" for item in params)
        assert "as_of" not in json.dumps(spec["paths"]["/v1/proxies"])


class TestProcessSeparation:
    def test_api_module_does_not_import_other_workers(self) -> None:
        import ast
        import pathlib

        source = pathlib.Path(__file__).resolve().parents[1] / "src" / "workers" / "api.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module.split(".")[0])
        assert "workers" not in names
        assert PROCESS_NAME == "ranking-api"
