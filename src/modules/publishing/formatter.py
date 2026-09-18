"""Deterministic Telegram channel message. No I/O, no Telegram, no SQLAlchemy.

Publisher does not choose business content: it posts whatever this module
renders after validation has already accepted the item.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

from modules.reporting.models import ReportItem
from modules.reporting.urls import canonical_tg_proxy_url

__all__ = [
    "TELEGRAM_MESSAGE_MAX_LENGTH",
    "PublicationFormatter",
    "escape_publication_field",
    "format_channel_message",
]

#: Bot API ``sendMessage`` text cap.
TELEGRAM_MESSAGE_MAX_LENGTH: Final = 4096

#: Characters that would become markup if ``parse_mode`` were ever enabled.
_UNSAFE: Final = frozenset("*_[]()`~<>&\\")


def escape_publication_field(value: str) -> str:
    """Neutralise control chars and markup in an untrusted display field.

    Hostnames that pass validation have none of these; this is defence in
    depth so a future ``parse_mode`` cannot inject. Newlines are stripped so
    a field cannot steal the last-line URL contract.
    """
    chars: list[str] = []
    for char in value:
        if char in _UNSAFE or ord(char) < 32:
            chars.append(" ")
        elif char.isprintable():
            chars.append(char)
    return " ".join("".join(chars).split())


class PublicationFormatter:
    """Build the standard plaintext channel post.

    Same ``ReportItem`` → same string. Country/location is omitted: the
    schema does not store it, and this formatter will not invent it.
    The MTProto secret is not a labeled field; it appears only inside the
    canonical ``tg://proxy?...`` last line, which users need in order to
    connect.
    """

    def format(self, item: ReportItem, *, limit: int = TELEGRAM_MESSAGE_MAX_LENGTH) -> str:
        if limit < 1:
            msg = "limit must be positive"
            raise ValueError(msg)
        url = canonical_tg_proxy_url(server=item.server, port=item.port, secret=item.secret)
        essential = [
            "MTProto proxy",
            f"proxy: {escape_publication_field(item.server)}:{item.port}",
            f"protocol: {escape_publication_field(item.protocol)}",
            f"status: {escape_publication_field(str(item.freshness))}",
            f"last_checked: {escape_publication_field(item.last_success_at.isoformat())}",
            f"quality: score {_dec(item.score)}",
        ]
        optional: list[str] = []
        if item.reliability_24h is not None:
            optional.append(f"reliability_24h: {_dec(item.reliability_24h)}")
        optional.append(f"sample_count_24h: {item.sample_count_24h}")
        if item.latency_p50_ms is not None:
            optional.append(f"latency_p50_ms: {_dec(item.latency_p50_ms)}")
        if item.secret_type:
            optional.append(f"secret_type: {escape_publication_field(item.secret_type)}")
        return _fit_message(essential, optional, url, limit=limit)


def format_channel_message(item: ReportItem, *, limit: int = TELEGRAM_MESSAGE_MAX_LENGTH) -> str:
    """Module-level entry used by the publisher. Deterministic."""
    return PublicationFormatter().format(item, limit=limit)


def _fit_message(
    essential: list[str],
    optional: list[str],
    url: str,
    *,
    limit: int,
) -> str:
    """Drop optional lines from the bottom until the message fits.

    The canonical URL is never dropped. Essential lines stay unless even
    they plus the URL exceed ``limit`` (then trailing essential lines go,
    still never the URL).
    """
    kept_optional = list(optional)
    text = _render(essential, kept_optional, url)
    while kept_optional and len(text) > limit:
        kept_optional.pop()
        text = _render(essential, kept_optional, url)
    if len(text) <= limit:
        return text
    kept_essential = list(essential)
    while len(kept_essential) > 1 and len(_render(kept_essential, [], url)) > limit:
        kept_essential.pop()
    return _render(kept_essential, [], url)


def _render(essential: list[str], optional: list[str], url: str) -> str:
    lines = [*essential, *optional, "", url]
    return "\n".join(lines)


def _dec(value: Decimal | None) -> str:
    return "-" if value is None else str(value)
