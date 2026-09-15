"""MTProto proxy connectivity testing layer."""

from __future__ import annotations

from modules.tester.models import TesterResult, TransportType
from modules.tester.probe import probe_proxy
from modules.tester.resolver import (
    DestinationBlockedError,
    DnsResolutionError,
    resolve_and_validate_destination,
)
from modules.tester.service import TesterService
from modules.tester.transport import (
    ConnectionTcpMTProxyAbridged,
    ConnectionTcpMTProxyIntermediate,
    ConnectionTcpMTProxyRandomizedIntermediate,
    TransportSelection,
    select_transport,
)

__all__ = [
    "ConnectionTcpMTProxyAbridged",
    "ConnectionTcpMTProxyIntermediate",
    "ConnectionTcpMTProxyRandomizedIntermediate",
    "DestinationBlockedError",
    "DnsResolutionError",
    "TesterResult",
    "TesterService",
    "TransportSelection",
    "TransportType",
    "probe_proxy",
    "resolve_and_validate_destination",
    "select_transport",
]
