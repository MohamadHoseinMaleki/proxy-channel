"""Deterministic proxy reporting and selection for future publishers."""

from __future__ import annotations

from modules.reporting.models import Report, ReportItem
from modules.reporting.policy import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    MAX_SUCCESS_AGE_HOURS,
    coerce_limit,
)
from modules.reporting.selector import PublishCandidate, select_publishable
from modules.reporting.service import ReportingService
from modules.reporting.urls import canonical_tg_proxy_url

__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "MAX_SUCCESS_AGE_HOURS",
    "PublishCandidate",
    "Report",
    "ReportItem",
    "ReportingService",
    "canonical_tg_proxy_url",
    "coerce_limit",
    "select_publishable",
]
