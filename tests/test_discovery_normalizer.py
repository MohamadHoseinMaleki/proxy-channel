"""Unit tests for :mod:`modules.discovery.normalizer` -- server, port, and secret validation."""

from __future__ import annotations

import pytest

from modules.discovery.models import SecretType
from modules.discovery.normalizer import (
    PortValidationError,
    SecretValidationError,
    ServerValidationError,
    validate_and_normalize_port,
    validate_and_normalize_server,
    validate_and_parse_secret,
    validate_hostname,
)


class TestServerValidation:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("1.2.3.4", "1.2.3.4"),
            ("8.8.8.8", "8.8.8.8"),
            ("  93.184.216.34  ", "93.184.216.34"),
            ("2606:4700:4700::1111", "2606:4700:4700::1111"),
            ("[2606:4700:4700::1111]", "2606:4700:4700::1111"),
            ("proxy.example.com", "proxy.example.com"),
            ("PROXY.EXAMPLE.COM.", "proxy.example.com"),
            ("  sub.proxy-host123.co.uk  ", "sub.proxy-host123.co.uk"),
        ],
    )
    def test_accepts_valid_public_endpoints(self, raw: str, expected: str) -> None:
        assert validate_and_normalize_server(raw) == expected

    @pytest.mark.parametrize(
        "disallowed_ip",
        [
            "127.0.0.1",
            "127.0.0.53",
            "::1",
            "[::1]",
            "10.0.0.1",
            "10.255.255.255",
            "172.16.0.1",
            "172.31.255.255",
            "192.168.1.1",
            "192.168.0.254",
            "169.254.1.1",
            "169.254.169.254",  # AWS/GCP metadata
            "224.0.0.1",  # Multicast
            "ff02::1",  # IPv6 multicast
            "0.0.0.0",  # Unspecified
            "::",  # IPv6 unspecified
            "fc00::1",  # IPv6 ULA
            "fe80::1",  # IPv6 link-local
            "100.64.0.1",  # CGNAT
            "192.0.2.1",  # TEST-NET-1
            "198.51.100.1",  # TEST-NET-2
            "203.0.113.1",  # TEST-NET-3
            "240.0.0.1",  # Reserved
            "::ffff:127.0.0.1",  # IPv4-mapped loopback
            "::ffff:169.254.169.254",  # IPv4-mapped metadata
        ],
    )
    def test_rejects_disallowed_ips(self, disallowed_ip: str) -> None:
        with pytest.raises(ServerValidationError):
            validate_and_normalize_server(disallowed_ip)

    @pytest.mark.parametrize(
        "invalid_host",
        [
            "",
            "   ",
            "localhost",
            "myhost.localhost",
            "server.local",
            "service.internal",
            "service.arpa",
            "metadata.google.internal",
            "-leading-hyphen.com",
            "trailing-hyphen-.com",
            "label..double-dot.com",
            "a" * 64 + ".com",  # label > 63 chars
            "example.123",  # all numeric TLD
            "singleword",  # single label
            "http://example.com",
            "example.com:443",
        ],
    )
    def test_rejects_invalid_hostnames(self, invalid_host: str) -> None:
        with pytest.raises(ServerValidationError):
            validate_and_normalize_server(invalid_host)

    def test_validate_hostname_helper(self) -> None:
        assert validate_hostname("example.com") is True
        assert validate_hostname("a.b.c.org") is True
        assert validate_hostname("localhost") is False
        assert validate_hostname("x" * 254) is False


class TestPortValidation:
    @pytest.mark.parametrize("port", [1, 80, 443, 8080, 65535, "1", "443", "65535"])
    def test_accepts_valid_ports(self, port: int | str) -> None:
        assert validate_and_normalize_port(port) == int(port)

    @pytest.mark.parametrize(
        "invalid_port",
        [
            0,
            65536,
            -1,
            -443,
            70000,
            "0",
            "65536",
            "-1",
            "443.0",
            "abc",
            "",
            "   ",
            True,
            False,
            None,
            [],
        ],
    )
    def test_rejects_invalid_ports(self, invalid_port: object) -> None:
        with pytest.raises(PortValidationError):
            validate_and_normalize_port(invalid_port)  # type: ignore[arg-type]


class TestSecretValidation:
    def test_valid_legacy_16_byte_hex(self) -> None:
        raw = "000102030405060708090a0b0c0d0e0f"
        secret, stype, sni = validate_and_parse_secret(raw)
        assert secret.reveal() == raw
        assert stype == SecretType.LEGACY
        assert sni is None

    def test_valid_legacy_base64(self) -> None:
        import base64

        raw_bytes = bytes(range(16))
        b64 = base64.b64encode(raw_bytes).decode("ascii")
        secret, stype, sni = validate_and_parse_secret(b64)
        assert secret.reveal() == b64
        assert stype == SecretType.LEGACY
        assert sni is None

    def test_valid_dd_secret(self) -> None:
        raw = "dd000102030405060708090a0b0c0d0e0f"
        secret, stype, sni = validate_and_parse_secret(raw)
        assert secret.reveal() == raw
        assert stype == SecretType.SECURE_RANDOMIZED
        assert sni is None

    def test_valid_fake_tls_without_explicit_domain(self) -> None:
        raw = "ee000102030405060708090a0b0c0d0e0f"
        secret, stype, sni = validate_and_parse_secret(raw)
        assert secret.reveal() == raw
        assert stype == SecretType.FAKE_TLS
        assert sni is None

    def test_valid_fake_tls_with_sni_domain(self) -> None:
        # ee + 16-byte key + "google.com" in hex ("676f6f676c652e636f6d")
        raw = "ee000102030405060708090a0b0c0d0e0f676f6f676c652e636f6d"
        secret, stype, sni = validate_and_parse_secret(raw)
        assert secret.reveal() == raw
        assert stype == SecretType.FAKE_TLS
        assert sni == "google.com"

    def test_valid_fake_tls_with_subdomain_sni(self) -> None:
        # ee + 16-byte key + "www.cloudflare.com" in hex
        domain_hex = "www.cloudflare.com".encode("ascii").hex()
        raw = "ee000102030405060708090a0b0c0d0e0f" + domain_hex
        secret, stype, sni = validate_and_parse_secret(raw)
        assert secret.reveal() == raw
        assert stype == SecretType.FAKE_TLS
        assert sni == "www.cloudflare.com"

    @pytest.mark.parametrize(
        "invalid_secret",
        [
            "",
            "   ",
            "ee000",  # odd-length hex
            "000102030405060708090a0b0c0d0e",  # 15 bytes (too short)
            "000102030405060708090a0b0c0d0e0f00",  # 17 bytes without dd/ee prefix
            "aa000102030405060708090a0b0c0d0e0f",  # 17 bytes with 0xaa prefix
            "ee1234",  # ee prefix but < 17 bytes
            "ee000102030405060708090a0b0c0d0e0fffffff",  # ee + key + non-ascii domain
            "ee000102030405060708090a0b0c0d0e0f6c6f63616c686f7374",  # ee + "localhost"
            "not-hex-or-base64!@#$%",
        ],
    )
    def test_rejects_invalid_secrets(self, invalid_secret: str) -> None:
        with pytest.raises(SecretValidationError):
            validate_and_parse_secret(invalid_secret)
