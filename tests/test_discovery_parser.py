"""Unit tests for :mod:`modules.discovery.parser` -- proxy URL parsing from text and HTML."""

from __future__ import annotations

import pytest

from modules.discovery.models import SecretType
from modules.discovery.parser import (
    ProxyParseError,
    extract_proxy_urls,
    parse_proxy_text,
    parse_proxy_url,
)


class TestParseProxyUrl:
    def test_parses_tg_proxy_url(self) -> None:
        url = "tg://proxy?server=proxy.example.com&port=443&secret=000102030405060708090a0b0c0d0e0f"
        proxy = parse_proxy_url(url)
        assert proxy.server == "proxy.example.com"
        assert proxy.port == 443
        assert proxy.secret.reveal() == "000102030405060708090a0b0c0d0e0f"
        assert proxy.secret_type == SecretType.LEGACY
        assert proxy.sni_domain is None

    def test_parses_https_t_me_proxy_url(self) -> None:
        url = (
            "https://t.me/proxy?server=1.2.3.4&port=8443"
            "&secret=ee000102030405060708090a0b0c0d0e0f676f6f676c652e636f6d"
        )
        proxy = parse_proxy_url(url)
        assert proxy.server == "1.2.3.4"
        assert proxy.port == 8443
        assert proxy.secret_type == SecretType.FAKE_TLS
        assert proxy.sni_domain == "google.com"

    def test_parses_http_t_me_proxy_url(self) -> None:
        url = "http://t.me/proxy?server=8.8.8.8&port=443&secret=dd000102030405060708090a0b0c0d0e0f"
        proxy = parse_proxy_url(url)
        assert proxy.server == "8.8.8.8"
        assert proxy.port == 443
        assert proxy.secret_type == SecretType.SECURE_RANDOMIZED

    def test_query_param_reordering_produces_identical_proxy(self) -> None:
        url1 = (
            "tg://proxy?server=proxy.example.com&port=443&secret=000102030405060708090a0b0c0d0e0f"
        )
        url2 = (
            "tg://proxy?secret=000102030405060708090a0b0c0d0e0f&port=443&server=proxy.example.com"
        )
        url3 = "https://t.me/proxy?port=443&secret=000102030405060708090a0b0c0d0e0f&server=PROXY.EXAMPLE.COM."

        p1 = parse_proxy_url(url1)
        p2 = parse_proxy_url(url2)
        p3 = parse_proxy_url(url3)

        assert p1 == p2 == p3
        assert p1.fingerprint == p2.fingerprint == p3.fingerprint

    def test_percent_encoded_parameters(self) -> None:
        url = "tg://proxy?server=proxy%2eexample%2ecom&port=443&secret=000102030405060708090a0b0c0d0e0f"
        proxy = parse_proxy_url(url)
        assert proxy.server == "proxy.example.com"

    def test_identical_duplicate_parameters_are_accepted(self) -> None:
        url = "tg://proxy?server=proxy.example.com&port=443&port=443&secret=000102030405060708090a0b0c0d0e0f"
        proxy = parse_proxy_url(url)
        assert proxy.port == 443

    def test_conflicting_duplicate_parameters_are_rejected(self) -> None:
        url = "tg://proxy?server=proxy.example.com&port=443&port=8443&secret=000102030405060708090a0b0c0d0e0f"
        with pytest.raises(ProxyParseError, match="Conflicting duplicate"):
            parse_proxy_url(url)

    @pytest.mark.parametrize(
        "invalid_url",
        [
            "",
            "   ",
            "tg://proxy",  # missing query
            "tg://proxy?server=1.2.3.4&port=443",  # missing secret
            "tg://proxy?server=1.2.3.4&secret=000102030405060708090a0b0c0d0e0f",  # missing port
            "tg://proxy?port=443&secret=000102030405060708090a0b0c0d0e0f",  # missing server
            "tg://proxy?server=&port=443&secret=000102030405060708090a0b0c0d0e0f",  # empty server
            (
                "tg://other?server=1.2.3.4&port=443&secret=000102030405060708090a0b0c0d0e0f"
            ),  # invalid tg path
            (
                "https://evil.com/proxy?server=1.2.3.4&port=443"
                "&secret=000102030405060708090a0b0c0d0e0f"
            ),  # invalid host
            (
                "https://t.me/other?server=1.2.3.4&port=443&secret=000102030405060708090a0b0c0d0e0f"
            ),  # invalid path
            (
                "ftp://t.me/proxy?server=1.2.3.4&port=443&secret=000102030405060708090a0b0c0d0e0f"
            ),  # invalid scheme
            "tg://proxy?server=127.0.0.1&port=443&secret=000102030405060708090a0b0c0d0e0f",
            "tg://proxy?server=1.2.3.4&port=0&secret=000102030405060708090a0b0c0d0e0f",
            "tg://proxy?server=1.2.3.4&port=443&secret=invalid-secret",
        ],
    )
    def test_rejects_invalid_urls(self, invalid_url: str) -> None:
        with pytest.raises(ProxyParseError):
            parse_proxy_url(invalid_url)

    def test_exception_never_contains_raw_secret(self) -> None:
        secret = "ee000102030405060708090a0b0c0d0e0finvalid-sni!@#"
        url = f"tg://proxy?server=1.2.3.4&port=443&secret={secret}"
        with pytest.raises(ProxyParseError) as exc_info:
            parse_proxy_url(url)
        assert secret not in str(exc_info.value)


class TestTextAndHtmlExtraction:
    def test_extract_proxy_urls_from_mixed_content(self) -> None:
        content = """
        Here is a proxy:
        tg://proxy?server=1.2.3.4&port=443&secret=000102030405060708090a0b0c0d0e0f.
        Another one in HTML:
        <a href="https://t.me/proxy?server=proxy.example.com&amp;port=8443&amp;secret=000102030405060708090a0b0c0d0e0f">Click</a>
        And bracketed: [tg://proxy?server=8.8.8.8&port=443&secret=000102030405060708090a0b0c0d0e0f]
        """
        urls = extract_proxy_urls(content)
        assert len(urls) == 3
        assert urls[0].startswith("tg://proxy")
        assert not urls[0].endswith(".")
        assert "proxy.example.com" in urls[1]
        assert not urls[2].endswith("]")

    def test_parse_proxy_text_deduplicates_by_fingerprint(self) -> None:
        content = """
        tg://proxy?server=1.2.3.4&port=443&secret=000102030405060708090a0b0c0d0e0f
        https://t.me/proxy?port=443&server=1.2.3.4&secret=000102030405060708090a0b0c0d0e0f
        tg://proxy?server=5.6.7.8&port=443&secret=000102030405060708090a0b0c0d0e0f
        """
        proxies = parse_proxy_text(content)
        assert len(proxies) == 2
        servers = {p.server for p in proxies}
        assert servers == {"1.2.3.4", "5.6.7.8"}

    def test_parse_proxy_text_skips_malformed_gracefully(self) -> None:
        content = """
        tg://proxy?server=127.0.0.1&port=443&secret=000102030405060708090a0b0c0d0e0f
        tg://proxy?server=1.2.3.4&port=443&secret=000102030405060708090a0b0c0d0e0f
        random non-url text
        """
        proxies = parse_proxy_text(content)
        assert len(proxies) == 1
        assert proxies[0].server == "1.2.3.4"
