# Spike Audit — Telethon / Pyrogram MTProto engine evaluation

**Audited:** 2026-09-15
**Auditor:** Task 001 (foundation audit)
**Scope:** `spike/spike_telethon.py`, `spike/spike_pyrogram.py`, `spike/REPORT.md`,
`spike/requirements.txt`
**Method:** static analysis of the committed spike code + **runtime verification
against the installed library source**. No network I/O. No proxy was contacted.

> **Headline:** `spike/REPORT.md` presents its section 2 findings as observed
> results ("Raw Findings"). They are **not reproducible from the committed
> code**, and two of them are contradicted by the library itself. The
> *recommendation* (Telethon) survives the audit; the *evidence* for it does not.
> Everything below states what was actually verified and how to re-verify it.

Reproduce every claim in this document with:

```bash
uv run --with telethon==1.34.0 python spike/verify_telethon_contract.py   # spike's pinned version
uv run --with telethon          python spike/verify_telethon_contract.py   # current release
```

Captured output is committed under `spike/evidence/`.

---

## 1. Verified environment facts

| Fact | Status |
|---|---|
| `telethon.network.connection.ConnectionTcpMTProxyRandomizedIntermediate` is exported | ✅ verified, 1.34.0 and 1.45.0 |
| `TelegramClient.__init__` accepts `connection=` and `proxy=` | ✅ verified |
| `proxy` must be the 3-tuple `(server, port, secret_hex)`; `TcpMTProxy.__init__` reads `proxy[2]` | ✅ verified |
| `TelegramClient.connect()` is annotated `-> None` | ✅ verified, 1.34.0 and 1.45.0 |
| `connect()` performs a real MTProto round trip: `InvokeWithLayerRequest(LAYER, help.GetConfigRequest())` | ✅ verified |
| `connect()` does **not** call `start()` / `sign_in()` | ✅ verified |
| `connect()` spawns `_update_loop` and `_keepalive_loop` background tasks | ✅ verified |
| `client.is_connected` is a **method** in Telethon | ✅ verified |
| `telethon.sessions.MemorySession` exists | ✅ verified |
| `TcpMTProxy._connect` waits **up to 2 s** after connecting (upstream workaround for issue #1134) | ✅ verified |
| `TcpMTProxy._connect` raises `ConnectionError("Proxy closed the connection after sending initial payload")` on immediate EOF | ✅ verified |
| `TcpMTProxy` docstring: *"The support for TcpMTProxy classes is **EXPERIMENTAL** … You shouldn't be using this class yet."* | ✅ verified — **absent from REPORT.md** |

---

## 2. Findings

### F1 — `spike_pyrogram.py` is a non-executable stub. (Severity: fatal to the Pyrogram comparison)

Evidence, from the committed file:

```python
TEST_CASES = {
    # ... Same as above
}                                  # -> an EMPTY dict

async def check_tcp(server, port, timeout=3.0):
    pass                           # -> returns None
```

```python
    tcp_ok, tcp_dur, tcp_err = await check_tcp(...)   # TypeError: cannot unpack None
```

There is no `main()`, no `asyncio.run(...)`, no `if __name__ == "__main__"` block,
and nothing writes `pyrogram_results.json`. The trailing line is a comment:
`# ... (main function identical to Telethon, outputs to pyrogram_results.json)`.

**Consequence:** this script cannot execute a single test case. Every Pyrogram
result in `REPORT.md` §2 ("Both successfully connected", "Both failed gracefully",
"Both yielded `TimeoutError`") and every Pyrogram ergonomics claim in §3 and §5
is **unsupported by any artifact in this repository**.

---

### F2 — `spike_telethon.py` treats `connect()` as a boolean. It never returns one. (Severity: fatal)

```python
connected = await asyncio.wait_for(client.connect(), timeout=5.0)
if connected:
    result["state"] = "MT_PROTO_CONNECTED"
else:
    result["state"] = "FAILED"
    result["error_category"] = "LIBRARY_ERROR"
```

`TelegramClient.connect()` is `-> None` in both 1.34.0 and 1.45.0 and contains no
`return True` on any path. So `connected` is **always `None`** and the `else`
branch always executes.

**Consequence:** even a genuinely successful MTProto connection through a working
proxy would have been recorded as `state=FAILED, error_category=LIBRARY_ERROR`.
`REPORT.md` §3.2's claim that `connect()` *"accurately returns `True` if Telegram
E2E is verified"* is false for the installed versions, and §2.1's claim that the
valid-proxy case connected successfully is unreachable through this code path.

---

### F3 — Every secret used by the spike is a fake-TLS `ee` secret, which Telethon 1.34.0 rejects outright. (Severity: fatal)

All four fixtures in `spike_telethon.py` (`valid`, `dead_endpoint`, `wrong_secret`,
`non_mtproto`) use secrets beginning `ee`, plus the README's example secret.

On **1.34.0**, `TcpMTProxy.__init__` does `self._secret = bytes.fromhex(proxy[2])`
and `MTProxyIO.init_header` then validates:

```python
is_dd = (len(secret) == 17) and (secret[0] == 0xDD)
secret = secret[1:] if is_dd else secret
if len(secret) != 16:
    raise ValueError("MTProxy secret must be a hex-string representing 16 bytes")
```

Measured with `spike/verify_telethon_contract.py` on Telethon 1.34.0
(`spike/evidence/telethon-1.34.0-contract.txt`):

```
format                                    hex  norm  result
spike 'valid' case secret                  54    27  ValueError: MTProxy secret must be a hex-string representing 16 bytes
spike 'wrong_secret' case                  54    27  ValueError: MTProxy secret must be a hex-string representing 16 bytes
spike 'dead_endpoint' case                 34    17  ValueError: MTProxy secret must be a hex-string representing 16 bytes
spike 'non_mtproto' case                   34    17  ValueError: MTProxy secret must be a hex-string representing 16 bytes
```

This raises during **header construction, before any socket is opened**. All four
cases would land in the spike's generic `except Exception` and be recorded as
`error_category = "UNKNOWN"`.

**Consequence:** on the version pinned in `spike/requirements.txt`, none of the
four behaviours described in `REPORT.md` §2 could have been observed.

---

### F4 — `normalize_secret` arrived in Telethon 1.35.0, not 1.34.0. (Severity: corrects an earlier draft of this audit)

Probing published wheels (`hasattr(TcpMTProxy, "normalize_secret")`):

| Telethon | `normalize_secret` |
|---|---|
| 1.34.0 | ❌ absent |
| **1.35.0** | ✅ **present (first release)** |
| 1.36.0 … 1.45.0 | ✅ present, source unchanged |

```python
@staticmethod
def normalize_secret(secret):
    if secret[:2] in ("ee", "dd"):  # Remove extra bytes
        secret = secret[2:]
    try:
        secret_bytes = bytes.fromhex(secret)
    except ValueError:
        secret = secret + '=' * (-len(secret) % 4)
        secret_bytes = base64.b64decode(secret.encode())
    return secret_bytes[:16]  # Remove the domain from the secret (until domain support is added)
```

On ≥ 1.35.0 all four spike secrets are **structurally accepted**
(`spike/evidence/telethon-1.45.0-contract.txt`).

> **Self-correction, recorded for honesty:** an earlier pass of this audit
> inspected `MTProxyIO.init_header` in isolation, bypassing `normalize_secret`,
> and concluded that even the latest Telethon rejected `ee` secrets. That
> conclusion was **wrong**. The verification script now decodes secrets through
> the same entry point the transport uses. The corrected matrix is in
> `spike/evidence/`.

---

### F5 — Structural acceptance of `ee` secrets is **not** working fake-TLS. (Severity: the key open risk for the MVP)

`normalize_secret` truncates to 16 bytes and **discards the SNI domain** — its own
comment says *"until domain support is added"*. `MTProxyIO` contains no TLS
`ClientHello`, no `server_name` extension and no `ssl` usage (verified by source
scan; the script reports `MTProxyIO implements TLS ClientHello / SNI emulation: False`).

The purpose of a fake-TLS secret is to make the proxy emit a plausible TLS
handshake for a specific domain. Telethon ≥ 1.35.0 does not do that.

**Status: EMPIRICALLY UNVERIFIED.** Whether real-world `ee`-secret MTProxies
accept Telethon's handshake cannot be answered by reading source. It requires a
live test against a real proxy — see §4, and Task 016's `pytest -m live`.
**No claim is made either way in this repository.**

---

### F6 — The spike wrote session files to disk while claiming to be in-memory. (Severity: requirement violation)

```python
# Ephemeral session (in-memory) to avoid locking DBs     <- the code comment
client = TelegramClient(f"memory_{case_name}", int(API_ID), API_HASH, **connection_kwargs)
```

Passing a **string** makes Telethon create `memory_<case>.session`, a SQLite file
on disk. The in-memory implementation is `telethon.sessions.MemorySession`
(verified available). This contradicts both the comment and the project rule
*"Do not store a Telegram session."*

---

### F7 — `is_connected` asymmetry between the two libraries. (Severity: latent bug)

`client.is_connected` is a **property in Pyrogram** and a **method in Telethon**.
`spike_pyrogram.py` uses `if client.is_connected:` — correct for Pyrogram, and
always-truthy if copied into Telethon code. `REPORT.md` §3 does not mention this.

---

### F8 — `REPORT.md` §2 and §5 report measurements that no artifact supports.

* §2 presents four numbered cases as "Raw Findings". Per F1–F3 these cannot have
  come from the committed code.
* §5 claims *"Under repeated load testing of dead/timeout proxies, Telethon's
  connection pool cleanly garbage collects its sockets."* No load test exists in
  the repository.
* §2's latency columns (`tcp_connect_ms`, `mtproto_connect_ms`) exist in the
  scripts, but **no results file was ever committed** (`results.json`,
  `telethon_results.json`, `pyrogram_results.json` are all absent, and
  `.gitignore` excludes `results.json`).
* No latency, success-rate or uptime figure anywhere in this repository has been
  measured against a real proxy.

---

## 3. What survives the audit

`REPORT.md`'s **recommendation of Telethon stands**, re-based on verified facts:

1. **Genuine unauthenticated E2E validation exists.** `connect()` sends
   `InvokeWithLayerRequest(LAYER, help.GetConfigRequest())` — a real MTProto RPC
   round trip through the proxy to Telegram's DC — and never calls `start()` or
   `sign_in()`. Success is therefore observable as *"no exception raised"*, which
   is exactly what Task 005 needs (Phase 3 without a user login).
2. **Explicit transport injection.** `connection=ConnectionTcpMTProxyRandomizedIntermediate`
   plus `proxy=(server, port, secret)` gives control over the obfuscation layer.
3. **A usable "not MTProto" signal.** `_connect` raises
   `ConnectionError("Proxy closed the connection after sending initial payload")`
   when the peer EOFs immediately — a real discriminator for Phase 2.
4. **`MemorySession`** supports the no-on-disk-session requirement.
5. `REPORT.md` §4's reasoning that a wrong secret is indistinguishable from a
   blackholed proxy (MTProxy deliberately drops bad payloads without RST) is
   **sound protocol reasoning** and is retained — but as a *hypothesis*, not an
   observation. It is the reason `WRONG_SECRET` is deliberately **absent** from
   the Task 005 error taxonomy.

The Pyrogram comparison is **withdrawn as unevidenced**. Re-running it is
optional and out of MVP scope.

---

## 4. Binding consequences for Task 005

| # | Constraint | Source |
|---|---|---|
| 1 | Pin **`telethon >= 1.35.0`** (target current 1.45.x). **Never 1.34.0.** | F3, F4 |
| 2 | Treat `connect()` as *no exception raised ⇒ success*. Never as a boolean. | F2 |
| 3 | Use `MemorySession()`. Never pass a string session name. | F6 |
| 4 | Budget **≥ 2 s** of unavoidable latency inside the transport's `_connect` when setting `MTPROTO_TIMEOUT_SECONDS`; the recommended 5–8 s ceiling is compatible, a 3 s ceiling is not. | §1 |
| 5 | Always `await client.disconnect()` in a `finally`, and don't rely on `client.is_connected` truthiness — **call** it. | §1, F7 |
| 6 | `connect()` leaves `_update_loop` / `_keepalive_loop` tasks behind; disconnect plus explicit task accounting is mandatory for the Task 017 leak test. | §1 |
| 7 | Do **not** emit a `WRONG_SECRET` category. Map those cases to `MTPROXY_TIMEOUT`. | §3.5 |
| 8 | Treat `TcpMTProxy` as upstream-**EXPERIMENTAL**: wrap it, pin the version, and re-run `verify_telethon_contract.py` on every Telethon upgrade. | §1 |
| 9 | **No** claim may be made that any real proxy works, or that fake-TLS `ee` proxies work through Telethon, until a live test has been run and its output committed. | F5, F8 |

---

## 5. Disposition of the original files

* `spike/REPORT.md` — retained verbatim for history, with a correction banner
  pointing here. Its §6 recommendation is still the project's position.
* `spike/spike_telethon.py`, `spike/spike_pyrogram.py` — retained as historical
  artifacts, **excluded from `ruff`/`mypy`** (they are known-broken and are not
  production code). Not to be copied into `src/`.
* `spike/verify_telethon_contract.py` — the replacement for "trust the report":
  a runnable, version-aware contract check with no network dependency.
* `spike/evidence/` — committed outputs of that script for 1.34.0 and 1.45.0.
