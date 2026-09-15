"""MTProxy transport selection and protocol capabilities.

Telethon MTProxy transport architecture:
* ConnectionTcpMTProxyAbridged: 1-byte length header, only for legacy 16-byte secrets.
* ConnectionTcpMTProxyIntermediate: 4-byte length header, only for legacy 16-byte secrets.
* ConnectionTcpMTProxyRandomizedIntermediate: 4-byte length header with randomized padding,
  mandatory for 0xdd secrets and compatible with legacy 16-byte secrets.
* Fake-TLS (0xee): Telethon >= 1.35.0 parses/normalizes 0xee secrets by stripping the prefix
  and truncating to 16 bytes, but MTProxyIO does NOT implement TLS ClientHello or SNI
  emulation. Wire-level Fake-TLS proxies that enforce TLS handshakes will drop this traffic.
  We explicitly isolate and classify this transport as unsupported until a dedicated TLS
  transport wrapper is added.
"""

from __future__ import annotations

from typing import Any

from telethon.network.connection.tcpmtproxy import (
    ConnectionTcpMTProxyAbridged,
    ConnectionTcpMTProxyIntermediate,
    ConnectionTcpMTProxyRandomizedIntermediate,
)

from modules.discovery.models import SecretType
from modules.tester.models import TransportType

__all__ = [
    "ConnectionTcpMTProxyAbridged",
    "ConnectionTcpMTProxyIntermediate",
    "ConnectionTcpMTProxyRandomizedIntermediate",
    "TransportSelection",
    "select_transport",
]


class TransportSelection:
    """The transport class and support status chosen for a secret format."""

    __slots__ = ("is_supported", "reason", "transport_cls", "transport_type")

    def __init__(
        self,
        transport_cls: type[Any] | None,
        transport_type: TransportType,
        *,
        is_supported: bool,
        reason: str | None = None,
    ) -> None:
        self.transport_cls = transport_cls
        self.transport_type = transport_type
        self.is_supported = is_supported
        self.reason = reason

    def __repr__(self) -> str:
        return (
            f"<TransportSelection type={self.transport_type} "
            f"supported={self.is_supported} reason={self.reason!r}>"
        )


def select_transport(secret_type: SecretType | str) -> TransportSelection:
    """Select the appropriate MTProxy transport for a given secret type.

    Guarantees:
    * 0xdd secrets use Randomized Intermediate (mandatory for 17-byte dd secrets).
    * Legacy secrets use Randomized Intermediate (maximum firewall evasion).
    * 0xee fake-TLS secrets are identified as unsupported by the upstream library
      rather than faking wire-level Fake-TLS support.
    """
    if isinstance(secret_type, SecretType):
        stype = secret_type
    else:
        try:
            stype = SecretType(secret_type)
        except ValueError:
            try:
                stype = SecretType[secret_type.upper()]
            except KeyError:
                msg = f"Unknown secret type {secret_type!r}"
                raise ValueError(msg) from None

    if stype in (SecretType.SECURE_RANDOMIZED, SecretType.LEGACY):
        return TransportSelection(
            ConnectionTcpMTProxyRandomizedIntermediate,
            TransportType.RANDOMIZED_INTERMEDIATE,
            is_supported=True,
        )

    # Fake-TLS (0xee): Documented limitation — Telethon does not implement
    # TLS ClientHello or SNI emulation.
    return TransportSelection(
        None,
        TransportType.FAKE_TLS,
        is_supported=False,
        reason=(
            "Telethon does not implement wire-level Fake-TLS emulation "
            "(TLS ClientHello and SNI are not generated)"
        ),
    )
