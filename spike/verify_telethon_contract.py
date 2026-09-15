#!/usr/bin/env python3
"""Verify the Telethon MTProxy contract *from the installed source*.

Why this exists
---------------
``spike/REPORT.md`` recorded empirical-sounding findings about Telethon and
Pyrogram. The audit in ``spike/AUDIT.md`` shows those findings could not have
been produced by the committed spike code. Rather than replace one set of
unverified claims with another, this script *derives* the facts that Task 005
(the production MTProto tester) depends on directly from the installed library.

It performs **no network I/O** and needs no Telegram credentials: everything it
checks is either library metadata or pure in-process header construction. No
result here is a claim that any real proxy works.

Usage
-----
Telethon is not yet a dependency of the platform (it arrives with Task 005), so
run this inside an environment that has it::

    uv run --with telethon==1.34.0 python spike/verify_telethon_contract.py
    uv run --with telethon          python spike/verify_telethon_contract.py

Exit codes: 0 = every expectation held, 1 = at least one failed,
2 = Telethon not installed (nothing verified).
"""

from __future__ import annotations

import inspect
import sys
from collections.abc import Callable
from typing import Any

#: Secret shapes seen in the wild and in the original spike fixtures.
#: ``hex length`` / ``byte length`` are reported at runtime, not hard-coded.
SECRET_FORMATS: dict[str, str] = {
    "legacy 16-byte (32 hex)": "000102030405060708090a0b0c0d0e0f",
    "dd-secret 17-byte 0xDD (34 hex)": "dd11112222333344445555666677778888",
    "fake-TLS 0xEE, no SNI (34 hex)": "ee00000000000000000000000000000000",
    "fake-TLS 0xEE + SNI google.com": (
        "ee111122223333444455556666777788887777772e676f6f676c652e636f6d"
    ),
    "spike 'valid' case secret": "ee00000000000000000000000000000000676f6f676c652e636f6d",
    "spike 'wrong_secret' case": "eeffffffffffffffffffffffffffffffff676f6f676c652e636f6d",
    "spike 'dead_endpoint' case": "ee00000000000000000000000000000000",
    "spike 'non_mtproto' case": "ee00000000000000000000000000000000",
}

#: Telethon release that introduced ``TcpMTProxy.normalize_secret``. Verified by
#: probing published wheels: 1.34.0 lacks it, 1.35.0 has it, unchanged to 1.45.0.
NORMALIZE_SECRET_SINCE = "1.35.0"


def _hr(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def _transport_secret_bytes(tcp_mt_proxy: Any, secret_hex: str) -> bytes:
    """Decode a secret exactly the way the installed transport would.

    Telethon >= 1.35.0 routes through ``TcpMTProxy.normalize_secret``; 1.34.0
    calls ``bytes.fromhex`` inline. Probing only ``init_header`` (as an earlier
    draft of this script did) bypasses that step and reports the wrong answer,
    so the real entry point is used here.
    """
    normalizer = getattr(tcp_mt_proxy, "normalize_secret", None)
    if normalizer is not None:
        return bytes(normalizer(secret_hex))
    return bytes.fromhex(secret_hex)


def _probe(
    tcp_mt_proxy: Any, mt_proxy_io: Any, codec: Any, secret_hex: str
) -> tuple[bool, str, int | None]:
    """Return (accepted, outcome_text, normalized_byte_length)."""
    try:
        secret = _transport_secret_bytes(tcp_mt_proxy, secret_hex)
    except Exception as exc:
        return False, f"decode {type(exc).__name__}: {exc}", None
    try:
        mt_proxy_io.init_header(secret, 2, codec)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}", len(secret)
    return True, "ACCEPTED (header built, no I/O)", len(secret)


