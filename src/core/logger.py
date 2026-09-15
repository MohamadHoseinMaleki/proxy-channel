"""Structured logging with mandatory secret redaction.

Two guarantees this module makes:

1. Every log record is a structured event (key/value), rendered as JSON outside
   development and as human-readable console output during development.
2. Credentials never reach the output. Redaction runs as a *processor*, i.e.
   after ``format_exc_info``, so it also scrubs secrets that appear inside
   exception messages and tracebacks -- not just in explicit kwargs.

The platform handles four classes of secret that must never be logged:
MTProto proxy secrets, Telegram bot tokens / API hashes, AI provider API keys
and database passwords.
"""

from __future__ import annotations

import contextlib
import importlib.util
import logging
import os
import re
import sys
from collections.abc import Mapping
from typing import Any

import structlog
from structlog.contextvars import bind_contextvars, clear_contextvars, unbind_contextvars
from structlog.types import EventDict, Processor, WrappedLogger

from core.config import Settings, get_settings

__all__ = [
    "DEFAULT_ERROR_MESSAGE_LIMIT",
    "REDACTED",
    "SENSITIVE_KEY_PATTERN",
    "bind_worker_context",
    "configure_logging",
    "get_logger",
    "redact",
    "redact_secrets",
    "safe_error_message",
    "scrub_secrets",
    "unbind_worker_context",
]

#: Replacement marker written in place of any redacted value.
REDACTED = "***REDACTED***"

# Key names whose *values* are always credentials. Matched as whole
# underscore/dash/dot separated segments so that ``error_category`` or
# ``source_url`` are never touched.
SENSITIVE_KEY_PATTERN = (
    r"(?:^|[_\-.])(?:"
    r"secret|secrets|proxy_secret|mtproto_secret|"
    r"token|tokens|bot_token|access_token|refresh_token|"
    r"password|passwd|pwd|"
    r"api_hash|apihash|api_key|apikey|"
    r"authorization|auth|"
    r"private_key|session_string|session_file|"
    r"database_url|db_url|dsn"
    r")(?:$|[_\-.])"
)

# ``secret=<value>`` style parameters embedded inside URLs and link strings,
# e.g. ``tg://proxy?server=1.2.3.4&port=443&secret=ee...``
# The three suppression comments below are deliberate: these constants are
# credential *detection* patterns, not credentials (ruff S105 is name-based).
_EMBEDDED_SECRET_PATTERN = (
    r"(?i)\b(secret|password|passwd|token|api_key|apikey|api_hash)"  # noqa: S105
    r"([=:]\s*)([^&\s'\"<>]+)"
)

# ``scheme://user:password@host`` -- the password component of a DSN.
_DSN_PASSWORD_PATTERN = r"(?i)\b([a-z][a-z0-9+.\-]*://[^:/@\s]+:)([^@\s/]+)(@)"  # noqa: S105

# Telegram Bot API tokens embedded in a request URL path: ``/bot<id>:<token>/``.
_BOT_TOKEN_PATTERN = r"(?i)\b(bot\d{5,}:)([A-Za-z0-9_\-]{25,})"  # noqa: S105

# A bare run of 32+ hex characters: an MTProto secret is 16 bytes (32 hex
# digits), usually ``ee``-prefixed, and a fake-TLS one is longer still.
#
# This pattern exists because key-based matching cannot reach it. When a CHECK
# constraint rejects a row, PostgreSQL appends ``DETAIL: Failing row contains
# (...)`` and echoes *every column*, so the secret arrives with no ``secret=``
# key in front of it -- verified against a real server, not assumed. The same
# happens for exclusion/unique violation detail and for NOTICE output.
#
# The cost is that 64-character proxy fingerprints are masked in error text too.
# Accepted deliberately: a fingerprint is derivable from the row and rarely
# belongs in an error message, whereas a secret in one is a credential leak that
# lands in log aggregators and in ``proxy_observations.error_message_safe``.
_HEX_SECRET_PATTERN = r"\b[0-9a-fA-F]{32,}\b"  # noqa: S105

