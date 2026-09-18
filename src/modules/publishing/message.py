"""Compatibility wrapper. Channel text lives in :mod:`modules.publishing.formatter`."""

from __future__ import annotations

from modules.publishing.formatter import PublicationFormatter, format_channel_message

__all__ = ["PublicationFormatter", "format_channel_message"]
