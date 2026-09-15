"""Parser for MTProto proxy URLs from text, HTML, and query strings.

Supports the following URL forms:
* ``tg://proxy?server=...&port=...&secret=...``
* ``https://t.me/proxy?server=...&port=...&secret=...``
* ``http://t.me/proxy?server=...&port=...&secret=...``

Guarantees:
1. Reordering query parameters produces the same logical configuration.
2. Percent-encoding is properly decoded.
3. Conflicting duplicate parameters are rejected deterministically.
4. Surrounding whitespace, punctuation, and HTML embedding are handled cleanly.
5. The parser never crashes on malformed external input.
6. Exception messages never leak the proxy secret or complete URL with secret.
"""

from __future__ import annotations

import html
import re
import urllib.parse
from typing import Final

from core.identity import PROTOCOL_MTPROTO
from core.logger import get_logger, scrub_secrets
from modules.discovery.models import MTProtoProxy
from modules.discovery.normalizer import (
    DiscoveryValidationError,
    validate_and_normalize_port,
    validate_and_normalize_server,
    validate_and_parse_secret,
)

__all__ = [
    "ProxyParseError",
    "extract_proxy_urls",
    "parse_proxy_text",
    "parse_proxy_url",
]

_logger = get_logger("modules.discovery.parser")

#: Regex for locating candidate proxy links in raw text or HTML attributes.
_URL_CANDIDATE_RE: Final = re.compile(
    r"""(?:tg://proxy\?[^\s"\'<>]+|https?://t\.me/proxy\?[^\s"\'<>]+)""",
    re.IGNORECASE,
)

#: Trailing punctuation to strip from extracted URLs.
_TRAILING_PUNCTUATION: Final = ".,;:!?)'\"[]{}<>"


class ProxyParseError(ValueError):
    """Raised when a proxy URL cannot be parsed or validated.

    Messages are strictly scrubbed of secrets.
    """


def parse_proxy_url(raw_url: str) -> MTProtoProxy:
    """Parse a single MTProto proxy URL into an MTProtoProxy domain object.

    Raises :class:`ProxyParseError` if the URL is invalid or malformed.
    """
    cleaned = raw_url.strip()
    if not cleaned:
        msg = "Proxy URL must not be empty"
        raise ProxyParseError(msg)

    # Strip surrounding quotes if any
    if (cleaned.startswith('"') and cleaned.endswith('"')) or (
        cleaned.startswith("'") and cleaned.endswith("'")
    ):
        cleaned = cleaned[1:-1].strip()

    try:
        parsed = urllib.parse.urlsplit(cleaned)
    except Exception as exc:
        msg = f"Malformed URL syntax: {type(exc).__name__}"
        raise ProxyParseError(msg) from None

    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    path = parsed.path.rstrip("/")

    # 1. Scheme and endpoint validation
    if scheme == "tg":
        # tg://proxy?... or tg://proxy/?...
        is_valid_tg = (netloc == "proxy" and path in ("", "/")) or (
            netloc == "" and path in ("proxy", "/proxy")
        )
        if not is_valid_tg:
            msg = f"Invalid tg:// path or netloc (expected tg://proxy?...): {netloc!r}/{path!r}"
            raise ProxyParseError(msg)
    elif scheme in ("http", "https"):
        # http(s)://t.me/proxy?...
        # Host must be t.me (strip default ports if present)
        host = netloc.split(":")[0]
        if host != "t.me":
            msg = f"Invalid HTTP proxy host {host!r} (expected 't.me')"
            raise ProxyParseError(msg)
        if path != "/proxy":
            msg = f"Invalid HTTP proxy path {path!r} (expected '/proxy')"
            raise ProxyParseError(msg)
    else:
        msg = f"Unsupported proxy URL scheme: {scheme!r} (must be tg, https, or http)"
        raise ProxyParseError(msg)

    # 2. Query parameter extraction
    if not parsed.query:
        msg = "Proxy URL is missing query parameters"
        raise ProxyParseError(msg)

    try:
        query_params = urllib.parse.parse_qs(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=False,
            errors="strict",
        )
    except Exception as exc:
        msg = f"Malformed query string: {type(exc).__name__}"
        raise ProxyParseError(msg) from None

    # Normalise query parameter keys to lowercase
    normalized_params: dict[str, list[str]] = {}
    for key, values in query_params.items():
        normalized_params[key.lower()] = values

    # Check for required parameters: server, port, secret
    required_keys = ("server", "port", "secret")
    for req in required_keys:
        if req not in normalized_params:
            msg = f"Missing required query parameter: {req!r}"
            raise ProxyParseError(msg)

    # Check for conflicting duplicate values
    extracted: dict[str, str] = {}
    for req in required_keys:
        values = normalized_params[req]
        if not values:
            msg = f"Query parameter {req!r} has no value"
            raise ProxyParseError(msg)

        unique_values = {v.strip() for v in values}
        if len(unique_values) > 1:
            msg = f"Conflicting duplicate values for query parameter {req!r}"
            raise ProxyParseError(msg)

        val = unique_values.pop()
        if not val:
            msg = f"Query parameter {req!r} must not be empty"
            raise ProxyParseError(msg)
        extracted[req] = val

    # 3. Structural validation using normalizer
    try:
        server = validate_and_normalize_server(extracted["server"])
    except DiscoveryValidationError as exc:
        raise ProxyParseError(f"Server validation error: {exc}") from None

    try:
        port = validate_and_normalize_port(extracted["port"])
    except DiscoveryValidationError as exc:
        raise ProxyParseError(f"Port validation error: {exc}") from None

    try:
        secret, secret_type, sni_domain = validate_and_parse_secret(extracted["secret"])
    except DiscoveryValidationError as exc:
        # Crucial: do NOT echo the invalid secret in the exception message!
        raise ProxyParseError(f"Secret validation error: {exc}") from None

    return MTProtoProxy(
        server=server,
        port=port,
        secret=secret,
        protocol=PROTOCOL_MTPROTO,
        secret_type=secret_type,
        sni_domain=sni_domain,
    )


def extract_proxy_urls(content: str) -> list[str]:
    """Extract candidate Telegram MTProto proxy URLs from arbitrary text or HTML.

    Handles HTML entity unescaping, surrounding punctuation, and duplicate link removal.
    """
    if not content:
        return []

    # Unescape HTML entities (e.g. &amp; -> &, &quot; -> ", etc.)
    unescaped = html.unescape(content)

    candidates: list[str] = []
    seen: set[str] = set()

    for match in _URL_CANDIDATE_RE.finditer(unescaped):
        url = match.group(0)
        # Strip trailing punctuation often attached in plain text
        url = url.rstrip(_TRAILING_PUNCTUATION)
        if url and url not in seen:
            seen.add(url)
            candidates.append(url)

    return candidates


def parse_proxy_text(content: str) -> list[MTProtoProxy]:
    """Extract and parse all valid MTProto proxies from arbitrary text or HTML.

    Malformed candidates are skipped safely. Result is deduplicated by fingerprint.
    """
    urls = extract_proxy_urls(content)
    proxies: list[MTProtoProxy] = []
    seen_fingerprints: set[str] = set()

    for raw_url in urls:
        try:
            proxy = parse_proxy_url(raw_url)
        except ProxyParseError as exc:
            _logger.debug("proxy_url_parse_failed", error=str(exc), url=scrub_secrets(raw_url))
            continue

        if proxy.fingerprint not in seen_fingerprints:
            seen_fingerprints.add(proxy.fingerprint)
            proxies.append(proxy)

    return proxies
