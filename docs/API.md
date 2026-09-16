# Ranking HTTP API

Read-only HTTP transport over `RankingService.list_top`. This process does
**not** discover, test, score, or mutate proxies. Ranking rules stay in
[`docs/RANKING.md`](RANKING.md) (D-040). HTTP is an adapter (D-041).

```
PostgreSQL
    ↓
RankingService.list_top(limit)     # server stamps as_of=utcnow()
    ↓
GET /v1/proxies                    # no public as_of
```

## Process

```bash
uv run mtproto-api
# or
uv run python -m workers.api
```

Independent OS process. Uvicorn owns SIGINT/SIGTERM. It is **not** wired
through `WorkerLifecycle` (that loop is for discovery/tester/scorer ticks).

| Setting | Default | Notes |
|---|---|---|
| `API_HOST` | `127.0.0.1` | Loopback. Binding a public interface is an operator choice. |
| `API_PORT` | `8080` | `1..65535` |

Uvicorn access logs are off (`access_log=False`). Application events go
through structlog. Third-party loggers `uvicorn` / `fastapi` follow
`THIRD_PARTY_LOG_LEVEL`.

There is no Redis, no rate-limit backend, and no application-level ranking
cache. Listings may send `Cache-Control: public, max-age=30` so a reverse
proxy can cache the JSON. That header is not a second ranking store.

## Endpoints

### `GET /healthz`

Liveness. The process is up. **No database call.**

```json
{"status": "ok"}
```

Always `200` while the process is running.

### `GET /readyz`

Readiness. Database connectivity only (`Database.is_reachable`). Empty
ranking is still ready.

| Status | Body |
|---|---|
| `200` | `{"status": "ready"}` |
| `503` | `{"status": "not_ready"}` |

Failures do not include SQL, DSNs, or exception text.

### `GET /v1/proxies?limit=`

Current first page of eligible proxies.

| Query | Default | Allowed |
|---|---|---|
| `limit` | `20` | integer `1..100` |

There is **no** public `as_of`. Freshness is `utcnow()` inside
`RankingService`. A client-supplied `as_of` query parameter is ignored
(unknown query keys are not part of the contract).

`200` example:

```json
{
  "items": [
    {
      "proxy_id": 1,
      "server": "203.0.113.10",
      "port": 443,
      "secret_type": "dd",
      "score": "85.000",
      "reliability_1h": "100.00",
      "reliability_6h": "95.00",
      "reliability_24h": "90.00",
      "latency_p50_ms": "2100.000",
      "latency_p95_ms": "2500.000",
      "sample_count_24h": 8,
      "scoring_version": "v1",
      "scored_at": "2026-09-16T12:00:00+00:00"
    }
  ],
  "count": 1,
  "limit": 20,
  "scoring_version": "v1"
}
```

Decimals are strings, matching Task 006 `ProxyListing.to_public_dict`.
Empty ranking is `200` with `"items": []`, not `404` or `503`.

Interactive OpenAPI: `GET /docs` and `GET /openapi.json`. The schema does
not include ORM models, secrets, fingerprints, or `as_of`.

## Errors

Generic bodies only. Exception text, SQL, tracebacks, and secrets are never
echoed.

| Status | Body | When |
|---|---|---|
| `400` | `{"detail": "invalid request"}` | Bad `limit` (FastAPI 422 is rewritten) |
| `404` | `{"detail": "not found"}` | Unknown path |
| `405` | `{"detail": "method not allowed"}` | Wrong HTTP method |
| `500` | `{"detail": "internal error"}` | Unhandled failure |
| `503` | `{"status": "not_ready"}` | `/readyz` only, database down |

## Request IDs

Every response includes `x-request-id`.

* If the client sends `x-request-id` matching `[A-Za-z0-9._-]{1,64}`, it is
  echoed.
* Otherwise the server mints a new id. Oversized or unsafe values are
  discarded, never logged (log injection).

The id is bound into structlog contextvars for the request.

## What is never returned

Same exclusions as ranking listings:

* MTProto secret / `ProxySecret.reveal()`
* `tg://` / `t.me/proxy` URLs
* fingerprint
* Telegram API hash, bot tokens, database DSN / password
* public `as_of` (would let clients replay freshness)

`secret_type` is `legacy` / `dd` / `ee` / `unknown` and is not reversible
to the secret.

## Not in this task

* Writes, admin routes, authentication
* Redis, Celery, Cloudflare Workers
* Flask / Django / Sanic / aiohttp
* Merging this process into discovery / tester / scorer
* Changing Task 005 weights or Task 006 SQL
