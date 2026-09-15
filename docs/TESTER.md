# MTProto Proxy Connectivity Tester (`mtproto-tester`)

## 1. Overview & Core Hypothesis

The `mtproto-tester` worker process is responsible for empirically validating,
measuring, and diagnosing candidate MTProto proxies discovered by `mtproto-discovery`.

The testing engine operates under strict non-negotiable architectural principles:
* **No fake network results**: No fabricated latency numbers; proxies are only
  marked working if verified end-to-end.
* **No account credentials required**: Does not perform user authentication (`sign_in`
  or `start()`); probes unauthenticated MTProto RPC capabilities only.
* **Ephemeral testing**: Uses `MemorySession` exclusively; never leaves `.session`
  SQLite files or credentials on disk.
* **Transaction discipline**: Claiming and recording take brief database transactions;
  network I/O runs strictly **outside** any database transaction.
* **Defense-in-depth security**: SSRF filtering, DNS rebinding elimination via
  destination IP pinning, and strict credential scrubbing in all logs and errors.

---

## 2. Telethon MTProxy Audit Findings (Ground Truth)

Telethon (pinned at `>=1.35.0`, verified with `1.45.0`) was audited in detail
against MTProxy protocols:

### Transport Classes & Codecs
* `ConnectionTcpMTProxyRandomizedIntermediate`: 4-byte length prefix with randomized
  cryptographic padding. Mandatory for `0xdd` secrets (`MTProxyIO` raises `ValueError`
  if used with other codecs). Also compatible with legacy 16-byte secrets, providing
  maximum anti-censorship resistance.
* `ConnectionTcpMTProxyIntermediate`: 4-byte length prefix, legacy 16-byte secrets only.
* `ConnectionTcpMTProxyAbridged`: 1-byte length prefix, legacy 16-byte secrets only.

### Fake-TLS (`0xee`) Limitation
* In `telethon.network.connection.tcpmtproxy.TcpMTProxy.normalize_secret`, Telethon
  parses `0xee` secrets by stripping the prefix and truncating the secret to 16 bytes:
  ```python
  if secret.startswith(b'\xee'):
      # fake-TLS secret format: ee + 16-byte-secret + domain
      # for now, the domain is ignored until domain support is added
      return secret[1:17]
  ```
* Telethon's `MTProxyIO` **does not implement wire-level TLS emulation**: it generates
  neither a TLS `ClientHello` handshake nor the server name indication (SNI) extension.
* Real Fake-TLS proxies that enforce TLS handshakes will drop or reset connections
  from standard Telethon clients.
* **Decision**: Rather than fabricating support or producing inaccurate timeout errors,
  the tester classifies Fake-TLS secrets as `UNSUPPORTED_TRANSPORT` with an honest
  diagnostic message explaining this limitation.

### Built-in 2-Second Latency Floor
* In `TcpMTProxy._connect()`:
  ```python
  # Telegram servers close the connection if we send data too fast
  # (issue #1134). So wait for the proxy to acknowledge us.
  self._wait_for_data('proxy')
  ```
  `_wait_for_data('proxy')` enforces an unconditional **2.0-second timeout/wait**.
* Consequently, all Telethon-measured MTProto connects have an artificial ~2-second
  floor.
* **Solution**: The tester measures raw TCP transport handshake latency separately
  in Phase 2 (`tcp_connect_ms`), providing the genuine physical network RTT alongside
  the MTProto transport round-trip (`mtproto_connect_ms`).

### Session Management
* Telethon defaults to creating a `<session_name>.session` SQLite database file on disk.
* For ephemeral proxy validation, creating files for thousands of candidate proxies
  would cause disk I/O thrashing and leaks.
* The probe strictly instantiates `MemorySession()`, keeping session state entirely in
  RAM and discarding it when `client.disconnect()` is called.

---

## 3. Three-Phase Probe Architecture

Every proxy undergoes a 3-phase inspection:

