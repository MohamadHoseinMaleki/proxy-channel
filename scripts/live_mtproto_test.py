#!/usr/bin/env python3
"""Live smoke test script for MTProto proxies.

Accepts configuration and proxy details safely via environment variables or stdin
so credentials and secrets never linger in shell command history.

Usage:
    export TELEGRAM_API_ID=123456
    export TELEGRAM_API_HASH=0123456789abcdef0123456789abcdef
    export PROXY_SERVER=1.2.3.4
    export PROXY_PORT=443
    export PROXY_SECRET=ee...
    uv run python scripts/live_mtproto_test.py
"""

from __future__ import annotations

import asyncio
import os
import sys

from core.identity import mask_secret
from modules.discovery.normalizer import (
    validate_and_normalize_port,
    validate_and_normalize_server,
    validate_and_parse_secret,
)
from modules.tester.probe import probe_proxy


async def run_live_test() -> int:
    api_id_raw = os.getenv("TELEGRAM_API_ID")
    api_hash = os.getenv("TELEGRAM_API_HASH")

    if not api_id_raw or not api_hash:
        print("[ERROR] TELEGRAM_API_ID and TELEGRAM_API_HASH environment variables are required.")
        return 2

    server_raw = os.getenv("PROXY_SERVER")
    port_raw = os.getenv("PROXY_PORT")
    secret_raw = os.getenv("PROXY_SECRET")

    if not server_raw or not port_raw or not secret_raw:
        print(
            "[ERROR] PROXY_SERVER, PROXY_PORT, and PROXY_SECRET "
            "environment variables are required."
        )
        return 2

    try:
        server = validate_and_normalize_server(server_raw)
        port = validate_and_normalize_port(port_raw)
        secret, secret_type, _sni_domain = validate_and_parse_secret(secret_raw)
    except Exception as exc:
        print(f"[ERROR] Proxy parameter validation failed: {exc}")
        return 1

    masked_sec = mask_secret(secret.reveal())
    print(f"Testing Proxy: {server}:{port} (secret={masked_sec}, type={secret_type.value})")

    result = await probe_proxy(
        proxy_id=1,
        server=server,
        port=port,
        secret=secret.reveal(),
        secret_type=secret_type,
        api_id=int(api_id_raw),
        api_hash=api_hash,
    )

    print("-" * 60)
    print(f"Target IP:       {result.target_ip or '-'}")
    print(f"Transport:       {result.transport_type.value}")
    tcp_str = f"({result.tcp_connect_ms:.1f}ms)" if result.tcp_connect_ms else ""
    print(f"TCP Handshake:   {'OK ' + tcp_str if result.tcp_connect_ms is not None else 'FAILED'}")
    print(f"MTProto / API:   {'OK' if result.success else 'FAILED'}")
    if result.mtproto_connect_ms:
        print(f"MTProto Latency: {result.mtproto_connect_ms:.1f}ms")
    if result.total_latency_ms:
        print(f"Total Latency:   {result.total_latency_ms:.1f}ms")
    if not result.success:
        print(f"Error Category:  {result.error_category}")
        print(f"Safe Message:    {result.error_message_safe}")
    print("-" * 60)
    print(f"VERDICT:         {'SUCCESS (WORKING)' if result.success else 'FAILED'}")

    return 0 if result.success else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run_live_test()))
