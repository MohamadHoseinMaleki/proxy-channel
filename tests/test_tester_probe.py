"""Unit tests for the 3-phase MTProto connectivity probe."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telethon.errors import RPCError
from telethon.sessions import MemorySession
from telethon.tl.functions.help import GetConfigRequest
from telethon.tl.types import Config, DcOption

from core.models import ErrorCategory
from modules.discovery.models import SecretType
from modules.tester.models import TransportType
from modules.tester.probe import API_VERIFY_REQUEST_CLS, probe_proxy
from modules.tester.resolver import DnsResolutionError

DUMMY_API_ID = 123456
DUMMY_API_HASH = "0123456789abcdef0123456789abcdef"


def _dummy_config() -> Config:
    """A structurally valid help.getConfig response (not a live network result)."""
    return Config(
        date=datetime(2026, 1, 1, tzinfo=UTC),
        expires=datetime(2026, 1, 2, tzinfo=UTC),
        test_mode=False,
        this_dc=2,
        dc_options=[DcOption(id=2, ip_address="149.154.167.51", port=443)],
        dc_txt_domain_name="apv3.stel.com",
        chat_size_max=200,
        megagroup_size_max=200000,
        forwarded_count_max=100,
        online_update_period_ms=30000,
        offline_blur_timeout_ms=1000,
        offline_idle_timeout_ms=5000,
        online_cloud_timeout_ms=300000,
        notify_cloud_delay_ms=30000,
        notify_default_delay_ms=1500,
        push_chat_period_ms=60000,
        push_chat_limit=1,
        edit_time_limit=172800,
        revoke_time_limit=172800,
        revoke_pm_time_limit=172800,
        rating_e_decay=2419200,
        stickers_recent_limit=200,
        channels_read_media_period=604800,
        call_receive_timeout_ms=20000,
        call_ring_timeout_ms=90000,
        call_connect_timeout_ms=30000,
        call_packet_timeout_ms=10000,
        me_url_prefix="https://t.me/",
        caption_length_max=1024,
        message_length_max=4096,
        webfile_dc_id=4,
    )


def _mock_tcp_writer() -> MagicMock:
    mock_writer = MagicMock()
    mock_writer.close = MagicMock()
    mock_writer.wait_closed = AsyncMock()
    return mock_writer


def _mock_client(**kwargs: Any) -> AsyncMock:
    mock_client = AsyncMock()
    mock_client.connect = AsyncMock()
    mock_client.disconnect = AsyncMock()
    mock_client.is_user_authorized = AsyncMock(return_value=False)
    for key, value in kwargs.items():
        setattr(mock_client, key, value)
    return mock_client


def _phase2_and_client(mock_client: AsyncMock) -> tuple[Any, Any]:
    return (
        patch("asyncio.open_connection", return_value=(AsyncMock(), _mock_tcp_writer())),
        patch("modules.tester.probe.TelegramClient", return_value=mock_client),
    )


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
    async def test_tcp_only_success_is_insufficient(self) -> None:
        mock_client = _mock_client(connect=AsyncMock(side_effect=ConnectionError("no mtproto")))
        tcp_patch, client_patch = _phase2_and_client(mock_client)
        with tcp_patch, client_patch:
            result = await probe_proxy(
                proxy_id=8,
                server="1.1.1.1",
                port=443,
                secret="dd" + "11" * 16,
                secret_type=SecretType.SECURE_RANDOMIZED,
                api_id=DUMMY_API_ID,
                api_hash=DUMMY_API_HASH,
            )

        assert result.success is False
        assert result.tcp_connect_ms is not None
        assert result.mtproto_connect_ms is None
        assert result.error_category == ErrorCategory.PROTOCOL_ERROR
        mock_client.disconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_mtproto_transport_connection_alone_is_insufficient(self) -> None:
        # connect() succeeds (transport up) but help.getConfig is not a Config.
        mock_client = _mock_client()
        mock_client.return_value = object()
        tcp_patch, client_patch = _phase2_and_client(mock_client)
        with tcp_patch, client_patch:
            result = await probe_proxy(
                proxy_id=8,
                server="1.1.1.1",
                port=443,
                secret="dd" + "11" * 16,
                secret_type=SecretType.SECURE_RANDOMIZED,
                api_id=DUMMY_API_ID,
                api_hash=DUMMY_API_HASH,
            )

        assert result.success is False
        assert result.tcp_connect_ms is not None
        assert result.mtproto_connect_ms is None
        assert result.error_category == ErrorCategory.PROTOCOL_ERROR
        mock_client.connect.assert_awaited()
        mock_client.assert_awaited()
        mock_client.disconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unauthorized_session_is_not_a_proxy_failure(self) -> None:
        mock_client = _mock_client()
        mock_client.return_value = _dummy_config()
        mock_client.is_user_authorized = AsyncMock(return_value=False)
        tcp_patch, client_patch = _phase2_and_client(mock_client)
        with tcp_patch, client_patch:
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
        mock_client.is_user_authorized.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_help_get_config_is_the_api_verification_rpc(self) -> None:
        mock_client = _mock_client()
        mock_client.return_value = _dummy_config()
        with (
            patch("asyncio.open_connection", return_value=(AsyncMock(), _mock_tcp_writer())),
            patch("modules.tester.probe.TelegramClient", return_value=mock_client) as mock_tg_cls,
        ):
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

        assert API_VERIFY_REQUEST_CLS is GetConfigRequest
        mock_client.assert_awaited()
        await_args = mock_client.await_args
        assert await_args is not None
        request = await_args.args[0]
        assert isinstance(request, GetConfigRequest)

        call_kwargs = mock_tg_cls.call_args[1]
        session_arg = mock_tg_cls.call_args[0][0]
        assert isinstance(session_arg, MemorySession)
        assert call_kwargs["auto_reconnect"] is False
        assert call_kwargs["timeout"] == 8.0
        mock_client.disconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_api_timeout_is_mt_proto_timeout(self) -> None:
        mock_client = _mock_client()
        mock_client.side_effect = TimeoutError()
        tcp_patch, client_patch = _phase2_and_client(mock_client)
        with tcp_patch, client_patch:
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
        mock_client.connect.assert_awaited()
        mock_client.disconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_api_rpc_error_is_classified(self) -> None:
        mock_client = _mock_client()
        mock_client.side_effect = RPCError(400, "RPC_ERROR")
        tcp_patch, client_patch = _phase2_and_client(mock_client)
        with tcp_patch, client_patch:
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
        mock_client.disconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_connect_timeout_still_disconnects(self) -> None:
        mock_client = _mock_client(connect=AsyncMock(side_effect=TimeoutError()))
        tcp_patch, client_patch = _phase2_and_client(mock_client)
        with tcp_patch, client_patch:
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
    async def test_cleanup_on_cancellation(self) -> None:
        mock_client = _mock_client(connect=AsyncMock(side_effect=asyncio.CancelledError()))
        tcp_patch, client_patch = _phase2_and_client(mock_client)
        with tcp_patch, client_patch, pytest.raises(asyncio.CancelledError):
            await probe_proxy(
                proxy_id=12,
                server="1.1.1.1",
                port=443,
                secret="dd" + "11" * 16,
                secret_type=SecretType.SECURE_RANDOMIZED,
                api_id=DUMMY_API_ID,
                api_hash=DUMMY_API_HASH,
            )

        mock_client.disconnect.assert_awaited_once()

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
