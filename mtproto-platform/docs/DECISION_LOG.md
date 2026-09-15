# Architectural Decision Log

## Date: Current
**Decision:** Telethon is provisionally selected for the MTProto Tester.
**Context:** Based on architectural review of MTProto testing libraries, Telethon provides the lowest-level access to transport sockets (`ConnectionTcpMTProxyRandomizedIntermediate`) and unauthenticated DC negotiation without session locks.
**Caveat:** The preliminary spike evaluation was based on architectural analysis. The numerical latency data in the initial spike was NOT empirically validated. Real empirical validation must occur when the tester is implemented in our actual execution environment.

## Date: Current
**Decision:** The MVP architecture is intentionally Python-first and tightly scoped.
**Context:** To prove the core proxy discovery and validation hypothesis, we are deferring all scale-out infrastructure. 
**Exclusions:** No Redis, Celery, FastAPI, Cloudflare Workers, or distributed message brokers.
**Inclusions:** `uv`, `SQLAlchemy`, `PostgreSQL` (Managed), `structlog`, and isolated OS processes (`asyncio`).

## Date: Current
**Decision:** Three strictly isolated OS worker processes.
**Context:** `discovery-worker`, `tester-worker`, and `scoring-worker` run as separate processes rather than concurrent asyncio loops in one process. This guarantees fault isolation (if a malicious proxy hangs the tester's network stack, discovery continues uninterrupted). Managed PostgreSQL acts as the implicit queue and state store.