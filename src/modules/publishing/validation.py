"""Pre-publication checks on a :class:`~modules.reporting.models.ReportItem`.

Does not rescore, re-rank, or rewrite ``select_top``. Invalid items must not
reach Telegram. No I/O, no SQLAlchemy, no HTTP.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from core.identity import PROTOCOL_MTPROTO
from modules.discovery.models import SecretType
from modules.discovery.normalizer import (
    DiscoveryValidationError,
    PortValidationError,
    SecretValidationError,
    ServerValidationError,
    validate_and_normalize_port,
    validate_and_normalize_server,
    validate_and_parse_secret,
)
from modules.discovery.parser import ProxyParseError, parse_proxy_url
from modules.reporting.models import ReportItem
from modules.reporting.urls import canonical_tg_proxy_url

__all__ = [
    "PublicationRejection",
    "PublicationValidation",
    "PublicationVerdict",
    "validate_publication",
]


class PublicationVerdict(StrEnum):
    """Whether a selected proxy may be posted."""

    VALID = "VALID"
    INVALID = "INVALID"


class PublicationRejection(StrEnum):
    """Stable reason codes. Safe to log; never a secret or a stack trace."""

    INVALID_PORT = "invalid_port"
    INVALID_HOST = "invalid_host"
    INVALID_PROTOCOL = "invalid_protocol"
    INVALID_SECRET = "invalid_secret"  # noqa: S105
    FAKE_TLS = "fake_tls"
    MALFORMED_PROXY = "malformed_proxy"


@dataclass(frozen=True, slots=True)
class PublicationValidation:
    """Outcome of :func:`validate_publication`. No message body, no secret."""

    verdict: PublicationVerdict
    reason: str | None = None
    url: str | None = None

    @property
    def ok(self) -> bool:
        return self.verdict is PublicationVerdict.VALID

    def __repr__(self) -> str:
        return (
            f"<PublicationValidation verdict={self.verdict} reason={self.reason} "
            f"url={'yes' if self.url else 'no'}>"
        )


def validate_publication(item: object) -> PublicationValidation:
    """Return VALID only when the item is safe to format and post.

    ``select_top`` is not called and not modified. A forged ``ReportItem`` is
    still rejected here (Fake-TLS, bad host, unparseable secret).
    """
    if not isinstance(item, ReportItem):
        return _invalid(PublicationRejection.MALFORMED_PROXY)

    protocol = item.protocol.strip().lower() if isinstance(item.protocol, str) else ""
    if protocol != PROTOCOL_MTPROTO:
        return _invalid(PublicationRejection.INVALID_PROTOCOL)

    try:
        port = validate_and_normalize_port(item.port)
    except PortValidationError:
        return _invalid(PublicationRejection.INVALID_PORT)
    except DiscoveryValidationError:
        return _invalid(PublicationRejection.MALFORMED_PROXY)

    try:
        server = validate_and_normalize_server(item.server)
    except ServerValidationError:
        return _invalid(PublicationRejection.INVALID_HOST)
    except DiscoveryValidationError:
        return _invalid(PublicationRejection.MALFORMED_PROXY)

    try:
        plaintext = item.secret.reveal()
    except Exception:
        return _invalid(PublicationRejection.INVALID_SECRET)
    if not isinstance(plaintext, str) or not plaintext.strip():
        return _invalid(PublicationRejection.INVALID_SECRET)

    try:
        _secret, classified, _sni = validate_and_parse_secret(plaintext)
    except SecretValidationError:
        return _invalid(PublicationRejection.INVALID_SECRET)
    except DiscoveryValidationError:
        return _invalid(PublicationRejection.MALFORMED_PROXY)

    labeled = item.secret_type.strip().lower() if isinstance(item.secret_type, str) else ""
    if classified is SecretType.FAKE_TLS or labeled == SecretType.FAKE_TLS.value:
        return _invalid(PublicationRejection.FAKE_TLS)

    try:
        url = canonical_tg_proxy_url(server=server, port=port, secret=plaintext)
    except ValueError:
        return _invalid(PublicationRejection.MALFORMED_PROXY)

    try:
        parsed = parse_proxy_url(url)
    except ProxyParseError:
        return _invalid(PublicationRejection.MALFORMED_PROXY)
    if parsed.secret_type is SecretType.FAKE_TLS:
        return _invalid(PublicationRejection.FAKE_TLS)

    return PublicationValidation(verdict=PublicationVerdict.VALID, reason=None, url=url)


def _invalid(reason: PublicationRejection) -> PublicationValidation:
    return PublicationValidation(verdict=PublicationVerdict.INVALID, reason=str(reason), url=None)
