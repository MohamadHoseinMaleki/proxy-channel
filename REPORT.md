# MTProto Library Spike Report

## 1. Environment
* Python 3.11.4
* Telethon 1.34.0
* Pyrogram 2.0.103
* Execution: Isolated `asyncio` loops

## 2. Test Cases & Raw Findings
Both libraries were subjected to four test cases:
1. **Valid Proxy**: Both successfully connected (`client.connect()`), negotiated the DH key exchange with Telegram's DC, and disconnected gracefully.
2. **Dead Endpoint (TCP Drop)**: Both failed gracefully at the TCP check stage (`TCP_TIMEOUT`).
3. **Wrong Secret**: Both established a TCP connection, sent the initial payload, and then *hung* until timeout. The proxy blackholes invalid secrets rather than returning an error. Both yielded `TimeoutError` during `connect()`.
4. **Non-MTProto (Standard HTTP)**: Both established TCP, sent the MTProto magic bytes, received non-compliant HTTP bytes back, and rapidly failed with a `ConnectionResetError` or `ProtocolError`.

## 3. API Ergonomics & Internals
* **Telethon:** Treats connections as explicit, injected dependencies. You pass `connection=ConnectionTcpMTProxyRandomizedIntermediate`. This gives extremely granular control over the network layer. If we need to subclass the socket behavior to get exact byte-level timings in the future, Telethon's `telethon.network.connection` module makes this trivial.
* **Pyrogram:** Abstracts the proxy via a simple dictionary `proxy={"scheme": "mtproxy", ...}`. Under the hood, it implements its own MTProxy logic, but it is tightly coupled to its `Session` class. It is slightly harder to pull apart the TCP phase from the MTProto DH exchange phase natively without our custom wrapper.

## 4. Error Classification Matrix
*Neither* library can magically differentiate a "Wrong Secret" from a "Dead MTProxy Backend" because MTProxies intentionally drop malicious/wrong-secret payloads without a TCP RST or error packet (to avoid active probing by censors). Therefore, `WRONG_SECRET` always manifests as `MTPROXY_HANDSHAKE_FAILED` (Timeout). This is a protocol limitation, not a library flaw.

## 5. Resource Cleanup
Both libraries clean up cleanly when using `await client.disconnect()`. However, Telethon's lower-level control made it easier to ensure `asyncio.Task` cleanup if the connection was violently aborted mid-handshake.

## 6. Conclusion and Recommendation

**RECOMMENDATION: TELETHON**

**Why?**
1. **Precise Transport Control:** Telethon explicitly exposes the MTProxy transport classes (e.g., `ConnectionTcpMTProxyRandomizedIntermediate`). We can target the exact obfuscation layer. Pyrogram abstracts this away, which limits debugging if a proxy uses a non-standard padding.
2. **Unauthenticated Execution:** Telethon's `client.connect()` perfectly halts after the network and DC configuration phase, never attempting auth unless `client.start()` or `client.sign_in()` is called. It accurately returns `True` if Telegram E2E is verified.
3. **Extensibility:** Telethon's decoupled `network` layer allows us to easily inject our own TCP timing wrappers in the future without forking the entire library.
4. **Stability:** Under repeated load testing of dead/timeout proxies, Telethon's connection pool cleanly garbage collects its sockets.

We will proceed with **Telethon** as the protocol engine for the `tester-worker`.