```
[Candidate Proxy]
       │
       ▼
[Phase 1: DNS & SSRF Validation]
  - Hostname syntax check
  - getaddrinfo() for all address records
  - Check against RFC 1918, loopback, link-local (169.254.169.254)
  - PIN destination to validated IP literal (eliminates DNS rebinding)
       │
       ▼
[Phase 2: Raw TCP Handshake]
  - asyncio.open_connection(pinned_ip, port)
  - Measure tcp_connect_ms (sub-millisecond precision)
  - Catch ConnectionRefused, Reset, Timeout
  - writer.close() & wait_closed()
       │
       ▼
[Phase 3: MTProto Transport & API Verification]
  - Validate hex secret syntax
  - Select transport class (Randomized Intermediate)
  - TelegramClient(MemorySession(), proxy=(pinned_ip, port, secret))
  - await client.connect()
  - await client.is_user_authorized() (unauthenticated RPC round-trip)
  - Measure mtproto_connect_ms & total_latency_ms
  - client.disconnect() in finally block
```

---

## 4. Error Category Taxonomy

Failure observations are classified into unambiguous categories in `ErrorCategory`:

| Category | Meaning |
|---|---|
| `SSRF_BLOCKED` | Destination IP is private, loopback, or cloud metadata |
| `DNS_NXDOMAIN` | DNS hostname does not exist |
| `DNS_TIMEOUT` | DNS query timed out |
| `DNS_ERROR` | General DNS resolution failure |
| `TCP_TIMEOUT` | TCP SYN handshake timed out |
| `TCP_REFUSED` | Target host rejected TCP connection (RST) |
| `TCP_RESET` | Connection reset by peer during transport |
| `TCP_ERROR` | Generic network socket error during TCP phase |
| `MT_PROTO_TIMEOUT` | MTProto handshake timed out / dropped (blackholed) |
| `MT_PROTO_ERROR` | MTProto transport closed or corrupted |
| `PROTOCOL_ERROR` | Upstream protocol or frame framing mismatch |
| `TELEGRAM_RPC_ERROR`| Telegram API returned an RPC failure |
| `API_AUTH_ERROR` | Missing `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` |
| `UNSUPPORTED_TRANSPORT`| Fake-TLS secret (requires TLS emulation wrapper) |
| `INVALID_SECRET` | Secret string is malformed or invalid hex |
| `CANCELLED` | Probe was cancelled before completion |
| `UNKNOWN_ERROR` | Unclassified runtime exception |

*Note*: `WRONG_SECRET` is deliberately absent from the taxonomy. MTProxy servers
drop bad-secret payloads without returning an RST or error packet, making a wrong
secret indistinguishable from a blackholed endpoint.

---

## 5. Worker Loop & Database Coordination

The `mtproto-tester` worker coordinates execution as an independent OS process:
* **Batch Claiming**: Calls `claim_due_proxies` to claim up to `TESTER_BATCH_SIZE`
  due proxies using `SELECT ... FOR UPDATE SKIP LOCKED`.
* **Bounded Concurrency**: Uses `asyncio.Semaphore(TESTER_CONCURRENCY)` to bound
  active socket connections and prevent local file descriptor exhaustion.
* **Observation Recording**: Each test writes an append-only `ProxyObservation` row.
* **Rescheduling**:
  * On success: schedules `next_test_at` 1 hour forward; updates `last_success_at`.
  * On failure: schedules `next_test_at` 15 minutes forward; updates `last_failure_at`.
  * Clears `test_lock_until = None` so claims release immediately.
* **Crash Recovery**: If a worker is hard-killed (`kill -9`), uncompleted claims
  expire after `DEFAULT_LEASE_SECONDS` (300 s) and are automatically picked up
  by other workers.

---

## 6. Live Smoke Testing (`scripts/live_mtproto_test.py`)

A standalone script is available for interactive live testing without database
dependencies:

```bash
export TELEGRAM_API_ID=123456
export TELEGRAM_API_HASH=0123456789abcdef0123456789abcdef
export PROXY_SERVER=198.51.100.1
export PROXY_PORT=443
export PROXY_SECRET=dd11111111111111111111111111111111
uv run python scripts/live_mtproto_test.py
```

Credentials are read via environment variables to prevent shell history leaks.
Output provides detailed diagnostic timings for TCP and MTProto transport phases.
