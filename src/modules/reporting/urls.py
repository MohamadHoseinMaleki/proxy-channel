"""Canonical ``tg://proxy?...`` URLs for the reporting TXT payload.

Reuses identity normalisation. Does not invent a second URL dialect.
Callers must not pass Fake-TLS secrets; this helper does not classify.
"""

from __future__ import annotations

import urllib.parse

from core.identity import ProxySecret, normalize_server

__all__ = ["canonical_tg_proxy_url"]


def canonical_tg_proxy_url(*, server: str, port: int, secret: str | ProxySecret) -> str:
    """Build ``tg://proxy?server=&port=&secret=`` with RFC 3986 query encoding.

    ``quote`` (not ``quote_plus``) so a ``+`` in a base64 secret is ``%2B``,
    not a space. The discovery parser round-trips this form.
    """
    host = normalize_server(server)
    if not host:
        msg = "server must not be empty"
        raise ValueError(msg)
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        msg = "port must be an int in 1..65535"
        raise ValueError(msg)
    plaintext = secret.reveal() if isinstance(secret, ProxySecret) else secret
    if not isinstance(plaintext, str) or not plaintext.strip():
        msg = "secret must not be empty"
        raise ValueError(msg)
    query = urllib.parse.urlencode(
        {"server": host, "port": str(port), "secret": plaintext.strip()},
        quote_via=urllib.parse.quote,
        safe="",
    )
    return f"tg://proxy?{query}"
