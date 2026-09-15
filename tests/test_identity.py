"""Unit tests for :mod:`core.identity` -- fingerprinting and secret hygiene.

No database, no network. These are the invariants the whole platform rests on:
if two sightings of one configuration do not produce one fingerprint, the
``uq_proxies_fingerprint`` constraint silently stops working and the table fills
with duplicates.
"""

from __future__ import annotations

import base64
import json

import pytest

from core.identity import (
    FINGERPRINT_LENGTH,
    PROTOCOL_MTPROTO,
    SECRET_MAX_LENGTH,
    ProxySecret,
    compute_fingerprint,
    mask_secret,
    normalize_server,
    secret_identity_bytes,
)

#: A realistic 16-byte MTProto secret in its dominant hex spelling.
HEX_SECRET = "ee" + "a1" * 15
#: The same secret as an ``ee``-prefixed fake-TLS key: 16 bytes of key plus an
#: SNI domain, hex-encoded.
FAKE_TLS_GOOGLE = "ee" + "b2" * 15 + "676f6f676c652e636f6d"
FAKE_TLS_TELEGRAM = "ee" + "b2" * 15 + "74656c656772616d2e6f7267"


def fp(
    server: str = "proxy.example.com",
    port: int = 443,
    secret: str = HEX_SECRET,
    protocol: str = PROTOCOL_MTPROTO,
) -> str:
    return compute_fingerprint(server=server, port=port, secret=secret, protocol=protocol)


class TestNormalizeServer:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Proxy.Example.COM", "proxy.example.com"),
            ("  proxy.example.com  ", "proxy.example.com"),
            ("\tproxy.example.com\n", "proxy.example.com"),
            ("proxy.example.com.", "proxy.example.com"),
            ("PROXY.EXAMPLE.COM.", "proxy.example.com"),
            ("[2001:DB8::1]", "2001:db8::1"),
            ("2001:db8::1", "2001:db8::1"),
            ("1.2.3.4", "1.2.3.4"),
        ],
    )
    def test_canonicalises(self, raw: str, expected: str) -> None:
        assert normalize_server(raw) == expected

    def test_bracketed_and_bare_ipv6_are_one_identity(self) -> None:
        assert normalize_server("[2001:DB8::1]") == normalize_server("2001:db8::1")

    def test_is_total_even_for_nonsense(self) -> None:
        # Normalisation must never raise: identity has to be defined for every
        # input, including ones Task 003's parser will later reject.
        assert normalize_server("not a host at all!!") == "not a host at all!!"
        assert normalize_server("") == ""
        assert normalize_server("[") == "["


class TestSecretIdentityBytes:
    def test_decodes_hex(self) -> None:
        assert secret_identity_bytes("01020304") == b"\x01\x02\x03\x04"

    def test_decodes_base64_when_not_hex(self) -> None:
        raw = b"\x01\x02\x03\x04\xff"
        assert secret_identity_bytes(base64.b64encode(raw).decode()) == raw

    def test_hex_takes_precedence_over_base64(self) -> None:
        # A 32-char lowercase hex string is *also* valid base64. Hex must win,
        # because reversing the order would silently re-fingerprint every row.
        value = "a1" * 16
        assert secret_identity_bytes(value) == bytes.fromhex(value)
        assert secret_identity_bytes(value) != base64.b64decode(value + "==")

    def test_hex_and_base64_spellings_agree(self) -> None:
        raw = bytes(range(16))
        assert secret_identity_bytes(raw.hex()) == secret_identity_bytes(
            base64.b64encode(raw).decode()
        )

    def test_pads_unpadded_base64(self) -> None:
        # Sources routinely strip base64 padding.
        raw = b"\x01\x02\x03\x04\x05"
        unpadded = base64.b64encode(raw).decode().rstrip("=")
        assert "=" not in unpadded
        assert secret_identity_bytes(unpadded) == raw

    def test_does_not_truncate_to_sixteen_bytes(self) -> None:
        # Deliberate divergence from Telethon's normalize_secret, which truncates
        # to 16 bytes and so drops the fake-TLS SNI domain. For identity the
        # domain matters: 0xee + 15 key bytes + len("google.com") == 26 bytes.
        decoded = secret_identity_bytes(FAKE_TLS_GOOGLE)
        assert len(decoded) == 26
        assert len(decoded) > 16
        assert decoded[-10:] == b"google.com"

    def test_sni_domain_is_part_of_identity(self) -> None:
        assert secret_identity_bytes(FAKE_TLS_GOOGLE) != secret_identity_bytes(FAKE_TLS_TELEGRAM)
        assert fp(secret=FAKE_TLS_GOOGLE) != fp(secret=FAKE_TLS_TELEGRAM)

    def test_strips_surrounding_whitespace(self) -> None:
        assert secret_identity_bytes(f"  {HEX_SECRET}\n") == secret_identity_bytes(HEX_SECRET)

    def test_falls_back_to_raw_bytes_for_unknown_encoding(self) -> None:
        # Determinism beats strictness here: Task 003 decides acceptability.
        assert secret_identity_bytes("!!!not-hex-or-base64!!!") == b"!!!not-hex-or-base64!!!"

    def test_rejects_empty(self) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            secret_identity_bytes("   ")


