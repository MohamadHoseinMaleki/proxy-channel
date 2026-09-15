"""Proxy discovery source adapters."""

from __future__ import annotations

from modules.discovery.sources.base import BaseSource
from modules.discovery.sources.raw_http import RawHttpSource, RawTextSource
from modules.discovery.sources.telegram_web import TelegramWebSource

__all__ = [
    "BaseSource",
    "RawHttpSource",
    "RawTextSource",
    "TelegramWebSource",
]
