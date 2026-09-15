"""Unit tests for MTProxy transport selection and protocol capabilities."""

from __future__ import annotations

import pytest
from telethon.network.connection.tcpmtproxy import (
    ConnectionTcpMTProxyRandomizedIntermediate,
)

from modules.discovery.models import SecretType
from modules.tester.models import TransportType
from modules.tester.transport import select_transport


class TestSelectTransport:
    def test_secure_randomized_selects_randomized_intermediate(self) -> None:
        sel = select_transport(SecretType.SECURE_RANDOMIZED)
        assert sel.is_supported is True
        assert sel.transport_type == TransportType.RANDOMIZED_INTERMEDIATE
        assert sel.transport_cls is ConnectionTcpMTProxyRandomizedIntermediate

    def test_legacy_selects_randomized_intermediate(self) -> None:
        sel = select_transport(SecretType.LEGACY)
        assert sel.is_supported is True
        assert sel.transport_type == TransportType.RANDOMIZED_INTERMEDIATE
        assert sel.transport_cls is ConnectionTcpMTProxyRandomizedIntermediate

    def test_fake_tls_is_explicitly_unsupported(self) -> None:
        sel = select_transport(SecretType.FAKE_TLS)
        assert sel.is_supported is False
        assert sel.transport_type == TransportType.FAKE_TLS
        assert sel.transport_cls is None
        assert sel.reason is not None
        assert "Fake-TLS" in sel.reason
        assert "ClientHello" in sel.reason

    def test_accepts_string_enum_values(self) -> None:
        sel_rand = select_transport("secure_randomized")
        assert sel_rand.is_supported is True
        assert sel_rand.transport_type == TransportType.RANDOMIZED_INTERMEDIATE

        sel_tls = select_transport("fake_tls")
        assert sel_tls.is_supported is False
        assert sel_tls.transport_type == TransportType.FAKE_TLS

    def test_rejects_invalid_secret_type(self) -> None:
        with pytest.raises(ValueError):
            select_transport("invalid_type")

    def test_repr_contains_type_and_status(self) -> None:
        sel = select_transport(SecretType.SECURE_RANDOMIZED)
        rep = repr(sel)
        assert "randomized_intermediate" in rep
        assert "supported=True" in rep