class TestComputeFingerprint:
    def test_shape(self) -> None:
        value = fp()
        assert len(value) == FINGERPRINT_LENGTH == 64
        assert value == value.lower()
        int(value, 16)  # must be valid hex

    def test_is_deterministic(self) -> None:
        assert fp() == fp()

    def test_matches_a_golden_vector(self) -> None:
        # A pinned digest. If the scheme changes by accident this fails, forcing a
        # conscious FINGERPRINT_VERSION bump and a documented migration plan
        # instead of silently splitting every existing identity in the table.
        assert fp(server="proxy.example.com", port=443, secret=HEX_SECRET) == (
            "c20233083a46ff080cf18963c1978c7f014b62c6527fd4198fa93cf94f43a32e"
        )

    @pytest.mark.parametrize(
        ("server", "port", "secret"),
        [
            ("PROXY.EXAMPLE.COM", 443, HEX_SECRET),
            ("  proxy.example.com ", 443, HEX_SECRET),
            ("proxy.example.com.", 443, HEX_SECRET),
            ("proxy.example.com", 443, f"  {HEX_SECRET}  "),
        ],
    )
    def test_cosmetic_differences_are_the_same_identity(
        self, server: str, port: int, secret: str
    ) -> None:
        assert fp(server=server, port=port, secret=secret) == fp()

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"port": 444},
            {"server": "other.example.com"},
            {"secret": "ee" + "a2" * 15},
            {"protocol": "socks5"},
        ],
    )
    def test_meaningful_differences_are_different_identities(
        self, kwargs: dict[str, object]
    ) -> None:
        assert fp(**kwargs) != fp()  # type: ignore[arg-type]

    def test_secret_is_part_of_identity(self) -> None:
        # server+port alone would merge genuinely different proxies: many hosts
        # advertise several distinct secrets on one endpoint.
        assert fp(secret="ee" + "11" * 15) != fp(secret="ee" + "22" * 15)

    def test_hex_and_base64_spellings_share_one_fingerprint(self) -> None:
        raw = bytes(range(16))
        assert fp(secret=raw.hex()) == fp(secret=base64.b64encode(raw).decode())

    def test_ipv6_notations_share_one_fingerprint(self) -> None:
        assert fp(server="[2001:DB8::1]") == fp(server="2001:db8::1")

    def test_field_separator_prevents_concatenation_collisions(self) -> None:
        # These are two genuinely different proxies: different ports, different
        # secrets. Joining the fields with ":" makes them identical, so an
        # identity built that way would collapse them into one row under
        # uq_proxies_fingerprint and silently lose a candidate.
        naive_left = ":".join(["x:1", str(2), "y"])
        naive_right = ":".join(["x", str(1), "2:y"])
        assert naive_left == naive_right == "x:1:2:y"

        # The \x1f separator cannot appear in a host, a port or a secret, so the
        # split is unambiguous and the identities stay distinct.
        assert compute_fingerprint(server="x:1", port=2, secret="y") != compute_fingerprint(
            server="x", port=1, secret="2:y"
        )

    def test_protocol_defaults_to_mtproto(self) -> None:
        assert fp() == compute_fingerprint(
            server="proxy.example.com", port=443, secret=HEX_SECRET, protocol=PROTOCOL_MTPROTO
        )

    def test_protocol_is_case_and_space_insensitive(self) -> None:
        assert fp(protocol="  MTPROTO  ") == fp()

    @pytest.mark.parametrize("port", [0, -1, 65536, 100000])
    def test_rejects_out_of_range_port(self, port: int) -> None:
        with pytest.raises(ValueError, match=r"1\.\.65535"):
            fp(port=port)

    @pytest.mark.parametrize("port", [True, False])
    def test_rejects_bool_port(self, port: bool) -> None:
        # bool is a subclass of int; `port=True` is almost certainly a bug.
        with pytest.raises(TypeError, match="must be an int"):
            fp(port=port)

    def test_rejects_string_port(self) -> None:
        with pytest.raises(TypeError, match="must be an int"):
            fp(port="443")  # type: ignore[arg-type]

    @pytest.mark.parametrize("server", ["", "   ", ".", "[.]"])
    def test_rejects_empty_server(self, server: str) -> None:
        with pytest.raises(ValueError, match="server must not be empty"):
            fp(server=server)

    def test_rejects_empty_secret(self) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            fp(secret="")

    def test_rejects_empty_protocol(self) -> None:
        with pytest.raises(ValueError, match="protocol must not be empty"):
            fp(protocol="  ")


