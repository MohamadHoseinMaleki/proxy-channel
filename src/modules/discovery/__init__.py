"""MTProto proxy discovery, parsing, validation, and provenance tracking."""

from __future__ import annotations

from modules.discovery.catalog import (
    DiscoverySourceSpecError,
    parse_discovery_sources,
    validate_http_source_url,
    validate_telegram_channel,
)
from modules.discovery.http import (
    HttpFetchError,
    ResponseSizeExceededError,
    SSRFProtectionError,
    SsrfSafeHttpClient,
)
from modules.discovery.models import (
    DiscoveredProxyCandidate,
    MTProtoProxy,
    SecretType,
)
from modules.discovery.normalizer import (
    DiscoveryValidationError,
    PortValidationError,
    SecretValidationError,
    ServerValidationError,
    validate_and_normalize_port,
    validate_and_normalize_server,
    validate_and_parse_secret,
    validate_hostname,
)
from modules.discovery.parser import (
    ProxyParseError,
    extract_proxy_urls,
    parse_proxy_text,
    parse_proxy_url,
)
from modules.discovery.service import (
    DiscoveryBatchResult,
    DiscoveryService,
    persist_candidate,
    persist_candidates,
)
from modules.discovery.sources import (
    BaseSource,
    RawHttpSource,
    RawTextSource,
    TelegramWebSource,
)

__all__ = [
    "BaseSource",
    "DiscoveredProxyCandidate",
    "DiscoveryBatchResult",
    "DiscoveryService",
    "DiscoverySourceSpecError",
    "DiscoveryValidationError",
    "HttpFetchError",
    "MTProtoProxy",
    "PortValidationError",
    "ProxyParseError",
    "RawHttpSource",
    "RawTextSource",
    "ResponseSizeExceededError",
    "SSRFProtectionError",
    "SecretType",
    "SecretValidationError",
    "ServerValidationError",
    "SsrfSafeHttpClient",
    "TelegramWebSource",
    "extract_proxy_urls",
    "parse_discovery_sources",
    "parse_proxy_text",
    "parse_proxy_url",
    "persist_candidate",
    "persist_candidates",
    "validate_and_normalize_port",
    "validate_and_normalize_server",
    "validate_and_parse_secret",
    "validate_hostname",
    "validate_http_source_url",
    "validate_telegram_channel",
]
