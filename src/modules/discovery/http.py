"""SSRF-safe asynchronous HTTP client for source fetching.

Protects against:
* Localhost, loopback, and private IPv4/IPv6 address ranges.
* Link-local (including 169.254.169.254 cloud metadata service).
* Multicast, unspecified, and reserved IP addresses.
* Internal DNS hostnames and metadata endpoints.
* Unsafe redirect chains (every hop is validated against SSRF rules).
* DNS rebinding between resolve and connect (the request is rewritten to the
  validated IP literal; a task-local getaddrinfo pin is defence in depth).
* Unbounded response bodies (streaming with max byte limits).
* Slow loris / hung connections (strict connect/read timeouts).
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import urllib.parse
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from typing import Any, Final

import httpx

from core.logger import safe_error_message, scrub_secrets
from modules.discovery.normalizer import is_disallowed_ip, validate_hostname

__all__ = [
    "DEFAULT_CONNECT_TIMEOUT_SECONDS",
    "DEFAULT_MAX_REDIRECTS",
    "DEFAULT_MAX_RESPONSE_BYTES",
    "DEFAULT_TIMEOUT_SECONDS",
    "DEFAULT_USER_AGENT",
    "HttpFetchError",
    "ResponseSizeExceededError",
    "SSRFProtectionError",
    "SsrfSafeHttpClient",
    "validate_ssrf_url",
]

DEFAULT_TIMEOUT_SECONDS: Final = 15.0
DEFAULT_CONNECT_TIMEOUT_SECONDS: Final = 5.0
DEFAULT_MAX_RESPONSE_BYTES: Final = 2 * 1024 * 1024  # 2 MiB
DEFAULT_MAX_REDIRECTS: Final = 3
DEFAULT_USER_AGENT: Final = (
    "mtproto-platform/0.1.0 (+https://github.com/MohamadHoseinMaleki/proxy-channel)"
)

_REDIRECT_STATUSES: Final = frozenset({301, 302, 303, 307, 308})

#: Task-local map of hostname → public IPs that httpx is allowed to connect to.
_PINNED_HOSTS: ContextVar[dict[str, tuple[str, ...]] | None] = ContextVar(
    "discovery_ssrf_pinned_hosts", default=None
)

_REAL_GETADDRINFO = socket.getaddrinfo
_GETADDRINFO_PATCHED = False


class HttpFetchError(Exception):
    """Base exception for discovery HTTP fetching errors."""


class SSRFProtectionError(HttpFetchError):
    """Raised when an HTTP target URL violates SSRF safety constraints."""


class ResponseSizeExceededError(HttpFetchError):
    """Raised when a remote HTTP response exceeds the allowed byte limit."""


def _hostname_key(host: str) -> str:
    if host.startswith("[") and host.endswith("]"):
        return host[1:-1]
    return host


def _gai_host_key(host: object) -> str | None:
    """Normalise a getaddrinfo host (str, bytes, or bracketed) to a pin key.

    httpx/anyio encode the hostname to ASCII/IDNA **bytes** before calling
    ``loop.getaddrinfo`` → ``socket.getaddrinfo``. A str-only pin map would
    miss that lookup and fall through to a fresh unpinned resolve.
    """
    if isinstance(host, bytes):
        try:
            host = host.decode("ascii")
        except UnicodeDecodeError:
            return None
    if not isinstance(host, str):
        return None
    return _hostname_key(host)


def _synthetic_addrinfo(
    ips: tuple[str, ...],
    port: Any,
    family: int,
    socktype: int,
    proto: int,
) -> list[Any]:
    port_num = int(port) if port else 0
    results: list[Any] = []
    for ip_str in ips:
        ip_obj = ipaddress.ip_address(ip_str)
        if ip_obj.version == 6:
            sockaddr: tuple[Any, ...] = (ip_str, port_num, 0, 0)
            af = socket.AF_INET6
        else:
            sockaddr = (ip_str, port_num)
            af = socket.AF_INET
        if family not in (0, socket.AF_UNSPEC, af):
            continue
        results.append((af, socktype or socket.SOCK_STREAM, proto or 6, "", sockaddr))
    return results


def _pinned_getaddrinfo(
    host: str,
    port: Any,
    family: int = 0,
    type: int = 0,
    proto: int = 0,
    flags: int = 0,
) -> list[Any]:
    pinned = _PINNED_HOSTS.get()
    key = _gai_host_key(host)
    if pinned is not None and key is not None and key in pinned:
        results = _synthetic_addrinfo(pinned[key], port, family, type, proto)
        if not results:
            msg = f"Pinned DNS produced no addresses for {key}"
            raise socket.gaierror(socket.EAI_NONAME, msg)
        return results
    return _REAL_GETADDRINFO(host, port, family, type, proto, flags)


def _url_with_pinned_ip(url: str, ip: str) -> str:
    """Rewrite *url* so the transport connects to ``ip`` without a second DNS lookup."""
    parsed = urllib.parse.urlsplit(url)
    host = f"[{ip}]" if ":" in ip else ip
    netloc = f"{host}:{parsed.port}" if parsed.port else host
    return urllib.parse.urlunsplit(
        (parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment)
    )


def _host_header(parsed: urllib.parse.SplitResult) -> str:
    hostname = parsed.hostname or ""
    if not hostname:
        return ""
    default_port = 443 if parsed.scheme == "https" else 80
    if parsed.port and parsed.port != default_port:
        if ":" in hostname:
            return f"[{hostname}]:{parsed.port}"
        return f"{hostname}:{parsed.port}"
    return hostname


def _ensure_getaddrinfo_patch() -> None:
    global _GETADDRINFO_PATCHED
    if _GETADDRINFO_PATCHED:
        return
    socket.getaddrinfo = _pinned_getaddrinfo  # type: ignore[assignment]
    _GETADDRINFO_PATCHED = True


@contextmanager
def _pin_hosts(mapping: dict[str, tuple[str, ...]]) -> Iterator[None]:
    token = _PINNED_HOSTS.set(mapping)
    try:
        yield
    finally:
        _PINNED_HOSTS.reset(token)


def _split_url(url: str) -> urllib.parse.SplitResult:
    try:
        return urllib.parse.urlsplit(url)
    except Exception as exc:
        msg = f"Invalid URL syntax: {type(exc).__name__}"
        raise SSRFProtectionError(msg) from None


async def _public_ips_for_host(hostname: str, port: int) -> tuple[str, ...]:
    """Resolve ``hostname`` and return only public addresses.

    Raises :class:`SSRFProtectionError` if any record is disallowed, so a mixed
    public+private answer cannot be used to reach metadata via Happy Eyeballs.
    """
    loop = asyncio.get_running_loop()
    try:
        addr_info = await loop.getaddrinfo(
            hostname,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        msg = f"DNS resolution failed for {hostname}: {type(exc).__name__}"
        raise HttpFetchError(msg) from None

    if not addr_info:
        msg = f"No DNS records found for {hostname}"
        raise HttpFetchError(msg)

    ips: list[str] = []
    seen: set[str] = set()
    for item in addr_info:
        sockaddr = item[4]
        raw_ip = sockaddr[0]
        try:
            resolved_ip = ipaddress.ip_address(str(raw_ip))
        except ValueError:
            continue
        ip_str = str(resolved_ip)
        if is_disallowed_ip(resolved_ip):
            msg = f"Hostname {hostname} resolved to disallowed IP {ip_str}"
            raise SSRFProtectionError(msg)
        if ip_str not in seen:
            seen.add(ip_str)
            ips.append(ip_str)
    if not ips:
        msg = f"No usable DNS records found for {hostname}"
        raise HttpFetchError(msg)
    return tuple(ips)


async def validate_ssrf_url(url: str) -> tuple[str, ...]:
    """Validate a URL against SSRF rules, including DNS resolution.

    Returns the public IP literals that the host resolved to (or the IP
    itself when the host is a literal). Raises :class:`SSRFProtectionError`
    if the URL is unsafe.
    """
    parsed = _split_url(url)
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        msg = f"Disallowed URL scheme {scheme!r} (must be http or https)"
        raise SSRFProtectionError(msg)

    hostname = parsed.hostname
    if not hostname:
        msg = "URL hostname must not be empty"
        raise SSRFProtectionError(msg)

    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        ip = None

    if ip is not None:
        if is_disallowed_ip(ip):
            msg = f"Target IP {hostname} is in a disallowed network range"
            raise SSRFProtectionError(msg)
        return (str(ip),)

    if not validate_hostname(hostname):
        msg = f"Target hostname {hostname!r} is not a valid public hostname"
        raise SSRFProtectionError(msg)

    port = parsed.port or (443 if scheme == "https" else 80)
    return await _public_ips_for_host(hostname, port)


class SsrfSafeHttpClient:
    """Async HTTP client enforcing strict SSRF checks, redirect bounds, and body limits."""

    def __init__(
        self,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
        user_agent: str = DEFAULT_USER_AGENT,
        transport: httpx.AsyncBaseTransport | httpx.BaseTransport | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.connect_timeout_seconds = connect_timeout_seconds
        self.max_response_bytes = max_response_bytes
        self.max_redirects = max_redirects
        self.user_agent = user_agent
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        _ensure_getaddrinfo_patch()

    def _timeout(self) -> httpx.Timeout:
        connect = min(self.connect_timeout_seconds, self.timeout_seconds)
        return httpx.Timeout(
            connect=connect,
            read=self.timeout_seconds,
            write=self.timeout_seconds,
            pool=self.timeout_seconds,
        )

    def _client_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "timeout": self._timeout(),
            "follow_redirects": False,
            # IP-rewritten origins would otherwise share a keep-alive socket
            # across different original hostnames (wrong Host/SNI on reuse).
            "limits": httpx.Limits(max_keepalive_connections=0, max_connections=20),
        }
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return kwargs

    def _headers(self) -> dict[str, str]:
        return {"User-Agent": self.user_agent, "Accept": "*/*"}

    async def aclose(self) -> None:
        """Close a retained client. Idempotent."""
        client = self._client
        self._client = None
        if client is not None:
            await client.aclose()

    async def __aenter__(self) -> SsrfSafeHttpClient:
        self._client = httpx.AsyncClient(**self._client_kwargs())
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    @asynccontextmanager
    async def _session(self) -> Any:
        if self._client is not None:
            yield self._client
            return
        async with httpx.AsyncClient(**self._client_kwargs()) as client:
            yield client

    async def get_text(self, url: str) -> str:
        """Fetch URL content as text with SSRF validation and redirect protection."""
        current_url = url
        async with self._session() as client:
            for _redirect_count in range(self.max_redirects + 1):
                pinned_ips = await validate_ssrf_url(current_url)
                parsed = urllib.parse.urlsplit(current_url)
                hostname = parsed.hostname or ""
                pinned_ip = pinned_ips[0]
                pin = {hostname: pinned_ips, _hostname_key(hostname): pinned_ips}
                if ":" in hostname:
                    pin[f"[{hostname}]"] = pinned_ips
                request_url = _url_with_pinned_ip(current_url, pinned_ip)
                headers = self._headers()
                host_header = _host_header(parsed)
                if host_header and _hostname_key(hostname) != pinned_ip:
                    headers["Host"] = host_header
                extensions: dict[str, Any] = {}
                if parsed.scheme == "https" and hostname and hostname != pinned_ip:
                    extensions["sni_hostname"] = hostname
                with _pin_hosts(pin):
                    try:
                        async with client.stream(
                            "GET",
                            request_url,
                            headers=headers,
                            extensions=extensions,
                        ) as response:
                            if response.status_code in _REDIRECT_STATUSES:
                                location = response.headers.get("Location")
                                if not location:
                                    msg = (
                                        f"Redirect status {response.status_code} "
                                        "missing Location header"
                                    )
                                    raise HttpFetchError(msg)
                                current_url = urllib.parse.urljoin(current_url, location)
                                continue

                            try:
                                response.raise_for_status()
                            except httpx.HTTPStatusError as exc:
                                msg = f"HTTP {exc.response.status_code}"
                                raise HttpFetchError(msg) from None

                            total_bytes = 0
                            chunks: list[bytes] = []
                            async for chunk in response.aiter_bytes():
                                total_bytes += len(chunk)
                                if total_bytes > self.max_response_bytes:
                                    msg = (
                                        f"Response size exceeded limit of "
                                        f"{self.max_response_bytes} bytes"
                                    )
                                    raise ResponseSizeExceededError(msg)
                                chunks.append(chunk)

                            raw_content = b"".join(chunks)
                            encoding = response.encoding or "utf-8"
                            try:
                                return raw_content.decode(encoding, errors="replace")
                            except LookupError:
                                return raw_content.decode("utf-8", errors="replace")

                    except (SSRFProtectionError, ResponseSizeExceededError, HttpFetchError):
                        raise
                    except (httpx.TimeoutException, httpx.RequestError) as exc:
                        kind = type(exc).__name__
                        msg = f"HTTP request failed: {kind}: {safe_error_message(exc)}"
                        raise HttpFetchError(msg) from None

            msg = f"Exceeded maximum redirect count of {self.max_redirects}"
            raise HttpFetchError(msg)


def safe_source_url(url: str | None) -> str | None:
    """Loggable form of a source URL (credentials scrubbed)."""
    if url is None:
        return None
    return scrub_secrets(url)