_MAX_REDACTION_DEPTH = 12

#: Hard cap on persisted error text. Mirrors the CHECK constraint on
#: ``proxy_observations.error_message_safe``; keep the two in sync.
DEFAULT_ERROR_MESSAGE_LIMIT = 500

_KEY_RE = re.compile(SENSITIVE_KEY_PATTERN, re.IGNORECASE)
_EMBEDDED_RE = re.compile(_EMBEDDED_SECRET_PATTERN)
_DSN_RE = re.compile(_DSN_PASSWORD_PATTERN)
_BOT_TOKEN_RE = re.compile(_BOT_TOKEN_PATTERN)
_HEX_SECRET_RE = re.compile(_HEX_SECRET_PATTERN)

_configured = False

#: Computed once; a plain ``bool`` so mypy does not prune the Windows branch
#: as unreachable when type-checking for a POSIX target.
_IS_WINDOWS: bool = sys.platform == "win32"


class _CurrentStdoutHandler(logging.StreamHandler):
    """``StreamHandler`` that re-resolves ``sys.stdout`` on every emit.

    Binding late means log output follows the stream that is current at emit
    time rather than at configuration time. That matters for systemd/journald
    redirection, for ``contextlib.redirect_stdout`` and for test capture, all of
    which swap ``sys.stdout`` after logging has already been configured.
    """

    def __init__(self) -> None:
        super().__init__(sys.stdout)

    def emit(self, record: logging.LogRecord) -> None:
        self.stream = sys.stdout
        super().emit(record)

    def flush(self) -> None:
        self.stream = sys.stdout
        super().flush()


def _is_sensitive_key(key: str) -> bool:
    return _KEY_RE.search(key) is not None


def _scrub_string(value: str) -> str:
    value = _EMBEDDED_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", value)
    value = _DSN_RE.sub(lambda m: f"{m.group(1)}{REDACTED}{m.group(3)}", value)
    value = _BOT_TOKEN_RE.sub(lambda m: f"{m.group(1)}{REDACTED}", value)
    # Last: the REDACTED marker itself contains no hex run, so masking bare
    # secrets cannot corrupt an earlier substitution.
    return _HEX_SECRET_RE.sub(REDACTED, value)


def scrub_secrets(text: str) -> str:
    """Mask credentials embedded inside a single string.

    Public wrapper around the value-level scrubber used by :func:`redact`.
    Exposed because sanitisation is needed in two places that must agree exactly:
    log output, and the ``error_message_safe`` column persisted with every
    :class:`~core.models.ProxyObservation`. Sharing one implementation means a
    pattern fixed in one place cannot leak through the other.
    """
    return _scrub_string(text)


def safe_error_message(
    value: BaseException | str | None, *, limit: int = DEFAULT_ERROR_MESSAGE_LIMIT
) -> str | None:
    """Build a persistable, secret-free error message.

    Deliberately keeps only ``"<ExceptionType>: <message>"`` -- never a traceback
    (``core.models`` enforces a length cap at the database level too). The result
    is passed through :func:`scrub_secrets` so a secret that reached an exception
    message cannot be written to ``proxy_observations``.
    """
    if value is None:
        return None
    if isinstance(value, BaseException):
        # An exception raised with no message is common (`raise TimeoutError`,
        # a bare `CancelledError`). The type name is then the only diagnostic
        # available, so keep it rather than returning None -- but drop the
        # dangling colon that a naive f-string would leave behind.
        name = type(value).__name__
        detail = str(value).strip()
        text = f"{name}: {detail}" if detail else name
    else:
        text = str(value).strip()
    if not text:
        return None
    text = " ".join(text.split())  # collapse newlines/traceback-ish whitespace
    scrubbed = scrub_secrets(text)
    if len(scrubbed) > limit:
        return scrubbed[: limit - 1].rstrip() + "\u2026"
    return scrubbed


