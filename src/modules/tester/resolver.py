"""Destination resolution and SSRF / DNS rebinding protection at connect time.

Threat model:
* An attacker registers a DNS domain pointing to a public IP, passes validation,
  and rebinds it to 127.0.0.1 or 169.254.169.254 (cloud metadata) for the actual connection.
* To eliminate this risk, we resolve the domain *once*, inspect ALL returned addresses,
  and pin the connection to the exact validated IP literal. No subsequent resolution occurs.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket

from modules.discovery.normalizer import is_disallowed_ip, validate_hostname

__all__ = [
    "DestinationBlockedError",
    "DnsResolutionError",
    "resolve_and_validate_destination",
]


class DnsResolutionError(Exception):
    """Raised when DNS resolution fails for a proxy host."""


class DestinationBlockedError(Exception):
    """Raised when a proxy endpoint resolves to an internal or disallowed IP range."""


async def resolve_and_validate_destination(
    server: str,
    port: int,
) -> tuple[str, list[str]]:
    """Resolve a proxy endpoint and validate all returned IP addresses against SSRF rules.

    Returns:
        (pinned_ip, all_validated_ips)

    Raises:
        :class:`DestinationBlockedError` if any IP is disallowed.
        :class:`DnsResolutionError` if hostname cannot be resolved.
    """
    cleaned_server = server.strip()

    # 1. Direct IP literal check
    try:
        ip = ipaddress.ip_address(cleaned_server)
        if is_disallowed_ip(ip):
            msg = f"Proxy IP {cleaned_server} is in a disallowed network range"
            raise DestinationBlockedError(msg)
        return cleaned_server, [cleaned_server]
    except ValueError:
        pass

    # 2. Hostname syntactic validation
    if not validate_hostname(cleaned_server):
        msg = f"Proxy hostname {cleaned_server!r} is not a valid public hostname"
        raise DestinationBlockedError(msg)

    # 3. DNS resolution of all address records
    loop = asyncio.get_running_loop()
    try:
        addr_info = await loop.getaddrinfo(
            cleaned_server,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        msg = f"DNS resolution failed for {cleaned_server}: {exc}"
        raise DnsResolutionError(msg) from None

    if not addr_info:
        msg = f"No DNS records found for {cleaned_server}"
        raise DnsResolutionError(msg)

    resolved_ips: list[str] = []
    seen: set[str] = set()

    for item in addr_info:
        sockaddr = item[4]
        ip_str = str(sockaddr[0])
        if ip_str in seen:
            continue
        seen.add(ip_str)

        try:
            parsed_ip = ipaddress.ip_address(ip_str)
            if is_disallowed_ip(parsed_ip):
                msg = f"Hostname {cleaned_server} resolved to disallowed IP {ip_str}"
                raise DestinationBlockedError(msg)
            resolved_ips.append(ip_str)
        except ValueError:
            msg = f"Unparseable IP address returned by DNS: {ip_str!r}"
            raise DestinationBlockedError(msg) from None

    if not resolved_ips:
        msg = f"No valid public IP addresses resolved for {cleaned_server}"
        raise DestinationBlockedError(msg)

    # Return pinned IP (the first one) to eliminate DNS rebinding risk
    return resolved_ips[0], resolved_ips
