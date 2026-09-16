# MTProto Proxy Discovery & Parsing (Tasks 003 / 008)

This document describes the discovery, parsing, normalisation, validation, SSRF protection,
persistence, and **discovery worker** implemented in Tasks 003 and 008.

---

## 1. Domain Model

The discovery layer defines immutable domain models in `src/modules/discovery/models.py`:

* **`MTProtoProxy`**: Represents a validated, normalised MTProto proxy configuration.
  - `server`: Canonical hostname or IP literal (lowercase, unbracketed IPv6, trailing dot stripped).
  - `port`: TCP port (`1 <= port <= 65535`).
  - `secret`: Wrapped in `core.identity.ProxySecret`. Plaintext is never exposed in `__repr__` or `__str__`.
  - `protocol`: Default `"mtproto"`.
  - `secret_type`: One of `SecretType.LEGACY`, `SecretType.SECURE_RANDOMIZED`, or `SecretType.FAKE_TLS`.
  - `sni_domain`: Preserved SNI hostname for fake-TLS secrets (e.g. `"google.com"`).
  - `fingerprint`: Precomputed SHA-256 canonical identity digest.
* **`DiscoveredProxyCandidate`**: A discovered proxy with provenance:
  - `proxy`: The `MTProtoProxy` instance.
  - `source_type`: `telegram_channel`, `http_page`, `raw_text`, or `manual`.
  - `source_name`: Channel handle (e.g. `"@proxy_channel"`) or source label.
  - `source_url`: URL of the page/channel (or `None`).
  - `raw_reference`: Scrubbed reference string (secrets masked with `scrub_secrets`).
  - `discovered_at`: Timestamp (UTC).

---

## 2. Accepted URL Formats

The parser in `src/modules/discovery/parser.py` extracts and parses MTProto proxies from:
1. `tg://proxy?server=...&port=...&secret=...`
2. `https://t.me/proxy?server=...&port=...&secret=...`
3. `http://t.me/proxy?server=...&port=...&secret=...`

### Parsing Rules
* **Parameter order independence:** `server`, `port`, and `secret` can appear in any order.
* **Percent encoding:** Standard percent encoding (`%20`, `%2E`, etc.) is decoded safely.
* **Duplicate parameters:**
  - Duplicate parameters with *identical* values (e.g. `port=443&port=443`) are accepted.
  - Duplicate parameters with *conflicting* values (e.g. `port=443&port=8443`) are rejected with `ProxyParseError`.
* **Surrounding noise:** Text and HTML extraction handles surrounding punctuation (`.`, `,`, `!`, `]`), HTML entity decoding (`&amp;` -> `&`), and HTML attribute embedding (`<a href="...">`).
* **Non-crashing:** The parser catches malformed syntax, bad percent encoding, and out-of-range parameters gracefully, returning errors or skipping invalid entries in bulk text.
* **Zero secret leakage:** Exceptions and logs never echo raw secrets or unscrubbed URLs.

---

## 3. Server & Port Validation

Server validation in `src/modules/discovery/normalizer.py`:
* **Public IPv4 & IPv6:** Accepted. Bracket notation (`[::]`) is normalised.
* **DNS Hostnames:** Syntactically validated against RFC 1035 / RFC 1123 without DNS resolution.
  - Length 1..253 chars, labels 1..63 chars, alphanumeric with hyphens, non-numeric TLD.
* **Disallowed Endpoints (Rejected):**
  - Loopback (`127.0.0.0/8`, `::1`)
  - Private networks (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `fc00::/7`)
  - Link-local (`169.254.0.0/16`, `fe80::/10`)
  - Cloud metadata endpoints (`169.254.169.254`, `metadata.google.internal`)
  - IPv4-mapped IPv6 equivalents (e.g. `::ffff:127.0.0.1`, `::ffff:169.254.169.254`)
  - Multicast (`224.0.0.0/4`, `ff00::/8`)
  - Unspecified (`0.0.0.0`, `::`)
  - Reserved & documentation ranges (`240.0.0.0/4`, `192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`, `100.64.0.0/10`)
  - Localhost and internal domains (`.localhost`, `.local`, `.internal`, `.arpa`, `.lan`)

Port validation:
* Strictly `1 <= port <= 65535`.
* Decimals, negative values, 0, >65535, non-numeric strings, and booleans are rejected.

---

## 4. Secret Formats & Telethon Capabilities

