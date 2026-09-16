"""Unit tests for :mod:`modules.discovery.http` -- SSRF protection and safe fetching."""

from __future__ import annotations

import asyncio
import socket
from typing import Any

import httpx
import pytest

from modules.discovery.http import (
    HttpFetchError,
    ResponseSizeExceededError,
    SSRFProtectionError,
    SsrfSafeHttpClient,
    _ensure_getaddrinfo_patch,
    _pin_hosts,
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
        ips = await validate_ssrf_url(safe_ip_url)
        assert ips

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
            "http://[::ffff:127.0.0.1]/",
            "http://[::ffff:169.254.169.254]/",
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

    async def test_rejects_mixed_public_and_private_dns(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        loop = asyncio.get_running_loop()

        async def fake_getaddrinfo(*_args: Any, **_kwargs: Any) -> list[Any]:
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 80)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 80)),
            ]

        monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)

        with pytest.raises(SSRFProtectionError, match="disallowed IP"):
            await validate_ssrf_url("http://dual-homed.example/proxies")


class TestSsrfSafeHttpClient:
    async def test_enforces_max_response_bytes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import modules.discovery.http as http_module

        async def fake_validate(_url: str) -> tuple[str, ...]:
            return ("93.184.216.34",)

        monkeypatch.setattr(http_module, "validate_ssrf_url", fake_validate)

        large_payload = b"A" * 250

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=large_payload)

        client = SsrfSafeHttpClient(max_response_bytes=100, transport=httpx.MockTransport(handler))
        with pytest.raises(ResponseSizeExceededError, match="exceeded limit of 100 bytes"):
            await client.get_text("http://example.com/large")

    async def test_http_status_error_is_wrapped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import modules.discovery.http as http_module

        async def fake_validate(_url: str) -> tuple[str, ...]:
            return ("8.8.8.8",)

        monkeypatch.setattr(http_module, "validate_ssrf_url", fake_validate)

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, content=b"missing")

        client = SsrfSafeHttpClient(transport=httpx.MockTransport(handler))
        with pytest.raises(HttpFetchError, match="HTTP 404"):
            await client.get_text("http://example.com/missing")

    async def test_timeout_is_wrapped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import modules.discovery.http as http_module

        async def fake_validate(_url: str) -> tuple[str, ...]:
            return ("8.8.8.8",)

        monkeypatch.setattr(http_module, "validate_ssrf_url", fake_validate)

        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow")

        client = SsrfSafeHttpClient(transport=httpx.MockTransport(handler))
        with pytest.raises(HttpFetchError, match="ReadTimeout"):
            await client.get_text("http://example.com/slow")

    async def test_redirect_to_loopback_is_blocked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        loop = asyncio.get_running_loop()

        async def fake_getaddrinfo(_host: str, port: int, *_args: Any, **_kwargs: Any) -> list[Any]:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port))]

        monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/start":
                return httpx.Response(302, headers={"Location": "http://127.0.0.1/secret"})
            return httpx.Response(200, content=b"leaked")

        client = SsrfSafeHttpClient(transport=httpx.MockTransport(handler))
        with pytest.raises(SSRFProtectionError):
            await client.get_text("http://example.com/start")

    async def test_too_many_redirects(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import modules.discovery.http as http_module

        async def fake_validate(_url: str) -> tuple[str, ...]:
            return ("8.8.8.8",)

        monkeypatch.setattr(http_module, "validate_ssrf_url", fake_validate)

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"Location": "/next"})

        client = SsrfSafeHttpClient(max_redirects=2, transport=httpx.MockTransport(handler))
        with pytest.raises(HttpFetchError, match="maximum redirect"):
            await client.get_text("http://example.com/start")

    async def test_error_message_does_not_include_secret(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import modules.discovery.http as http_module

        secret = "000102030405060708090a0b0c0d0e0f"

        async def fake_validate(_url: str) -> tuple[str, ...]:
            return ("8.8.8.8",)

        monkeypatch.setattr(http_module, "validate_ssrf_url", fake_validate)

        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(f"failed connecting to https://x/?secret={secret}")

        client = SsrfSafeHttpClient(transport=httpx.MockTransport(handler))
        with pytest.raises(HttpFetchError) as exc_info:
            await client.get_text(f"http://example.com/list?secret={secret}")
        assert secret not in str(exc_info.value)

    async def test_cancel_during_fetch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import modules.discovery.http as http_module

        async def fake_validate(_url: str) -> tuple[str, ...]:
            return ("8.8.8.8",)

        monkeypatch.setattr(http_module, "validate_ssrf_url", fake_validate)
        started = asyncio.Event()

        async def handler(_request: httpx.Request) -> httpx.Response:
            started.set()
            await asyncio.sleep(30)
            return httpx.Response(200, content=b"late")

        client = SsrfSafeHttpClient(transport=httpx.MockTransport(handler))
        async with client:
            task = asyncio.create_task(client.get_text("http://example.com/hang"))
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        await client.aclose()

    def test_pinned_getaddrinfo_ignores_later_private_answer(self) -> None:
        _ensure_getaddrinfo_patch()
        with _pin_hosts({"rebind.example": ("8.8.8.8",)}):
            results = socket.getaddrinfo("rebind.example", 443)
        assert results
        assert results[0][4][0] == "8.8.8.8"
