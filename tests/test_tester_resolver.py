"""Unit tests for DNS resolution, SSRF protection, and destination pinning."""

from __future__ import annotations

import socket
from unittest.mock import AsyncMock, patch

import pytest

from modules.tester.resolver import (
    DestinationBlockedError,
    DnsResolutionError,
    resolve_and_validate_destination,
)


class TestDirectIpLiterals:
    @pytest.mark.asyncio
    async def test_valid_public_ip_is_pinned_without_dns(self) -> None:
        pinned, all_ips = await resolve_and_validate_destination("1.1.1.1", 443)
        assert pinned == "1.1.1.1"
        assert all_ips == ["1.1.1.1"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "blocked_ip",
        [
            "127.0.0.1",
            "127.0.1.1",
            "10.0.0.1",
            "172.16.0.1",
            "192.168.1.1",
            "169.254.169.254",  # AWS/GCP/Azure link-local metadata
            "0.0.0.0",
            "::1",
            "fe80::1",
            "fc00::1",
            "224.0.0.1",  # multicast
            "::",  # unspecified
            "::ffff:127.0.0.1",  # IPv4-mapped loopback
            "::ffff:169.254.169.254",  # IPv4-mapped metadata
            "::ffff:10.0.0.1",  # IPv4-mapped private
        ],
    )
    async def test_disallowed_ip_literals_are_rejected(self, blocked_ip: str) -> None:
        with pytest.raises(DestinationBlockedError, match="disallowed network range"):
            await resolve_and_validate_destination(blocked_ip, 443)

    @pytest.mark.asyncio
    async def test_hostname_resolving_to_ipv4_mapped_loopback_is_blocked(self) -> None:
        fake_addrinfo = [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::ffff:127.0.0.1", 443, 0, 0)),
        ]
        with patch("asyncio.get_running_loop") as mock_get_loop:
            mock_loop = AsyncMock()
            mock_loop.getaddrinfo.return_value = fake_addrinfo
            mock_get_loop.return_value = mock_loop
            with pytest.raises(DestinationBlockedError, match="disallowed IP"):
                await resolve_and_validate_destination("mapped.example.com", 443)


class TestHostnameValidationAndDns:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "invalid_host",
        [
            "invalid..host",
            "-leading-hyphen.com",
            "trailing-hyphen-.com",
            "spaces in name.com",
        ],
    )
    async def test_syntactically_invalid_hostnames_rejected(self, invalid_host: str) -> None:
        with pytest.raises(DestinationBlockedError, match="not a valid public hostname"):
            await resolve_and_validate_destination(invalid_host, 443)

    @pytest.mark.asyncio
    async def test_successful_dns_resolution_pins_first_ip(self) -> None:
        fake_addrinfo = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.35", 443)),
        ]
        with patch("asyncio.get_running_loop") as mock_get_loop:
            mock_loop = AsyncMock()
            mock_loop.getaddrinfo.return_value = fake_addrinfo
            mock_get_loop.return_value = mock_loop

            pinned, all_ips = await resolve_and_validate_destination("example.com", 443)
            assert pinned == "93.184.216.34"
            assert all_ips == ["93.184.216.34", "93.184.216.35"]

    @pytest.mark.asyncio
    async def test_dns_rebinding_or_private_resolution_rejected(self) -> None:
        # Threat: hostname resolves to private IP or metadata service
        fake_addrinfo = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 443)),
        ]
        with patch("asyncio.get_running_loop") as mock_get_loop:
            mock_loop = AsyncMock()
            mock_loop.getaddrinfo.return_value = fake_addrinfo
            mock_get_loop.return_value = mock_loop

            with pytest.raises(DestinationBlockedError, match="disallowed IP"):
                await resolve_and_validate_destination("evil-metadata.com", 443)

    @pytest.mark.asyncio
    async def test_dns_resolution_gaierror_raises_dns_resolution_error(self) -> None:
        with patch("asyncio.get_running_loop") as mock_get_loop:
            mock_loop = AsyncMock()
            mock_loop.getaddrinfo.side_effect = socket.gaierror("Name or service not known")
            mock_get_loop.return_value = mock_loop

            with pytest.raises(DnsResolutionError, match="DNS resolution failed"):
                await resolve_and_validate_destination("nonexistent.invalid", 443)

    @pytest.mark.asyncio
    async def test_dns_empty_records_raises_dns_resolution_error(self) -> None:
        with patch("asyncio.get_running_loop") as mock_get_loop:
            mock_loop = AsyncMock()
            mock_loop.getaddrinfo.return_value = []
            mock_get_loop.return_value = mock_loop

            with pytest.raises(DnsResolutionError, match="No DNS records found"):
                await resolve_and_validate_destination("empty.invalid", 443)