Decoded by trying hexadecimal first, followed by base64 fallback:

| Format | Byte Structure | Wire / Encoding | Telethon Wire Support |
|---|---|---|---|
| **Legacy MTProto** | 16 bytes | 32 hex chars or base64 | Supported across all versions |
| **Randomized (`dd`)** | 17 bytes (byte 0 = `0xDD`) | 34 hex chars (starts `dd`) | Supported across all versions |
| **Fake-TLS (`ee`)** | >= 17 bytes (byte 0 = `0xEE`) | 34+ hex chars (starts `ee`) | Structurally accepted in Telethon >= 1.35.0; **SNI emulation empirically unverified** |

### Fake-TLS SNI Handling
* If length is 17 bytes: valid fake-TLS without explicit SNI (`sni_domain = None`).
* If length > 17 bytes: bytes from index 17 onwards are decoded as ASCII hostname.
  - If valid hostname: extracted as `sni_domain` and preserved.
  - If invalid ASCII or invalid hostname syntax: rejected with `SecretValidationError`.
* **Important:** Telethon >= 1.35.0 truncates secrets to 16 bytes and drops SNI domain in its current transport implementation. We preserve the SNI domain on the domain model so future testers or transports (Task 005) have full transport context.

---

## 5. Fingerprinting & Normalisation

Reuses `core.identity.compute_fingerprint`:
```
SHA-256("v1" + 0x1f + protocol + 0x1f + normalized_server + 0x1f + port + 0x1f + secret_bytes_hex)
```
* URLs differing only in parameter order, case, or bracket notation yield the **exact same fingerprint**.
* Genuinely different proxies (different server, port, key, or SNI domain) yield **different fingerprints**.

---

## 6. SSRF Protection & HTTP Fetching

Implemented in `src/modules/discovery/http.py`:
* **`SsrfSafeHttpClient`**:
  - Finite connect and read timeouts (default 15s / 5s connect).
  - Bounded response bodies (default max 2 MiB) via chunked stream reading.
  - Controlled redirect handling: max 3 redirects. `follow_redirects` is never enabled; hops are followed in process so each `Location` is SSRF-checked.
  - **Every redirect hop** validates destination scheme (`http`/`https`) and resolves DNS to verify that the target IP does not resolve to loopback, private, link-local, multicast, mapped, or cloud metadata ranges.
  - Mixed public+private DNS answers are rejected (Happy Eyeballs must not pick the private record).
  - Resolved public IPs are **pinned** for the duration of the request (`ContextVar` + `getaddrinfo` wrapper) so a rebinding hostname cannot connect to a later private answer.
  - HTTP status errors, timeouts, and transport errors are wrapped as `HttpFetchError` with `safe_error_message` (no raw URL / secret in the exception).
  - The worker owns one client and `aclose()`s it in `finally`.

---

## 7. Source Adapters

Implemented in `src/modules/discovery/sources/`:
* **`TelegramWebSource`**:
  - Scrapes public Telegram channel web previews (`https://t.me/s/{channel_name}`).
  - **Zero login requirement:** No API ID, no phone number, no bot token, no session file.
* **`RawHttpSource`**:
  - Scrapes public HTTP/HTTPS URLs (pastebins, raw text lists) via `SsrfSafeHttpClient`.
* **`RawTextSource`**:
  - Parses in-memory strings or manual copy-pastes.

---

## 8. Persistence & Discovery Lifecycle

Implemented in `src/modules/discovery/service.py`:
* **Atomic Upsert:**
  - Uses PostgreSQL `INSERT INTO proxies ... ON CONFLICT (fingerprint) DO UPDATE`.
  - On new proxy: inserts with `is_active=True`, `first_seen_at=now`, `last_seen_at=now`, `next_test_at=now` (due immediately).
  - On existing proxy: updates `last_seen_at=now`, preserves `first_seen_at`, retains existing test results and error categories.
* **Provenance Log:**
  - Inserts append-only record into `proxy_discoveries` with scrubbed `raw_reference`, `source_type`, `source_name`, `source_url`.
* **Lifecycle State:**
  ```
  DISCOVERED -> STRUCTURALLY_VALID -> TEST_PENDING (next_test_at <= now)
  ```
  * Discovery **never** marks a proxy as `WORKING` or records fake observations. Connectivity is the exclusive domain of the tester worker (Task 005/006).
