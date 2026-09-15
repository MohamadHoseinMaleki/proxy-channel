"""Validation and normalisation for MTProto proxy endpoints.

This module provides strict validation for:
* Server: public IPv4, IPv6, and FQDN hostnames (rejects loopback, private,
  link-local, multicast, unspecified, reserved, and cloud metadata endpoints).
  DNS resolution is deliberately NOT performed during parsing.
* Port: 1 <= port <= 65535, rejects 0, >65535, negative, decimal, non-numeric.
* Secret: legacy (16 bytes), dd-intermediate (17 bytes starting with 0xdd),
  and fake-TLS (>=17 bytes starting with 0xee and valid SNI domain).
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import ipaddress
import re
from typing import Final

from core.identity import (
    ProxySecret,
    normalize_server,
)
from modules.discovery.models import SecretType

__all__ = [
    "DiscoveryValidationError",
    "PortValidationError",
    "SecretValidationError",
    "ServerValidationError",
    "is_disallowed_ip",
    "validate_and_normalize_port",
    "validate_and_normalize_server",
    "validate_and_parse_secret",
    "validate_hostname",
]


class DiscoveryValidationError(ValueError):
    """Base error raised when proxy attributes fail structural validation.

    Message is always clean of secrets.
    """


class ServerValidationError(DiscoveryValidationError):
    """Raised when server fails IP or hostname structural validation."""


class PortValidationError(DiscoveryValidationError):
    """Raised when port is not a valid 16-bit unsigned integer (1..65535)."""


class SecretValidationError(DiscoveryValidationError):
    """Raised when MTProto secret is not valid legacy, dd, or ee format."""


# ---------------------------------------------------------------------------
# Server / IP / Hostname Validation
# ---------------------------------------------------------------------------

#: Disallowed IP networks (private, reserved, documentation, carrier NAT).
_DISALLOWED_NETWORKS: Final[tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]] = (
    ipaddress.ip_network("0.0.0.0/8"),  # Current network (only valid as source)
    ipaddress.ip_network("10.0.0.0/8"),  # Private
    ipaddress.ip_network("100.64.0.0/10"),  # Shared Address Space (CGNAT)
    ipaddress.ip_network("127.0.0.0/8"),  # Loopback
    ipaddress.ip_network("169.254.0.0/16"),  # Link-Local
    ipaddress.ip_network("172.16.0.0/12"),  # Private
    ipaddress.ip_network("192.0.0.0/24"),  # IETF Protocol Assignments
    ipaddress.ip_network("192.0.2.0/24"),  # Documentation (TEST-NET-1)
    ipaddress.ip_network("192.168.0.0/16"),  # Private
    ipaddress.ip_network("198.18.0.0/15"),  # Benchmarking
    ipaddress.ip_network("198.51.100.0/24"),  # Documentation (TEST-NET-2)
    ipaddress.ip_network("203.0.113.0/24"),  # Documentation (TEST-NET-3)
    ipaddress.ip_network("224.0.0.0/4"),  # Multicast
    ipaddress.ip_network("240.0.0.0/4"),  # Reserved
    ipaddress.ip_network("255.255.255.255/32"),  # Broadcast
    # IPv6
    ipaddress.ip_network("::/128"),  # Unspecified
    ipaddress.ip_network("::1/128"),  # Loopback
    ipaddress.ip_network("100::/64"),  # Discard prefix
    ipaddress.ip_network("2001:db8::/32"),  # Documentation
    ipaddress.ip_network("fc00::/7"),  # Unique Local (ULA)
    ipaddress.ip_network("fe80::/10"),  # Link-Local
    ipaddress.ip_network("ff00::/8"),  # Multicast
)

#: Disallowed internal/special-use domain suffixes.
_DISALLOWED_DOMAIN_SUFFIXES: Final[tuple[str, ...]] = (
    "localhost",
    ".localhost",
    ".local",
    ".internal",
    ".arpa",
    ".localdomain",
    ".lan",
    ".home",
    ".corp",
    ".intranet",
    "metadata.google.internal",
)

#: Label pattern for RFC 1035 / RFC 1123 domain names.
_HOSTNAME_LABEL_RE: Final = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$", re.IGNORECASE)


def is_disallowed_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True if an IP is loopback, private, link-local, multicast or reserved."""
    if (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_unspecified
        or ip.is_reserved
    ):
        return True

    # If it is an IPv4-mapped IPv6 address (e.g. ::ffff:127.0.0.1), check mapped IPv4.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        return is_disallowed_ip(ip.ipv4_mapped)

    # Check against our explicit disallowed CIDR list (covers CGNAT, TEST-NET, etc.)
    return any(ip in net for net in _DISALLOWED_NETWORKS)


def validate_hostname(hostname: str) -> bool:
    """Validate DNS hostname syntax without performing DNS resolution."""
    normalized = hostname.strip().lower().rstrip(".")
    if not normalized or len(normalized) > 253:
        return False

    # Check for disallowed internal / reserved domain suffixes.
    if normalized == "localhost" or any(
        normalized.endswith(suffix) for suffix in _DISALLOWED_DOMAIN_SUFFIXES
    ):
        return False

    labels = normalized.split(".")
    # Public internet hostnames must have at least two labels (e.g. domain.tld).
    if len(labels) < 2:
        return False

    for label in labels:
        if not label or len(label) > 63:
            return False
        if not _HOSTNAME_LABEL_RE.match(label):
            return False

    # TLD must not be all-numeric.
    return not labels[-1].isdigit()


