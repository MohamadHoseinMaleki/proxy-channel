"""Telegram Bot API implementation of :class:`TelegramPublisher`.

Talks only to ``https://api.telegram.org``. The URL is constructed here, never
taken from the environment, so a mis-set channel id cannot become an SSRF
gadget. Token lives in the path (Bot API convention) and is never logged.
"""

from __future__ import annotations

import json
import re
from typing import Any, Final

import httpx
from pydantic import SecretStr

from core.logger import safe_error_message
from modules.publishing.protocol import PublishResult

__all__ = [
    "BOT_API_BASE",
    "BotApiTelegramPublisher",
    "TelegramPublishError",
    "is_retryable_status",
]

BOT_API_BASE: Final = "https://api.telegram.org"
_TOKEN_RE: Final = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{20,}$")
_PERMANENT_CODES: Final = frozenset({400, 401, 403, 404})


class TelegramPublishError(Exception):
    """Raised only for programmer errors (bad token / empty channel)."""


def is_retryable_status(code: int | None) -> bool:
    """Whether an HTTP / Bot API error code should be retried.

    ``None`` means a transport failure (timeout, connect error) — retry.
    ``429`` and ``5xx`` are transient. ``400``/``401``/``403``/``404`` are not.
    """
    if code is None:
        return True
    return bool(code == 429 or code >= 500)


class BotApiTelegramPublisher:
    """``sendMessage`` against the Bot API. Inject ``transport`` in tests."""

    def __init__(
        self,
        *,
        token: str | SecretStr,
        channel_id: str,
        timeout_seconds: float = 15.0,
        connect_timeout_seconds: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        plaintext = token.get_secret_value() if isinstance(token, SecretStr) else token
        if not isinstance(plaintext, str) or not _TOKEN_RE.match(plaintext.strip()):
            msg = "telegram_bot_token is missing or not a Bot API token"
            raise TelegramPublishError(msg)
        chat = channel_id.strip() if isinstance(channel_id, str) else ""
        if not chat:
            msg = "telegram_channel_id must not be empty"
            raise TelegramPublishError(msg)
        self._token = plaintext.strip()
        self._channel_id = chat
        self._timeout = httpx.Timeout(
            connect=min(connect_timeout_seconds, timeout_seconds),
            read=timeout_seconds,
            write=timeout_seconds,
            pool=timeout_seconds,
        )
        self._transport = transport
        self._client = client
        self._owns_client = client is None

    def _url(self) -> str:
        return f"{BOT_API_BASE}/bot{self._token}/sendMessage"

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _session(self) -> httpx.AsyncClient:
        if self._client is None:
            kwargs: dict[str, Any] = {
                "timeout": self._timeout,
                "follow_redirects": False,
            }
            if self._transport is not None:
                kwargs["transport"] = self._transport
            self._client = httpx.AsyncClient(**kwargs)
        return self._client

    async def publish(self, message: str) -> PublishResult:
        if not isinstance(message, str) or not message.strip():
            return PublishResult(
                ok=False,
                telegram_message_id=None,
                error_safe="empty_message",
                error_code=400,
                retryable=False,
            )
        payload = {
            "chat_id": self._channel_id,
            "text": message,
            "disable_web_page_preview": True,
        }
        try:
            client = await self._session()
            response = await client.post(self._url(), json=payload)
        except httpx.TimeoutException as exc:
            return PublishResult(
                ok=False,
                telegram_message_id=None,
                error_safe=safe_error_message(exc) or type(exc).__name__,
                retryable=True,
            )
        except httpx.HTTPError as exc:
            return PublishResult(
                ok=False,
                telegram_message_id=None,
                error_safe=safe_error_message(exc) or type(exc).__name__,
                retryable=True,
            )

        return _parse_send_message_response(response)


def _parse_send_message_response(response: httpx.Response) -> PublishResult:
    retry_after = _retry_after_from_header(response)
    try:
        body: Any = response.json()
    except json.JSONDecodeError:
        return PublishResult(
            ok=False,
            telegram_message_id=None,
            error_safe=f"HTTP {response.status_code}: invalid JSON",
            error_code=response.status_code or None,
            retry_after=retry_after,
            retryable=True,
        )
    if not isinstance(body, dict):
        return PublishResult(
            ok=False,
            telegram_message_id=None,
            error_safe=f"HTTP {response.status_code}: unexpected payload",
            error_code=response.status_code or None,
            retry_after=retry_after,
            retryable=True,
        )
    retry_after = _max_retry_after(retry_after, _retry_after_from_body(body))
    if body.get("ok") is True:
        result = body.get("result")
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if isinstance(message_id, int) and not isinstance(message_id, bool) and message_id > 0:
            return PublishResult(ok=True, telegram_message_id=message_id, error_safe=None)
        return PublishResult(
            ok=False,
            telegram_message_id=None,
            error_safe="missing telegram message id",
            retryable=True,
        )
    description = body.get("description")
    raw_code = body.get("error_code", response.status_code)
    code = raw_code if isinstance(raw_code, int) and not isinstance(raw_code, bool) else None
    detail = (
        description if isinstance(description, str) and description.strip() else "telegram_error"
    )
    retryable = is_retryable_status(code)
    if code in _PERMANENT_CODES:
        retryable = False
    return PublishResult(
        ok=False,
        telegram_message_id=None,
        error_safe=safe_error_message(f"HTTP {code}: {detail}") or "telegram_error",
        error_code=code,
        retry_after=retry_after,
        retryable=retryable,
    )


def _retry_after_from_body(body: dict[str, Any]) -> float | None:
    params = body.get("parameters")
    if not isinstance(params, dict):
        return None
    raw = params.get("retry_after")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    if raw < 0:
        return None
    return float(raw)


def _retry_after_from_header(response: httpx.Response) -> float | None:
    header = response.headers.get("Retry-After")
    if header is None:
        return None
    try:
        value = float(header.strip())
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    return value


def _max_retry_after(left: float | None, right: float | None) -> float | None:
    if left is None:
        return right
    if right is None:
        return left
    return max(left, right)
