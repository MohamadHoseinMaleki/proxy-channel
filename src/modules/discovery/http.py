"""SSRF-safe asynchronous HTTP client for source fetching.

Protects against:
* Localhost, loopback, and private IPv4/IPv6 address ranges.
* Link-local (including 169.254.169.254 cloud metadata service).
* Multicast, unspecified, and reserved IP addresses.
* Internal DNS hostnames and metadata endpoints.
* Unsafe redirect chains (every hop is validated against SSRF rules).
* Unbounded response bodies (streaming with max byte limits).
* Slow loris / hung connections (strict timeouts).
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import urllib.parse
from typing import Final

import httpx

from core.logger import get_logger
from modules.discovery.normalizer import is_disallowed_ip, validate_hostname

__all__ = [
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

_logger = get_logger("modules.discovery.http")

DEFAULT_TIMEOUT_SECONDS: Final = 15.0
DEFAULT_MAX_RESPONSE_BYTES: Final = 2 * 1024 * 1024  # 2 MiB
DEFAULT_MAX_REDIRECTS: Final = 3
DEFAULT_USER_AGENT: Final = (
    "mtproto-platform/0.1.0 (+https://github.com/MohamadHoseinMaleki/proxy-channel)"
)


class HttpFetchError(Exception):
    """Base exception for discovery HTTP fetching errors."""


class SSRFProtectionError(HttpFetchError):
    """Raised when an HTTP target URL violates SSRF safety constraints."""


class ResponseSizeExceededError(HttpFetchError):
    """Raised when a remote HTTP response exceeds the allowed byte limit."""


async def validate_ssrf_url(url: str) -> None:
    """Validate a URL against SSRF rules, including DNS resolution check.

    Raises :class:`SSRFProtectionError` if the URL is unsafe.
    """
    try:
        parsed = urllib.parse.urlsplit(url)
    except Exception as exc:
        msg = f"Invalid URL syntax: {exc}"
        raise SSRFProtectionError(msg) from None

    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        msg = f"Disallowed URL scheme {scheme!r} (must be http or https)"
        raise SSRFProtectionError(msg)

    hostname = parsed.hostname
    if not hostname:
        msg = "URL hostname must not be empty"
        raise SSRFProtectionError(msg)

    # 1. Test for direct IP literal
    try:
        ip = ipaddress.ip_address(hostname)
        if is_disallowed_ip(ip):
            msg = f"Target IP {hostname} is in a disallowed network range"
            raise SSRFProtectionError(msg)
        return
    except ValueError:
        pass

    # 2. Test for disallowed hostname syntax
    if not validate_hostname(hostname):
        msg = f"Target hostname {hostname!r} is not a valid public hostname"
        raise SSRFProtectionError(msg)

    # 3. Resolve DNS asynchronously to verify underlying IP addresses
    port = parsed.port or (443 if scheme == "https" else 80)
    loop = asyncio.get_running_loop()
    try:
        addr_info = await loop.getaddrinfo(
            hostname,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        msg = f"DNS resolution failed for {hostname}: {exc}"
        raise HttpFetchError(msg) from None

    if not addr_info:
        msg = f"No DNS records found for {hostname}"
        raise HttpFetchError(msg)

    for item in addr_info:
        sockaddr = item[4]
        ip_str = sockaddr[0]
        try:
            resolved_ip = ipaddress.ip_address(ip_str)
            if is_disallowed_ip(resolved_ip):
                msg = f"Hostname {hostname} resolved to disallowed IP {ip_str}"
                raise SSRFProtectionError(msg)
        except ValueError:
            continue


class SsrfSafeHttpClient:
    """Async HTTP client enforcing strict SSRF checks, redirect bounds, and body limits."""

    def __init__(
        self,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes
        self.max_redirects = max_redirects
        self.user_agent = user_agent

    async def get_text(self, url: str) -> str:
        """Fetch URL content as text with SSRF validation and redirect protection."""
        current_url = url
        headers = {"User-Agent": self.user_agent, "Accept": "*/*"}
        timeout = httpx.Timeout(self.timeout_seconds)

        for _redirect_count in range(self.max_redirects + 1):
            await validate_ssrf_url(current_url)

            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                try:
                    async with client.stream("GET", current_url, headers=headers) as response:
                        if response.status_code in (301, 302, 303, 307, 308):
                            location = response.headers.get("Location")
                            if not location:
                                msg = (
                                    f"Redirect status {response.status_code} "
                                    "missing Location header"
                                )
                                raise HttpFetchError(msg)
                            current_url = urllib.parse.urljoin(current_url, location)
                            continue

                        response.raise_for_status()

                        # Read stream with strict size limit
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
                        except Exception:
                            return raw_content.decode("utf-8", errors="replace")

                except (httpx.TimeoutException, httpx.RequestError) as exc:
                    msg = f"HTTP request failed: {type(exc).__name__}: {exc}"
                    raise HttpFetchError(msg) from None

        msg = f"Exceeded maximum redirect count of {self.max_redirects}"
        raise HttpFetchError(msg)