def validate_and_normalize_server(raw_server: str) -> str:
    """Validate and normalize a server host (IPv4, IPv6, or DNS hostname).

    Does NOT resolve DNS. Rejects internal, loopback, private, link-local,
    multicast, and reserved endpoints.
    """
    cleaned = raw_server.strip()
    if not cleaned:
        msg = "Server host must not be empty"
        raise ServerValidationError(msg)

    canonical = normalize_server(cleaned)
    if not canonical:
        msg = "Server host normalized to empty"
        raise ServerValidationError(msg)

    # 1. Test for IP address literal
    try:
        ip = ipaddress.ip_address(canonical)
        if is_disallowed_ip(ip):
            msg = f"Server IP address {canonical} is not a valid public address"
            raise ServerValidationError(msg)
        return canonical
    except ValueError:
        pass

    # 2. Test for valid DNS hostname
    if not validate_hostname(canonical):
        msg = f"Server host {canonical!r} is not a valid public hostname or IP literal"
        raise ServerValidationError(msg)

    return canonical


# ---------------------------------------------------------------------------
# Port Validation
# ---------------------------------------------------------------------------


def validate_and_normalize_port(raw_port: int | str) -> int:
    """Validate and normalize a TCP port to 1..65535.

    Rejects non-numeric, decimal, negative, 0, and >65535.
    """
    received: object = raw_port
    if isinstance(received, bool):
        msg = "Port must be an integer, got bool"
        raise PortValidationError(msg)

    if isinstance(received, int):
        port_num = received
    elif isinstance(received, str):
        cleaned = received.strip()
        if not cleaned:
            msg = "Port must not be empty"
            raise PortValidationError(msg)
        if not cleaned.isdigit():
            msg = f"Port must be an integer, got {cleaned!r}"
            raise PortValidationError(msg)
        port_num = int(cleaned)
    else:
        msg = f"Port must be an integer or string, got {type(received).__name__}"
        raise PortValidationError(msg)

    if not 1 <= port_num <= 65535:
        msg = f"Port must be within 1..65535, got {port_num}"
        raise PortValidationError(msg)

    return port_num


# ---------------------------------------------------------------------------
# Secret Validation & Parsing
# ---------------------------------------------------------------------------


def validate_and_parse_secret(
    raw_secret: str,
) -> tuple[ProxySecret, SecretType, str | None]:
    """Validate an MTProto proxy secret and extract its structural metadata.

    Returns:
        (ProxySecret, SecretType, sni_domain)

    Valid formats:
    * LEGACY: exactly 16 bytes decoded (e.g. 32 hex chars or base64 equivalent).
    * SECURE_RANDOMIZED (dd): exactly 17 bytes decoded starting with 0xdd.
    * FAKE_TLS (ee): >= 17 bytes decoded starting with 0xee. If > 17 bytes,
      the bytes from index 17 onwards represent the SNI hostname, which must
      decode to valid ASCII and satisfy hostname rules.
    """
    cleaned = raw_secret.strip()
    if not cleaned:
        msg = "Secret must not be empty"
        raise SecretValidationError(msg)

    # 1. Decode to raw bytes: hex first, then base64 fallback
    secret_bytes: bytes | None = None
    with contextlib.suppress(ValueError):
        secret_bytes = bytes.fromhex(cleaned)

    if secret_bytes is None:
        with contextlib.suppress(binascii.Error, ValueError, UnicodeEncodeError):
            padded = cleaned + "=" * (-len(cleaned) % 4)
            secret_bytes = base64.b64decode(padded.encode("ascii"), validate=True)

    if secret_bytes is None:
        msg = "Secret is not valid hex or base64 encoded data"
        raise SecretValidationError(msg)

    total_len = len(secret_bytes)

    # 2. Check secret format variants
    if total_len == 16:
        # Standard legacy secret
        return ProxySecret(cleaned), SecretType.LEGACY, None

    if total_len == 17 and secret_bytes[0] == 0xDD:
        # Intermediate randomized secret (0xdd prefix)
        return ProxySecret(cleaned), SecretType.SECURE_RANDOMIZED, None

    if total_len >= 17 and secret_bytes[0] == 0xEE:
        # Fake-TLS secret (0xee prefix)
        if total_len == 17:
            # Fake-TLS without explicit SNI domain
            return ProxySecret(cleaned), SecretType.FAKE_TLS, None

        # Remaining bytes represent the SNI domain
        domain_bytes = secret_bytes[17:]
        try:
            domain_str = domain_bytes.decode("ascii")
        except UnicodeDecodeError:
            msg = "Fake-TLS SNI domain is not valid ASCII text"
            raise SecretValidationError(msg) from None

        domain_clean = domain_str.strip().lower().rstrip(".")
        if not validate_hostname(domain_clean):
            msg = f"Fake-TLS SNI {domain_clean!r} is not a valid hostname"
            raise SecretValidationError(msg)

        return ProxySecret(cleaned), SecretType.FAKE_TLS, domain_clean

    # Any other byte combination is invalid MTProto secret structure
    msg = (
        f"Invalid MTProto secret structure (length={total_len} bytes, "
        f"prefix=0x{secret_bytes[0]:02x} if non-empty). Must be 16 bytes, "
        "17 bytes with 0xdd prefix, or >=17 bytes with 0xee prefix."
    )
    raise SecretValidationError(msg)