def redact(value: Any, *, _depth: int = 0) -> Any:
    """Recursively mask credentials inside arbitrary data structures.

    Returns a new object; the input is never mutated (important because
    SQLAlchemy rows and Pydantic models are frequently shared).
    """
    if _depth > _MAX_REDACTION_DEPTH:
        return REDACTED
    if isinstance(value, str):
        return _scrub_string(value)
    if isinstance(value, Mapping):
        return {
            key: (
                REDACTED
                if isinstance(key, str) and _is_sensitive_key(key)
                else redact(item, _depth=_depth + 1)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        items = (redact(item, _depth=_depth + 1) for item in value)
        return type(value)(items) if isinstance(value, tuple) else list(items)
    if isinstance(value, (set, frozenset)):
        return type(value)(redact(item, _depth=_depth + 1) for item in value)
    return value


def redact_secrets(_logger: WrappedLogger, _method_name: str, event_dict: EventDict) -> EventDict:
    """structlog processor that masks credentials anywhere in the event."""
    for key in list(event_dict):
        if isinstance(key, str) and _is_sensitive_key(key):
            event_dict[key] = REDACTED
        else:
            event_dict[key] = redact(event_dict[key])
    return event_dict


def _has_colorama() -> bool:
    """Whether ``colorama`` is importable, without importing it."""
    with contextlib.suppress(ImportError, ValueError):
        return importlib.util.find_spec("colorama") is not None
    return False


def _color_enabled() -> bool:
    """ANSI colour only on an interactive terminal; opt-in on legacy Windows.

    Windows consoles need ``colorama`` to translate ANSI escapes; without it we
    fall back to plain output unless ``FORCE_COLOR`` explicitly opts in.
    """
    if not sys.stdout.isatty():
        return _force_color_env()
    if not _IS_WINDOWS:
        return True
    return _has_colorama() or _force_color_env()


def _force_color_env() -> bool:
    return os.environ.get("FORCE_COLOR", "").strip().lower() in {"1", "true", "yes"}


def configure_logging(settings: Settings | None = None, *, force: bool = False) -> None:
    """Configure structlog + stdlib logging. Idempotent unless ``force``."""
    global _configured
    if _configured and not force:
        return

    cfg = settings or get_settings()
    level = getattr(logging, cfg.log_level, logging.INFO)
    log_format = cfg.resolved_log_format

    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    renderer: Processor
    if log_format == "json":
        renderer = structlog.processors.JSONRenderer(sort_keys=True)
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=_color_enabled())

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            # Redaction runs after format_exc_info so secrets hidden inside
            # exception text are scrubbed too.
            redact_secrets,
            renderer,
        ],
        foreign_pre_chain=shared_processors,
    )

    handler = _CurrentStdoutHandler()
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # Third-party libraries are configured independently: application DEBUG must
    # not drown the structured event stream in library chatter. Raise
    # THIRD_PARTY_LOG_LEVEL when debugging Telethon or SQLAlchemy.
    noisy_level = getattr(logging, cfg.third_party_log_level, logging.WARNING)
    noisy_loggers = (
        "asyncio",
        "sqlalchemy.engine",
        "sqlalchemy.pool",
        "telethon",
        "pyrogram",
        "asyncpg",
    )
    for name in noisy_loggers:
        logging.getLogger(name).setLevel(noisy_level)

    _configured = True


def get_logger(name: str | None = None, **initial_context: Any) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger, configuring logging on first use."""
    if not _configured:
        configure_logging()
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    if initial_context:
        return logger.bind(**initial_context)
    return logger


def bind_worker_context(worker: str, *, run_id: str | None = None, **extra: Any) -> None:
    """Attach ``worker`` (and optional run id) to every log line in this process."""
    context: dict[str, Any] = {"worker": worker}
    if run_id is not None:
        context["run_id"] = run_id
    context.update(extra)
    bind_contextvars(**context)


def unbind_worker_context(*keys: str) -> None:
    """Remove previously bound context keys (all of them when called bare)."""
    if keys:
        unbind_contextvars(*keys)
    else:
        clear_contextvars()
