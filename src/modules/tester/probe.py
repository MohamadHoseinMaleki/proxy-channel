"""Three-phase MTProto proxy connectivity probe.

Phases:
1. DNS & SSRF Validation: Resolve server, check all addresses against SSRF rules,
   and pin the destination to a single validated IP address (prevents DNS rebinding).
2. TCP Latency: Establish a raw TCP socket to (target_ip, port) to measure transport latency.
3. MTProto Transport & API Verification: Initialize Telethon with MemorySession,
   establish the MTProto obfuscated connection, then send unauthenticated
   ``help.getConfig`` and require a ``types.Config`` response. TCP success and
   ``client.connect()`` alone are not sufficient. ``is_user_authorized()`` is
   not the connectivity criterion.

Guarantees:
* No persistent session files: uses MemorySession exclusively.
* Disconnects and releases sockets and background tasks in all code paths.
* Does not require user authentication (start() or sign_in()).
* Cancellation propagates cleanly without being converted into failure.
* Secrets and API hashes are strictly excluded from error messages and logs.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Final

from telethon import TelegramClient
from telethon.errors import RPCError
from telethon.sessions import MemorySession
from telethon.tl.functions.help import GetConfigRequest
from telethon.tl.types import Config

from core.logger import get_logger, safe_error_message
from core.models import ErrorCategory
from modules.discovery.models import SecretType
from modules.tester.models import TesterResult
from modules.tester.resolver import (
    DestinationBlockedError,
    DnsResolutionError,
    resolve_and_validate_destination,
)
from modules.tester.transport import select_transport

__all__ = ["API_VERIFY_REQUEST_CLS", "probe_proxy"]

_logger = get_logger("modules.tester.probe")

DEFAULT_TCP_TIMEOUT: Final = 3.0
DEFAULT_MTPROTO_TIMEOUT: Final = 8.0
DEFAULT_TOTAL_TIMEOUT: Final = 15.0

# Canonical unauthenticated Telegram API RPC used as the Phase-3
# REAL_TELEGRAM_API_RPC_VERIFIED criterion.
#
# help.getConfig (TL constructor 0xc4f9186b):
# * does not require user login, a user session, phone verification, or a bot token
# * is a real high-level MTProto/API request (not transport setup, not session state)
# * returns ``types.Config`` (dc_options, this_dc, ...) from Telegram itself
#
# ``client.is_user_authorized()`` is intentionally NOT used: it issues
# ``updates.GetStateRequest`` (authorization-required), swallows every
# ``RPCError``, and returns False for a fresh MemorySession. That False is
# not proof of API connectivity, and it is not a proxy failure either.
API_VERIFY_REQUEST_CLS: Final[type[GetConfigRequest]] = GetConfigRequest


def _is_verified_api_response(result: object) -> bool:
    """True only when Telegram answered help.getConfig with a Config object."""
    return isinstance(result, Config) and bool(getattr(result, "dc_options", None))


async def probe_proxy(
    *,
    proxy_id: int,
    server: str,
    port: int,
    secret: str,
    secret_type: SecretType | str = SecretType.LEGACY,
    api_id: int | None = None,
    api_hash: str | None = None,
    tcp_timeout_seconds: float = DEFAULT_TCP_TIMEOUT,
    mtproto_timeout_seconds: float = DEFAULT_MTPROTO_TIMEOUT,
    total_timeout_seconds: float = DEFAULT_TOTAL_TIMEOUT,
) -> TesterResult:
    """Execute a full 3-phase connectivity test against an MTProto proxy."""
    t_start = time.monotonic()

    try:
        return await asyncio.wait_for(
            _execute_probe(
                proxy_id=proxy_id,
                server=server,
                port=port,
                secret=secret,
                secret_type=secret_type,
                api_id=api_id,
                api_hash=api_hash,
                tcp_timeout=tcp_timeout_seconds,
                mtproto_timeout=mtproto_timeout_seconds,
                t_start=t_start,
            ),
            timeout=total_timeout_seconds,
        )
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        return TesterResult(
            proxy_id=proxy_id,
            success=False,
            error_category=ErrorCategory.MT_PROTO_TIMEOUT,
            error_message_safe=f"Total test timeout exceeded ({total_timeout_seconds}s)",
        )


async def _execute_probe(
    *,
    proxy_id: int,
    server: str,
    port: int,
    secret: str,
    secret_type: SecretType | str,
    api_id: int | None,
    api_hash: str | None,
    tcp_timeout: float,
    mtproto_timeout: float,
    t_start: float,
) -> TesterResult:
    # -----------------------------------------------------------------------
    # Phase 1: DNS & SSRF Validation (DNS Rebinding Protected)
    # -----------------------------------------------------------------------
    try:
        pinned_ip, _ = await resolve_and_validate_destination(server, port)
    except DestinationBlockedError as exc:
        return TesterResult(
            proxy_id=proxy_id,
            success=False,
            error_category=ErrorCategory.SSRF_BLOCKED,
            error_message_safe=safe_error_message(exc),
        )
    except DnsResolutionError as exc:
        return TesterResult(
            proxy_id=proxy_id,
            success=False,
            error_category=ErrorCategory.DNS_ERROR,
            error_message_safe=safe_error_message(exc),
        )

    # -----------------------------------------------------------------------
    # Phase 2: TCP Connection & Latency Measurement
    # -----------------------------------------------------------------------
    t_tcp = time.monotonic()
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(pinned_ip, port),
            timeout=tcp_timeout,
        )
        tcp_ms = (time.monotonic() - t_tcp) * 1000.0
        writer.close()
        await writer.wait_closed()
    except TimeoutError:
        return TesterResult(
            proxy_id=proxy_id,
            success=False,
            target_ip=pinned_ip,
            error_category=ErrorCategory.TCP_TIMEOUT,
            error_message_safe=f"TCP connection timed out after {tcp_timeout}s",
        )
    except ConnectionRefusedError:
        return TesterResult(
            proxy_id=proxy_id,
            success=False,
            target_ip=pinned_ip,
            error_category=ErrorCategory.TCP_REFUSED,
            error_message_safe="TCP connection refused by target host",
        )
    except ConnectionResetError:
        return TesterResult(
            proxy_id=proxy_id,
            success=False,
            target_ip=pinned_ip,
            error_category=ErrorCategory.TCP_RESET,
            error_message_safe="TCP connection reset by target host",
        )
    except OSError as exc:
        return TesterResult(
            proxy_id=proxy_id,
            success=False,
            target_ip=pinned_ip,
            error_category=ErrorCategory.TCP_ERROR,
            error_message_safe=safe_error_message(exc),
        )

    # -----------------------------------------------------------------------
    # Phase 3: MTProto Transport & API Verification
    # -----------------------------------------------------------------------
    try:
        bytes.fromhex(secret)
    except ValueError:
        return TesterResult(
            proxy_id=proxy_id,
            success=False,
            tcp_connect_ms=tcp_ms,
            target_ip=pinned_ip,
            error_category=ErrorCategory.INVALID_SECRET,
            error_message_safe="Proxy secret is not valid hexadecimal",
        )

    selection = select_transport(secret_type)
    if not selection.is_supported or selection.transport_cls is None:
        return TesterResult(
            proxy_id=proxy_id,
            success=False,
            tcp_connect_ms=tcp_ms,
            target_ip=pinned_ip,
            error_category=ErrorCategory.UNSUPPORTED_TRANSPORT,
            error_message_safe=selection.reason,
            transport_type=selection.transport_type,
        )

    if not api_id or not api_hash:
        return TesterResult(
            proxy_id=proxy_id,
            success=False,
            tcp_connect_ms=tcp_ms,
            target_ip=pinned_ip,
            error_category=ErrorCategory.API_AUTH_ERROR,
            error_message_safe="TELEGRAM_API_ID and TELEGRAM_API_HASH are not configured",
            transport_type=selection.transport_type,
        )

    client: TelegramClient | None = None
    t_mtp = time.monotonic()
    try:
        client = TelegramClient(
            MemorySession(),
            api_id=int(api_id),
            api_hash=api_hash,
            connection=selection.transport_cls,
            proxy=(pinned_ip, port, secret),
            timeout=mtproto_timeout,
            auto_reconnect=False,
            receive_updates=False,
        )

        await asyncio.wait_for(client.connect(), timeout=mtproto_timeout)
        # Transport being up is not enough. Verify a real unauthenticated
        # Telegram API RPC through the proxy and require a Config response.
        config = await asyncio.wait_for(
            client(API_VERIFY_REQUEST_CLS()),
            timeout=mtproto_timeout,
        )
        if not _is_verified_api_response(config):
            return TesterResult(
                proxy_id=proxy_id,
                success=False,
                tcp_connect_ms=tcp_ms,
                target_ip=pinned_ip,
                error_category=ErrorCategory.PROTOCOL_ERROR,
                error_message_safe=(
                    "Unauthenticated help.getConfig did not return a Telegram Config object"
                ),
                transport_type=selection.transport_type,
            )

        mtp_ms = (time.monotonic() - t_mtp) * 1000.0
        total_ms = (time.monotonic() - t_start) * 1000.0

        return TesterResult(
            proxy_id=proxy_id,
            success=True,
            tcp_connect_ms=tcp_ms,
            mtproto_connect_ms=mtp_ms,
            total_latency_ms=total_ms,
            transport_type=selection.transport_type,
            target_ip=pinned_ip,
        )

    except asyncio.CancelledError:
        raise
    except TimeoutError:
        return TesterResult(
            proxy_id=proxy_id,
            success=False,
            tcp_connect_ms=tcp_ms,
            target_ip=pinned_ip,
            error_category=ErrorCategory.MT_PROTO_TIMEOUT,
            error_message_safe=f"MTProto handshake timed out after {mtproto_timeout}s",
            transport_type=selection.transport_type,
        )
    except ConnectionError as exc:
        msg = str(exc)
        category = (
            ErrorCategory.MT_PROTO_TIMEOUT
            if "closed the connection after sending initial payload" in msg
            else ErrorCategory.PROTOCOL_ERROR
        )
        return TesterResult(
            proxy_id=proxy_id,
            success=False,
            tcp_connect_ms=tcp_ms,
            target_ip=pinned_ip,
            error_category=category,
            error_message_safe=safe_error_message(exc),
            transport_type=selection.transport_type,
        )
    except RPCError as exc:
        return TesterResult(
            proxy_id=proxy_id,
            success=False,
            tcp_connect_ms=tcp_ms,
            target_ip=pinned_ip,
            error_category=ErrorCategory.TELEGRAM_RPC_ERROR,
            error_message_safe=safe_error_message(exc),
            transport_type=selection.transport_type,
        )
    except Exception as exc:
        return TesterResult(
            proxy_id=proxy_id,
            success=False,
            tcp_connect_ms=tcp_ms,
            target_ip=pinned_ip,
            error_category=ErrorCategory.UNKNOWN_ERROR,
            error_message_safe=safe_error_message(exc),
            transport_type=selection.transport_type,
        )
    finally:
        if client is not None:
            with contextlib.suppress(Exception):
                await client.disconnect()
