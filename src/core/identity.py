"""Proxy identity: normalisation, deterministic fingerprinting, secret hygiene.

This module answers one question: **when are two MTProto proxy configurations the
same proxy?** Everything downstream depends on the answer, because ``fingerprint``
is the UNIQUE key that stops 10,000 copies of one configuration becoming 10,000
rows.

It is deliberately free of SQLAlchemy and of I/O so that Task 003's parser, the
ORM models and the tests can all share one definition of identity.

Secret handling
---------------
An MTProto secret is credential-like. :class:`ProxySecret` wraps it so that the
plaintext is only reachable through an explicit :meth:`ProxySecret.reveal` call;
``str()``, ``repr()`` and f-string interpolation all yield a masked form. It is
**not** a ``str`` subclass on purpose -- a subclass would leak through
``str.__format__`` and through any ``isinstance(x, str)`` serialisation path.

At-rest encryption is deliberately *not* implemented: there is no key-management
strategy in the MVP, and inventing one silently would produce a false sense of
security. The tester needs the real secret to connect, so the column stores it.
Protection comes from (a) this wrapper, (b) the log/persist scrubber in
:mod:`core.logger`, and (c) never selecting the column into a report or log line.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from typing import Final

__all__ = [
    "FINGERPRINT_ALGORITHM",
    "FINGERPRINT_LENGTH",
    "FINGERPRINT_VERSION",
    "PROTOCOL_MTPROTO",
    "SECRET_MAX_LENGTH",
    "SERVER_MAX_LENGTH",
    "ProxySecret",
    "compute_fingerprint",
    "mask_secret",
    "normalize_server",
    "secret_identity_bytes",
]

#: The only protocol the MVP supports. Kept as a value rather than a closed
#: database CHECK constraint so a future protocol is a code change, not a
#: migration -- see docs/DECISION_LOG.md D-018.
PROTOCOL_MTPROTO: Final = "mtproto"

#: Bumped if the fingerprint *scheme* ever changes. Mixed schemes in one table
#: would silently split or merge identities, so the version is part of the hash.
FINGERPRINT_VERSION: Final = "v1"

FINGERPRINT_ALGORITHM: Final = "sha256"
FINGERPRINT_LENGTH: Final = 64  # hex digest length of sha256

#: Field separator inside the hashed payload. US (0x1f) cannot appear in a
#: hostname, a port or a hex/base64 secret, so the split is unambiguous -- unlike
#: a naive ``f"{server}:{port}"`` concatenation, which would let ``server="a:1"``
#: collide with ``server="a", port=1``.
_FIELD_SEPARATOR: Final = "\x1f"

#: Column limits, mirrored by CHECK constraints in the migration.
SERVER_MAX_LENGTH: Final = 255
SECRET_MAX_LENGTH: Final = 512

#: How many characters of a secret may appear at each end of a masked form.
_MASK_HEAD: Final = 4
_MASK_TAIL: Final = 4
#: Below this length a secret is fully masked; showing head/tail of a short
#: value would disclose too large a fraction of it.
_MASK_MIN_LENGTH: Final = 16


def normalize_server(server: str) -> str:
    """Canonicalise a proxy host for identity comparison.

    * surrounding whitespace removed
    * lowercased -- DNS names and the hex digits of an IPv6 literal are
      case-insensitive (RFC 4034 §6.1, RFC 5952 §4.3)
    * IPv6 bracket notation stripped, so ``[2001:db8::1]`` and ``2001:db8::1``
      are one identity
    * a trailing FQDN root dot removed

    This does **not** validate the host and does **not** resolve DNS; validation
    is Task 003's job. Normalisation here must stay total so that identity is
    defined for every input, including ones a parser will later reject.
    """
    value = server.strip().lower()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    return value.rstrip(".")


def secret_identity_bytes(secret: str) -> bytes:
    """Decode a secret to the canonical bytes that define proxy identity.

    Two spellings of the same secret must produce one fingerprint, and two
    genuinely different secrets must not. Decoding to bytes achieves both, and it
    mirrors what Telethon >= 1.35.0 does in ``TcpMTProxy.normalize_secret``
    (verified in ``spike/AUDIT.md``): hex first, then a base64 fallback.

    Order matters. A 32-character lowercase-hex string is *also* valid base64, so
    hex is tried first -- it is the dominant MTProto encoding, and choosing the
    other order would silently change every existing fingerprint.

    Unlike Telethon this does **not** truncate to 16 bytes. Telethon drops the
    fake-TLS SNI domain because it cannot use it; for *identity* the domain is
    part of the configuration, so ``ee<key>google.com`` and ``ee<key>telegram.org``
    are correctly treated as different proxies.
    """
    value = secret.strip()
    if not value:
        msg = "MTProto secret must not be empty"
        raise ValueError(msg)

    try:
        return bytes.fromhex(value)
    except ValueError:
        pass

    try:
        padded = value + "=" * (-len(value) % 4)
        return base64.b64decode(padded.encode("ascii"), validate=True)
    except (binascii.Error, ValueError, UnicodeEncodeError):
        pass

    # Not hex and not base64: fall back to the raw bytes so identity stays
    # deterministic instead of raising. Task 003's parser decides whether such a
    # value is acceptable at all.
    return value.encode("utf-8")


def compute_fingerprint(
    *,
    server: str,
    port: int,
    secret: str,
    protocol: str = PROTOCOL_MTPROTO,
) -> str:
    """Return the deterministic SHA-256 fingerprint of a proxy configuration.

    Identity is ``protocol + normalized server + port + secret bytes``. The secret
    is **included**: several distinct MTProto secrets routinely share one
    ``server:port``, so hashing the endpoint alone would merge different proxies
    into a single row and lose candidates.

    SHA-256 is used for its collision resistance and fixed 64-character hex
    output, which makes the UNIQUE index cheap and predictable. The hash is not a
    security boundary -- it is an identity key -- but a strong hash means an
    attacker who can insert rows cannot manufacture a collision to hijack an
    existing proxy's history.
    """
    if not isinstance(port, int) or isinstance(port, bool):
        msg = f"port must be an int, got {type(port).__name__}"
        raise TypeError(msg)
    if not 1 <= port <= 65535:
        msg = f"port must be within 1..65535, got {port}"
        raise ValueError(msg)

    normalized_protocol = protocol.strip().lower()
    if not normalized_protocol:
        msg = "protocol must not be empty"
        raise ValueError(msg)

    normalized_server = normalize_server(server)
    if not normalized_server:
        msg = "server must not be empty"
        raise ValueError(msg)

    payload = _FIELD_SEPARATOR.join(
        (
            FINGERPRINT_VERSION,
            normalized_protocol,
            normalized_server,
            str(port),
            secret_identity_bytes(secret).hex(),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def mask_secret(value: str) -> str:
    """Return a non-reversible display form such as ``abcd...7890``.

    Short values are masked entirely: revealing the head and tail of an 8
    character secret would disclose most of it.
    """
    text = value.strip()
    if not text:
        return ""
    if len(text) < _MASK_MIN_LENGTH:
        return "*" * len(text)
    return f"{text[:_MASK_HEAD]}...{text[-_MASK_TAIL:]}"


class ProxySecret:
    """An MTProto secret that cannot be printed by accident.

    ``str()``, ``repr()`` and ``format()`` all yield the masked form. The
    plaintext requires an explicit :meth:`reveal`, which gives a reviewer one
    obvious thing to grep for.

    Not JSON-serialisable on purpose: ``json.dumps`` raises ``TypeError`` rather
    than emitting the secret. (structlog's renderer falls back to ``repr()``, so
    a secret that does reach a log line is masked twice over -- once by this class
    and once by the key/value scrubber in :mod:`core.logger`.)
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        # The signature stays `str` so mypy catches misuse at typed call sites,
        # but the runtime guard is kept because plenty of callers are not typed:
        # JSON payloads, DB drivers, scripts. For a class whose whole purpose is
        # stopping a credential from leaking, that redundancy is the point.
        # Widening through `received` keeps the check meaningful to mypy instead
        # of unreachable, so no suppression is needed.
        received: object = value
        if not isinstance(received, str):
            msg = f"ProxySecret expects str, got {type(received).__name__}"
            raise TypeError(msg)
        stripped = received.strip()
        if not stripped:
            msg = "MTProto secret must not be empty"
            raise ValueError(msg)
        if len(stripped) > SECRET_MAX_LENGTH:
            msg = f"MTProto secret exceeds {SECRET_MAX_LENGTH} characters"
            raise ValueError(msg)
        self._value = stripped

    # -- explicit access ----------------------------------------------------

    def reveal(self) -> str:
        """The plaintext secret. Only for handing to the MTProto transport."""
        return self._value

    @property
    def masked(self) -> str:
        return mask_secret(self._value)

    @property
    def identity_bytes(self) -> bytes:
        return secret_identity_bytes(self._value)

    # -- safe rendering -----------------------------------------------------

    def __str__(self) -> str:
        return self.masked

    def __repr__(self) -> str:
        return f"ProxySecret({self.masked!r})"

    def __format__(self, format_spec: str) -> str:
        # The format spec is intentionally ignored: no presentation of this
        # object may ever produce the plaintext.
        del format_spec
        return self.masked

    # -- equality / hashing -------------------------------------------------

    def __eq__(self, other: object) -> bool:
        if isinstance(other, ProxySecret):
            return self._value == other._value
        if isinstance(other, str):
            # Convenient and still safe: comparison never renders the secret.
            return self._value == other
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._value)

    def __len__(self) -> int:
        return len(self._value)

    def __bool__(self) -> bool:
        return True
