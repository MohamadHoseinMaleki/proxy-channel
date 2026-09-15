"""Discovery domain models for MTProto proxies.

These models represent discovered proxies, parsed configurations, and discovery
events prior to or during persistence.

Key design invariants:
1. Safe by default: ``repr()`` and ``str()`` never expose the plaintext secret.
2. Reuses ``core.identity.ProxySecret``: no duplicate secret wrappers.
3. Reuses ``core.identity.compute_fingerprint``: identity is unified.
4. Transport information: fake-TLS SNI domain and secret format are preserved.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from core.identity import (
    PROTOCOL_MTPROTO,
    ProxySecret,
    compute_fingerprint,
    normalize_server,
)
from core.logger import scrub_secrets
from core.models import SourceType, utcnow

__all__ = [
    "DiscoveredProxyCandidate",
    "MTProtoProxy",
    "SecretType",
]


class SecretType(StrEnum):
    """The wire format of an MTProto proxy secret.

    Determined by inspecting the decoded byte sequence:
    * LEGACY: 16-byte raw key (classic intermediate/abridged MTProto).
    * SECURE_RANDOMIZED (dd): 17 bytes starting with 0xdd (random padding).
    * FAKE_TLS (ee): >= 17 bytes starting with 0xee (TLS emulation with optional SNI).
    """

    LEGACY = "legacy"
    SECURE_RANDOMIZED = "dd"
    FAKE_TLS = "ee"


@dataclass(frozen=True, slots=True)
class MTProtoProxy:
    """Domain representation of a validated, normalised MTProto proxy configuration.

    This object is immutable and safe to log or print in debug output: its
    ``repr()`` and ``str()`` representations only display the masked secret.
    The raw secret is only accessible through ``secret.reveal()``.
    """

    server: str
    port: int
    secret: ProxySecret
    protocol: str = field(default=PROTOCOL_MTPROTO)
    secret_type: SecretType = field(default=SecretType.LEGACY)
    sni_domain: str | None = field(default=None)
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        # Enforce canonical server form (strip brackets, lowercase, strip trailing dot).
        canonical_server = normalize_server(self.server)
        object.__setattr__(self, "server", canonical_server)

        # Precompute the deterministic SHA-256 fingerprint.
        fp = compute_fingerprint(
            server=canonical_server,
            port=self.port,
            secret=self.secret.reveal(),
            protocol=self.protocol,
        )
        object.__setattr__(self, "fingerprint", fp)

    # -- Safe string representations (Secrets never leaked) -----------------

    def __repr__(self) -> str:
        sni_repr = f" sni={self.sni_domain!r}" if self.sni_domain else ""
        return (
            f"<MTProtoProxy {self.protocol}://{self.server}:{self.port} "
            f"type={self.secret_type.value} secret={self.secret.masked!r}{sni_repr} "
            f"fp={self.fingerprint[:12]}...>"
        )

    def __str__(self) -> str:
        return f"{self.protocol}://{self.server}:{self.port}?secret={self.secret.masked}"


@dataclass(frozen=True, slots=True)
class DiscoveredProxyCandidate:
    """A proxy discovered from an external source with full provenance.

    The ``raw_reference`` field is automatically scrubbed upon construction
    so that raw URLs containing plaintext secrets cannot leak into persistence
    or exception strings.
    """

    proxy: MTProtoProxy
    source_type: SourceType | str
    source_name: str
    source_url: str | None = None
    raw_reference: str | None = None
    discovered_at: datetime = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        if self.raw_reference:
            object.__setattr__(self, "raw_reference", scrub_secrets(self.raw_reference))

    def __repr__(self) -> str:
        return (
            f"<DiscoveredProxyCandidate proxy={self.proxy!r} "
            f"source={self.source_type}:{self.source_name}>"
        )
