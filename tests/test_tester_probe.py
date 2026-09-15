"""Unit tests for the 3-phase MTProto connectivity probe."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telethon.errors import RPCError
from telethon.sessions import MemorySession

from core.models import ErrorCategory
from modules.discovery.models import SecretType
from modules.tester.models import TransportType
from modules.tester.probe import probe_proxy
from modules.tester.resolver import DnsResolutionError

DUMMY_API_ID = 123456
DUMMY_API_HASH = "0123456789abcdef0123456789abcdef"


class TestProbeSsrfAndDns:
    @pytest.mark.asyncio
    async def test_disallowed_destination_returns_ssrf_blocked(self) -> None:
        result = await probe_proxy(
            proxy_id=1,
            server="127.0.0.1",
            port=443,
            secret="dd" + "11" * 16,
            secret_type=SecretType.SECURE_RANDOMIZED,
        )
        assert result.success is False
        assert result.error_category == ErrorCategory.SSRF_BLOCKED
        assert result.tcp_connect_ms is None

    @pytest.mark.asyncio
    async def test_dns_failure_returns_dns_error(self) -> None:
        with patch(
            "modules.tester.probe.resolve_and_validate_destination",
            side_effect=DnsResolutionError("DNS resolution failed"),
        ):
            result = await probe_proxy(
                proxy_id=2,
                server="nonexistent.example.com",
                port=443,
                secret="dd" + "11" * 16,
                secret_type=SecretType.SECURE_RANDOMIZED,
            )
            assert result.success is False
            assert result.error_category == ErrorCategory.DNS_ERROR


class TestProbeTransportCapability:
    @pytest.mark.asyncio
    async def test_fake_tls_secret_returns_unsupported_transport(self) -> None:
        # Pinned public IP to bypass DNS
        result = await probe_proxy(
            proxy_id=3,
            server="1.1.1.1",
            port=443,
            secret="ee" + "aa" * 16 + "7777772e636f6d",
            secret_type=SecretType.FAKE_TLS,
        )
        assert result.success is False
        assert result.error_category == ErrorCategory.UNSUPPORTED_TRANSPORT
        assert result.transport_type == TransportType.FAKE_TLS
        assert "Fake-TLS" in (result.error_message_safe or "")

    @pytest.mark.asyncio
    async def test_invalid_secret_hex_returns_invalid_secret(self) -> None:
        result = await probe_proxy(
            proxy_id=4,
            server="1.1.1.1",
            port=443,
            secret="not_a_valid_hex_string_at_all!",
            secret_type=SecretType.LEGACY,
        )
        assert result.success is False
        assert result.error_category == ErrorCategory.INVALID_SECRET


class TestProbeTcpPhase:
    @pytest.mark.asyncio
    async def test_tcp_connection_refused(self) -> None:
        refused_err = ConnectionRefusedError("Connection refused")
        with patch("asyncio.open_connection", side_effect=refused_err):
            result = await probe_proxy(
                proxy_id=5,
                server="1.1.1.1",
                port=443,
                secret="dd" + "11" * 16,
                secret_type=SecretType.SECURE_RANDOMIZED,
            )
            assert result.success is False
            assert result.error_category == ErrorCategory.TCP_REFUSED
            assert result.tcp_connect_ms is None

    @pytest.mark.asyncio
    async def test_tcp_timeout(self) -> None:
        with patch("asyncio.open_connection", side_effect=TimeoutError("TCP connection timed out")):
            result = await probe_proxy(
                proxy_id=6,
                server="1.1.1.1",
                port=443,
                secret="dd" + "11" * 16,
                secret_type=SecretType.SECURE_RANDOMIZED,
            )
            assert result.success is False
            assert result.error_category == ErrorCategory.TCP_TIMEOUT

    @pytest.mark.asyncio
    async def test_tcp_connection_reset(self) -> None:
        with patch("asyncio.open_connection", side_effect=ConnectionResetError("Reset by peer")):
            result = await probe_proxy(
                proxy_id=7,
                server="1.1.1.1",
                port=443,
                secret="dd" + "11" * 16,
                secret_type=SecretType.SECURE_RANDOMIZED,
            )
            assert result.success is False
            assert result.error_category == ErrorCategory.TCP_RESET


class TestProbeTelethonPhase:
    @pytest.mark.asyncio
    async def test_probe_success(self) -> None:
        # Mock successful TCP connect
        mock_writer = AsyncMock()
        mock_writer.close = MagicMock()
        mock_writer.wait_closed = AsyncMock()

        # Mock Telethon client
        mock_client = AsyncMock()
        mock_client.connect = AsyncMock()
        mock_client.is_user_authorized = AsyncMock(return_value=False)
        mock_client.disconnect = AsyncMock()

        with patch("asyncio.open_connection", return_value=(AsyncMock(), mock_writer)), \
             patch("modules.tester.probe.TelegramClient", return_value=mock_client) as mock_tg_cls:

            result = await probe_proxy(
                proxy_id=8,
                server="1.1.1.1",
                port=443,
                secret="dd" + "11" * 16,
                secret_type=SecretType.SECURE_RANDOMIZED,
                api_id=DUMMY_API_ID,
                api_hash=DUMMY_API_HASH,
            )

            assert result.success is True
            assert result.error_category is None
            assert result.tcp_connect_ms is not None
            assert result.mtproto_connect_ms is not None
            assert result.total_latency_ms is not None

            # Verify ephemeral MemorySession was passed
            call_kwargs = mock_tg_cls.call_args[1]
            session_arg = mock_tg_cls.call_args[0][0]
            assert isinstance(session_arg, MemorySession)
            assert call_kwargs["auto_reconnect"] is False
            assert call_kwargs["timeout"] == 8.0

            # Verify client disconnected cleanly
            mock_client.disconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_probe_mtproto_timeout(self) -> None:
        mock_writer = AsyncMock()
        mock_writer.close = MagicMock()
        mock_writer.wait_closed = AsyncMock()

        mock_client = AsyncMock()
        # Telethon timeout when proxy blackholes or drops traffic
        mock_client.connect = AsyncMock(side_effect=TimeoutError())
        mock_client.disconnect = AsyncMock()

        with patch("asyncio.open_connection", return_value=(AsyncMock(), mock_writer)), \
             patch("modules.tester.probe.TelegramClient", return_value=mock_client):

            result = await probe_proxy(
                proxy_id=9,
                server="1.1.1.1",
                port=443,
                secret="dd" + "11" * 16,
                secret_type=SecretType.SECURE_RANDOMIZED,
                api_id=DUMMY_API_ID,
                api_hash=DUMMY_API_HASH,
            )

            assert result.success is False
            assert result.error_category == ErrorCategory.MT_PROTO_TIMEOUT
            mock_client.disconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_probe_rpc_error(self) -> None:
        mock_writer = AsyncMock()
        mock_writer.close = MagicMock()
        mock_writer.wait_closed = AsyncMock()

        mock_client = AsyncMock()
        mock_client.connect = AsyncMock()
        mock_client.is_user_authorized = AsyncMock(side_effect=RPCError(400, "RPC_ERROR"))
        mock_client.disconnect = AsyncMock()

        with patch("asyncio.open_connection", return_value=(AsyncMock(), mock_writer)), \
             patch("modules.tester.probe.TelegramClient", return_value=mock_client):

            result = await probe_proxy(
                proxy_id=10,
                server="1.1.1.1",
                port=443,
                secret="dd" + "11" * 16,
                secret_type=SecretType.SECURE_RANDOMIZED,
                api_id=DUMMY_API_ID,
                api_hash=DUMMY_API_HASH,
            )

            assert result.success is False
            assert result.error_category == ErrorCategory.TELEGRAM_RPC_ERROR

    @pytest.mark.asyncio
    async def test_secret_is_never_leaked_in_error_message(self) -> None:
        raw_secret = "dd" + "ab" * 16
        with patch(
            "asyncio.open_connection",
            side_effect=ConnectionError(f"Failed with secret={raw_secret}"),
        ):
            result = await probe_proxy(
                proxy_id=11,
                server="1.1.1.1",
                port=443,
                secret=raw_secret,
                secret_type=SecretType.SECURE_RANDOMIZED,
            )
            assert result.success is False
            assert raw_secret not in (result.error_message_safe or "")