class TestMaskSecret:
    def test_masks_the_middle(self) -> None:
        masked = mask_secret(HEX_SECRET)
        assert masked == f"{HEX_SECRET[:4]}...{HEX_SECRET[-4:]}"
        assert HEX_SECRET not in masked

    def test_fully_masks_short_values(self) -> None:
        # Revealing head+tail of an 8-char value would disclose most of it.
        assert mask_secret("abcdef12") == "********"
        assert "abcdef12" not in mask_secret("abcdef12")

    @pytest.mark.parametrize("value", ["", "   "])
    def test_empty_masks_to_empty(self, value: str) -> None:
        assert mask_secret(value) == ""

    def test_never_leaks_the_whole_secret(self) -> None:
        for length in range(1, 64):
            value = "a" * length
            assert mask_secret(value) != value


class TestProxySecret:
    def test_is_not_a_str_subclass(self) -> None:
        # A str subclass would leak through str.__format__ and through any
        # isinstance(x, str) serialisation path.
        assert not isinstance(ProxySecret(HEX_SECRET), str)

    def test_strips_whitespace_but_preserves_case(self) -> None:
        secret = ProxySecret(f"  {HEX_SECRET.upper()}  \n")
        assert secret.reveal() == HEX_SECRET.upper()

    def test_reveal_returns_plaintext(self) -> None:
        assert ProxySecret(HEX_SECRET).reveal() == HEX_SECRET

    @pytest.mark.parametrize(
        "render",
        [
            str,
            repr,
            lambda s: f"{s}",
            lambda s: f"{s}",
            lambda s: f"{s!s}",
            lambda s: f"{s!r}",
            lambda s: f"value={s} end",
            lambda s: f"{s:>40}",
            lambda s: f"{s!r}",
            lambda s: "%s" % s,  # noqa: UP031 - percent rendering is the point
            lambda s: f"{[s]}",
            lambda s: json.dumps({"secret": str(s)}),
        ],
        ids=[
            "str",
            "repr",
            "fstring",
            "format",
            "format-s",
            "format-r",
            "embedded",
            "format-spec",
            "fstring-repr",
            "percent",
            "in-list",
            "json",
        ],
    )
    def test_no_rendering_leaks_the_plaintext(self, render: object) -> None:
        secret = ProxySecret(HEX_SECRET)
        rendered = render(secret)  # type: ignore[operator]
        assert HEX_SECRET not in rendered

    def test_format_spec_is_ignored(self) -> None:
        secret = ProxySecret(HEX_SECRET)
        assert f"{secret}" == f"{secret!s}" == f"{secret:anything}" == secret.masked

    def test_json_serialisation_raises_rather_than_leaks(self) -> None:
        # Deliberate: an accidental json.dumps(proxy) must fail loudly instead of
        # emitting the secret into an API response or a log line.
        with pytest.raises(TypeError):
            json.dumps({"secret": ProxySecret(HEX_SECRET)})

    def test_structlog_would_render_the_masked_form(self) -> None:
        # structlog's JSONRenderer falls back to repr() via default=str/repr.
        secret = ProxySecret(HEX_SECRET)
        assert HEX_SECRET not in json.dumps({"secret": secret}, default=repr)

    def test_equality_with_proxy_secret(self) -> None:
        assert ProxySecret(HEX_SECRET) == ProxySecret(HEX_SECRET)
        assert ProxySecret(HEX_SECRET) != ProxySecret("ee" + "c3" * 15)

    def test_equality_with_str(self) -> None:
        assert ProxySecret(HEX_SECRET) == HEX_SECRET
        assert ProxySecret(HEX_SECRET) != "something else"

    def test_equality_with_other_types_is_not_implemented(self) -> None:
        assert ProxySecret(HEX_SECRET).__eq__(42) is NotImplemented
        assert ProxySecret(HEX_SECRET) != 42

    def test_is_hashable_and_usable_as_a_dict_key(self) -> None:
        mapping = {ProxySecret(HEX_SECRET): "ok"}
        assert mapping[ProxySecret(HEX_SECRET)] == "ok"

    def test_len_and_bool(self) -> None:
        secret = ProxySecret(HEX_SECRET)
        assert len(secret) == len(HEX_SECRET)
        assert bool(secret) is True

    def test_identity_bytes_matches_module_function(self) -> None:
        assert ProxySecret(HEX_SECRET).identity_bytes == secret_identity_bytes(HEX_SECRET)

    def test_uses_slots_so_no_attribute_can_be_attached(self) -> None:
        secret = ProxySecret(HEX_SECRET)
        with pytest.raises(AttributeError):
            secret.plaintext = HEX_SECRET  # type: ignore[attr-defined]

    @pytest.mark.parametrize("value", ["", "   ", "\n\t"])
    def test_rejects_empty(self, value: str) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            ProxySecret(value)

    def test_rejects_non_str(self) -> None:
        with pytest.raises(TypeError, match="expects str"):
            ProxySecret(b"\xee" + b"\x00" * 16)  # type: ignore[arg-type]

    def test_rejects_overlong(self) -> None:
        with pytest.raises(ValueError, match=f"exceeds {SECRET_MAX_LENGTH}"):
            ProxySecret("a" * (SECRET_MAX_LENGTH + 1))

    def test_accepts_maximum_length(self) -> None:
        assert len(ProxySecret("a" * SECRET_MAX_LENGTH)) == SECRET_MAX_LENGTH
