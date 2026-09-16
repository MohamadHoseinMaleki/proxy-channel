"""Parse operator-configured discovery sources from settings.

``DISCOVERY_SOURCES`` is a semicolon-separated list of ``kind:value`` entries.

* ``telegram:<channel>`` — public t.me preview of a channel username.
  The channel must be a Telegram identifier (letter, then 5-32 alphanumerics
  and underscores). Paths, ``..``, ``@``, and URL fragments are rejected so
  they cannot be smuggled into ``https://t.me/s/{channel}``.
* ``http:<url>`` / ``https:<url>`` — raw text/HTML over HTTP(S). The URL is
  shape-checked here (scheme + host); DNS and SSRF run at fetch time.

The default is empty: an unconfigured worker ticks honestly and fetches nothing.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from modules.discovery.sources.raw_http import RawHttpSource
from modules.discovery.sources.telegram_web import TelegramWebSource

__all__ = [
    "DiscoverySourceSpecError",
    "parse_discovery_sources",
    "validate_http_source_url",
    "validate_telegram_channel",
]

# Telegram usernames: 5-32 chars, start with a letter, then alphanumerics/_ .
_TELEGRAM_CHANNEL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")

Source = TelegramWebSource | RawHttpSource


class DiscoverySourceSpecError(ValueError):
    """Raised when a single ``DISCOVERY_SOURCES`` entry is malformed."""


def validate_telegram_channel(channel: str) -> str:
    """Return a canonical channel identifier or raise."""
    name = channel.strip().lstrip("@")
    if not name or not _TELEGRAM_CHANNEL_RE.fullmatch(name):
        msg = "Telegram channel must be a 5-32 character identifier"
        raise DiscoverySourceSpecError(msg)
    if ".." in name or "/" in name or "\\" in name or ":" in name:
        msg = "Telegram channel must not contain path separators"
        raise DiscoverySourceSpecError(msg)
    return name


def validate_http_source_url(url: str) -> str:
    """Reject non-http(s) URLs and empty hosts without performing DNS."""
    candidate = url.strip()
    try:
        parsed = urlsplit(candidate)
    except Exception as exc:
        msg = f"Invalid HTTP source URL: {type(exc).__name__}"
        raise DiscoverySourceSpecError(msg) from None
    if parsed.scheme.lower() not in ("http", "https"):
        msg = "HTTP source URL must use http or https"
        raise DiscoverySourceSpecError(msg)
    if not parsed.hostname:
        msg = "HTTP source URL must include a hostname"
        raise DiscoverySourceSpecError(msg)
    if parsed.username or parsed.password:
        msg = "HTTP source URL must not include credentials"
        raise DiscoverySourceSpecError(msg)
    return candidate


def parse_discovery_sources(raw: str) -> list[Source]:
    """Parse ``DISCOVERY_SOURCES`` into source objects.

    Empty / whitespace-only input yields an empty list. Invalid entries raise
    :class:`DiscoverySourceSpecError` so the caller can skip them one at a time.
    """
    sources: list[Source] = []
    text = (raw or "").strip()
    if not text:
        return sources
    for piece in text.split(";"):
        entry = piece.strip()
        if not entry:
            continue
        sources.append(_parse_one(entry))
    return sources


def _parse_one(entry: str) -> Source:
    lowered = entry.lower()
    if lowered.startswith("telegram:"):
        return TelegramWebSource(validate_telegram_channel(entry.split(":", 1)[1]))
    if lowered.startswith("http:"):
        rest = entry.split(":", 1)[1].strip()
        if rest.lower().startswith(("http://", "https://")):
            return RawHttpSource(validate_http_source_url(rest))
        if lowered.startswith(("http://", "https://")):
            return RawHttpSource(validate_http_source_url(entry))
        msg = "HTTP source must be a full http(s) URL after 'http:'"
        raise DiscoverySourceSpecError(msg)
    if lowered.startswith("https://"):
        return RawHttpSource(validate_http_source_url(entry))
    msg = "Source entry must be 'telegram:<channel>' or 'http:<url>'"
    raise DiscoverySourceSpecError(msg)
