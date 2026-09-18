"""Telegram channel publishing over Task 012 selection."""

from __future__ import annotations

from modules.publishing.bot_api import BotApiTelegramPublisher
from modules.publishing.fake import FakeTelegramPublisher
from modules.publishing.formatter import PublicationFormatter, format_channel_message
from modules.publishing.health import PublicationHealth, PublicationHealthService
from modules.publishing.metrics import PublicationMetrics
from modules.publishing.observe import PublicationErrorClass
from modules.publishing.protocol import PublishResult, TelegramPublisher
from modules.publishing.scheduler import PublicationScheduler
from modules.publishing.service import PublishCycleResult, PublishingService
from modules.publishing.validation import (
    PublicationRejection,
    PublicationValidation,
    PublicationVerdict,
    validate_publication,
)

__all__ = [
    "BotApiTelegramPublisher",
    "FakeTelegramPublisher",
    "PublicationErrorClass",
    "PublicationFormatter",
    "PublicationHealth",
    "PublicationHealthService",
    "PublicationMetrics",
    "PublicationRejection",
    "PublicationScheduler",
    "PublicationValidation",
    "PublicationVerdict",
    "PublishCycleResult",
    "PublishResult",
    "PublishingService",
    "TelegramPublisher",
    "format_channel_message",
    "validate_publication",
]
