"""Deterministic channel message for one :class:`~modules.reporting.models.ReportItem`.

Same input → same text. No clock, no I/O, no Telegram.
"""

from __future__ import annotations

from decimal import Decimal

from modules.reporting.models import ReportItem

__all__ = ["format_channel_message"]


def format_channel_message(item: ReportItem) -> str:
    """Render the standard proxy post.

    Field order is part of the contract. Decimals use ``str()`` so they match
    the reporting JSON payload. The canonical ``tg://proxy?...`` URL is the
    last line.
    """
    lines = [
        "MTProto proxy",
        f"server: {item.server}",
        f"port: {item.port}",
        f"secret: {item.secret.reveal()}",
        f"score: {_dec(item.score)}",
        f"reliability_24h: {_dec(item.reliability_24h)}",
        f"sample_count_24h: {item.sample_count_24h}",
        f"latency_p50_ms: {_dec(item.latency_p50_ms)}",
        f"freshness: {item.freshness}",
        f"secret_type: {item.secret_type}",
        "",
        item.url,
    ]
    return "\n".join(lines)


def _dec(value: Decimal | None) -> str:
    return "-" if value is None else str(value)
