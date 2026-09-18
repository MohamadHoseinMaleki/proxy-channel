"""Deterministic retry delay. No sleep, no I/O, no clock."""

from __future__ import annotations

from typing import Final

__all__ = [
    "DEFAULT_LEASE_SECONDS",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_RETRY_BASE_SECONDS",
    "DEFAULT_RETRY_MAX_SECONDS",
    "retry_delay_seconds",
]

DEFAULT_LEASE_SECONDS: Final = 60.0
DEFAULT_MAX_RETRIES: Final = 8
DEFAULT_RETRY_BASE_SECONDS: Final = 2.0
DEFAULT_RETRY_MAX_SECONDS: Final = 300.0


def retry_delay_seconds(
    *,
    attempt: int,
    base_seconds: float = DEFAULT_RETRY_BASE_SECONDS,
    max_seconds: float = DEFAULT_RETRY_MAX_SECONDS,
    retry_after: float | None = None,
) -> float:
    """Exponential backoff for the *next* send after ``attempt`` failures.

    ``attempt`` is 1-based (the attempt that just failed). Delay is
    ``min(base * 2**(attempt-1), max)``, then at least ``retry_after`` when
    Telegram sent one. Never sleeps.
    """
    if attempt < 1:
        attempt = 1
    if base_seconds <= 0 or max_seconds <= 0:
        msg = "backoff bounds must be positive"
        raise ValueError(msg)
    delay = min(base_seconds * (2 ** (attempt - 1)), max_seconds)
    if retry_after is not None and retry_after > 0:
        delay = max(delay, retry_after)
    return float(delay)
