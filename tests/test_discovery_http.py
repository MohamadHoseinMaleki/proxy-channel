"""Unit tests for :mod:`modules.discovery.http` -- SSRF protection and safe fetching."""

from __future__ import annotations

import asyncio
import socket
from typing import Any

import pytest

from modules.discovery.http import (
    ResponseSizeExceededError,
    SSRFProtectionError,
    SsrfSafeHttpClient,
    validate_ssrf_url,
)


class TestValidateSsrfUrl:
    @pytest.mark.parametrize(
        "safe_ip_url",
        [
            "http://8.8.8.8/proxies.txt",
            "https://1.1.1.1/index.html",
        ],
    )
    async def test_accepts_public_ip_urls(self, safe_ip_url: str) -> None:
        await validate_ssrf_url(safe_ip_url)

    @pytest.mark.parametrize(
        "disallowed_url",
        [
            "http://127.0.0.1:8000/secret",
            "http://localhost:5432/",
            "https://10.0.0.1/admin",
            "http://192.168.1.1/router",
            "http://172.16.0.1/",
            "http://169.254.169.254/latest/meta-data/",
            "http://[::1]/",
            "http://[fe80::1]/",
            "ftp://example.com/file",
            "file:///etc/passwd",
            "gopher://127.0.0.1:70/",
            "http://service.internal/",
            "http://metadata.google.internal/",
        ],
    )
    async def test_rejects_disallowed_urls(self, disallowed_url: str) -> None:
        with pytest.raises(SSRFProtectionError):
            await validate_ssrf_url(disallowed_url)

    async def test_rejects_dns_resolving_to_private_ip(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        loop = asyncio.get_running_loop()

        async def fake_getaddrinfo(*_args: Any, **_kwargs: Any) -> list[Any]:
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80)),
            ]

        monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)

        with pytest.raises(SSRFProtectionError, match="disallowed IP"):
            await validate_ssrf_url("http://sneaky-domain.com/proxies")


class TestSsrfSafeHttpClient:
    async def test_enforces_max_response_bytes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = SsrfSafeHttpClient(max_response_bytes=100)

        # Mock validate_ssrf_url to allow through
        import modules.discovery.http as http_module

        monkeypatch.setattr(http_module, "validate_ssrf_url", lambda _url: asyncio.sleep(0))

        # Create a mock httpx transport returning 200 bytes
        import httpx

        large_payload = b"A" * 250

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=large_payload)

        transport = httpx.MockTransport(handler)

        # Monkeypatch httpx.AsyncClient to use this mock transport
        orig_init = httpx.AsyncClient.__init__

        def fake_init(self_client: Any, *args: Any, **kwargs: Any) -> None:
            kwargs["transport"] = transport
            orig_init(self_client, *args, **kwargs)

        monkeypatch.setattr(httpx.AsyncClient, "__init__", fake_init)

        with pytest.raises(ResponseSizeExceededError, match="exceeded limit of 100 bytes"):
            await client.get_text("http://example.com/large")
