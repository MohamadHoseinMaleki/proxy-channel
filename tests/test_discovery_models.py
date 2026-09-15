"""Unit tests for :mod:`modules.discovery.models` -- MTProtoProxy and DiscoveredProxyCandidate.

Guarantees asserted:
* Immutable slots-based domain models.
* Secret is wrapped in ProxySecret and never leaked in repr, str, or format.
* Server is automatically canonicalised upon proxy construction.
* Deterministic fingerprint is precomputed upon construction.
* Raw reference in candidate is scrubbed upon construction.
"""

from __future__ import annotations

from core.identity import PROTOCOL_MTPROTO, ProxySecret, compute_fingerprint
from core.models import SourceType
from modules.discovery.models import (
    DiscoveredProxyCandidate,
    MTProtoProxy,
    SecretType,
)


class TestMTProtoProxy:
    def test_construction_and_fingerprint(self) -> None:
        raw_secret = "ee000102030405060708090a0b0c0d0e0f676f6f676c652e636f6d"
        proxy = MTProtoProxy(
            server="  PROXY.Example.Com.  ",
            port=443,
            secret=ProxySecret(raw_secret),
            protocol=PROTOCOL_MTPROTO,
            secret_type=SecretType.FAKE_TLS,
            sni_domain="google.com",
        )

        assert proxy.server == "proxy.example.com"
        assert proxy.port == 443
        assert proxy.secret.reveal() == raw_secret
        assert proxy.secret_type == SecretType.FAKE_TLS
        assert proxy.sni_domain == "google.com"

        expected_fp = compute_fingerprint(
            server="proxy.example.com",
            port=443,
            secret=raw_secret,
            protocol=PROTOCOL_MTPROTO,
        )
        assert proxy.fingerprint == expected_fp

    def test_repr_and_str_never_leak_secret(self) -> None:
        raw_secret = "ee000102030405060708090a0b0c0d0e0f676f6f676c652e636f6d"
        proxy = MTProtoProxy(
            server="proxy.example.com",
            port=443,
            secret=ProxySecret(raw_secret),
            protocol=PROTOCOL_MTPROTO,
            secret_type=SecretType.FAKE_TLS,
            sni_domain="google.com",
        )

        r = repr(proxy)
        s = str(proxy)

        assert raw_secret not in r
        assert raw_secret not in s
        assert proxy.secret.masked in r
        assert proxy.secret.masked in s
        assert "google.com" in r

    def test_immutability(self) -> None:
        proxy = MTProtoProxy(
            server="1.2.3.4",
            port=443,
            secret=ProxySecret("000102030405060708090a0b0c0d0e0f"),
        )
        try:
            proxy.server = "5.6.7.8"  # type: ignore[misc]
            msg = "Expected FrozenInstanceError or AttributeError"
            raise AssertionError(msg)
        except (AttributeError, TypeError):
            pass


class TestDiscoveredProxyCandidate:
    def test_scrubs_raw_reference(self) -> None:
        raw_secret = "000102030405060708090a0b0c0d0e0f"
        proxy = MTProtoProxy(
            server="1.2.3.4",
            port=443,
            secret=ProxySecret(raw_secret),
        )
        raw_url = f"tg://proxy?server=1.2.3.4&port=443&secret={raw_secret}"
        candidate = DiscoveredProxyCandidate(
            proxy=proxy,
            source_type=SourceType.TELEGRAM_CHANNEL,
            source_name="@test_channel",
            source_url="https://t.me/s/test_channel",
            raw_reference=raw_url,
        )

        assert raw_secret not in candidate.raw_reference  # type: ignore[operator]
        assert raw_secret not in repr(candidate)
        assert candidate.source_name == "@test_channel"