def main() -> int:
    try:
        import telethon
        from telethon import TelegramClient
        from telethon.network import connection as tl_connection
        from telethon.network.connection.tcpmtproxy import (
            ConnectionTcpMTProxyRandomizedIntermediate,
            MTProxyIO,
            TcpMTProxy,
        )
    except ImportError as exc:
        print(f"Telethon is not installed in this environment: {exc}")
        print("Run with:  uv run --with telethon python spike/verify_telethon_contract.py")
        return 2

    failures: list[str] = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f" -- {detail}" if detail else ""))
        if not condition:
            failures.append(label)

    _hr(f"Telethon {telethon.__version__} on Python {sys.version.split()[0]}")

    # -- 1. Transport class availability -----------------------------------
    _hr("1. MTProxy transport classes")
    check(
        "ConnectionTcpMTProxyRandomizedIntermediate is exported",
        hasattr(tl_connection, "ConnectionTcpMTProxyRandomizedIntermediate"),
    )
    doc = inspect.getdoc(TcpMTProxy) or ""
    print(f"[INFO] TcpMTProxy docstring warns 'EXPERIMENTAL': {'EXPERIMENTAL' in doc}")
    for line in doc.splitlines():
        if "EXPERIMENTAL" in line or "shouldn't be using" in line:
            print(f"       {line.strip()}")

    # -- 2. Client construction contract -----------------------------------
    _hr("2. TelegramClient construction contract")
    params = list(inspect.signature(TelegramClient.__init__).parameters)
    for param in ("connection", "proxy", "timeout", "auto_reconnect", "receive_updates"):
        check(f"accepts `{param}=`", param in params)

    # -- 3. What connect() actually returns --------------------------------
    _hr("3. connect() return contract   <-- spike/REPORT.md got this wrong")
    sig = inspect.signature(TelegramClient.connect)
    print(f"[INFO] signature: {sig}")
    check(
        "connect() is annotated `-> None`, not bool",
        sig.return_annotation in (None, inspect.Signature.empty),
        f"return_annotation={sig.return_annotation!r}",
    )
    src = inspect.getsource(TelegramClient.connect)
    check(
        "connect() never returns a truthy value",
        "return True" not in src,
        "`if await client.connect():` is therefore always False",
    )
    check(
        "connect() issues a real MTProto RPC (help.GetConfigRequest)",
        "GetConfigRequest" in src,
        "success is proven by this round-trip, not by a boolean",
    )
    check("connect() does not call start()/sign_in()", "sign_in" not in src)
    check(
        "connect() spawns background loops that must be torn down",
        "_update_loop" in src and "_keepalive_loop" in src,
    )

    # -- 4. Connection introspection and teardown --------------------------
    _hr("4. Introspection and teardown")
    # Defined on a mixin, so look it up on the class rather than in __dict__.
    is_conn = getattr(TelegramClient, "is_connected", None)
    check(
        "is_connected is a METHOD in Telethon (a property in Pyrogram)",
        inspect.isfunction(is_conn),
        f"type={type(is_conn).__name__}; `if client.is_connected:` is always truthy",
    )
    try:
        from telethon.sessions import MemorySession

        memory_session_ok = MemorySession is not None
    except ImportError:
        memory_session_ok = False
    check(
        "MemorySession exists for ephemeral, on-disk-free sessions",
        memory_session_ok,
        "passing a *string* session name instead writes a .session SQLite file",
    )
    disc_src = inspect.getsource(TelegramClient.disconnect)
    print(
        f"[INFO] disconnect() shields teardown with asyncio.shield: {'asyncio.shield' in disc_src}"
    )
    print("[INFO] disconnect() returns a coroutine/Task when the loop is running,")
    print("       therefore it must always be awaited.")

    # -- 5. Secret format support ------------------------------------------
    _hr("5. Secret formats accepted by the MTProxy transport   <-- CRITICAL")
    normalizer = getattr(TcpMTProxy, "normalize_secret", None)
    has_normalize = normalizer is not None
    print(f"[INFO] TcpMTProxy.normalize_secret present: {has_normalize}")
    print(f"[INFO] introduced in Telethon {NORMALIZE_SECRET_SINCE} (1.34.0 does not have it)")
    if has_normalize:
        for line in inspect.getsource(normalizer).splitlines():
            print(f"    {line}")
    else:
        print("    1.34.0 instead does:  self._secret = bytes.fromhex(proxy[2])")
    print()
    print("MTProxyIO.init_header() validation, verbatim:")
    for line in inspect.getsource(MTProxyIO.init_header).splitlines()[1:10]:
        print(f"    {line}")
    print()
    print(f"{'format':40s} {'hex':>4s} {'norm':>5s}  result")
    print("-" * 96)
    codec = ConnectionTcpMTProxyRandomizedIntermediate.packet_codec
    results: dict[str, tuple[bool, int | None]] = {}
    for label, secret in SECRET_FORMATS.items():
        accepted, outcome, norm_len = _probe(TcpMTProxy, MTProxyIO, codec, secret)
        results[label] = (accepted, norm_len)
        shown = "-" if norm_len is None else str(norm_len)
        print(f"{label:40s} {len(secret):4d} {shown:>5s}  {outcome}")

    print()
    legacy_ok = results["legacy 16-byte (32 hex)"][0]
    dd_ok = results["dd-secret 17-byte 0xDD (34 hex)"][0]
    ee_ok = results["fake-TLS 0xEE + SNI google.com"][0]
    check("legacy 16-byte secrets are accepted", legacy_ok)
    check("dd-secrets are accepted", dd_ok)
    check(
        "fake-TLS (0xEE) acceptance matches the installed version",
        ee_ok == has_normalize,
        f"accepted={ee_ok}, normalize_secret={has_normalize}",
    )

    # Structural acceptance is NOT the same as working fake-TLS.
    tls_emulation = any(
        token in inspect.getsource(MTProxyIO)
        for token in ("ClientHello", "client_hello", "server_name", "SNI", "ssl")
    )
    print()
    print(f"[INFO] MTProxyIO implements TLS ClientHello / SNI emulation: {tls_emulation}")
    if has_normalize and not tls_emulation:
        print("[INFO] 1.35.0+ truncates the secret to 16 bytes and DROPS the SNI domain")
        print("       ('until domain support is added' -- upstream comment). So an 0xEE")
        print("       secret is structurally usable, but whether a real fake-TLS MTProxy")
        print("       accepts the resulting handshake is EMPIRICALLY UNVERIFIED here.")
        print("       Resolving that needs a live test (Task 005/016), not source reading.")

    check(
        "TcpMTProxy takes the secret from proxy[2] (3-tuple server/port/secret)",
        "proxy[2]" in inspect.getsource(TcpMTProxy.__init__),
    )

    # -- 6. Latency floor baked into the transport --------------------------
    _hr("6. Built-in latency floor in TcpMTProxy._connect")
    connect_src = inspect.getsource(TcpMTProxy._connect)
    for line in connect_src.splitlines():
        if "_wait_for_data" in line or "at_eof" in line:
            print(f"[INFO] {line.strip()}")
    compact = connect_src.replace(" ", "")
    check(
        "TcpMTProxy waits up to 2s after connecting (issue #1134 workaround)",
        "_wait_for_data" in connect_src and ",2)" in compact,
        "a hard floor on MTProto connect latency that timeouts must budget for",
    )
    check(
        "TcpMTProxy raises ConnectionError if the proxy closes immediately",
        "Proxy closed the connection after sending initial payload" in connect_src,
        "this is the observable signal for a non-MTProto endpoint",
    )

    # -- summary ------------------------------------------------------------
    _hr("SUMMARY")
    if failures:
        print(f"{len(failures)} expectation(s) did NOT hold:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("All expectations held for this Telethon version.")
    print("Task 005 must be implemented against THESE facts, not spike/REPORT.md.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
