# Proxy ranking & serving

## Purpose

Expose the **best currently-known** proxies from persisted Task 005
`ProxyScore` snapshots. Ranking does not probe the network, does not
recalculate scores, and does not change tester or scorer scheduling.

There is no HTTP framework in this repository (D-016). The serving contract
is `RankingService.list_top` returning a `RankingPage` of `ProxyListing`
values. A public transport (Task 010) can sit on top of this later.

```
PostgreSQL  (proxies ⋈ latest proxy_scores)
        ↓
RankingService.list_top(as_of, limit)
        ↓
RankingPage / ProxyListing   (no secrets)
```

## Serving contract

```
ProxyListing
    proxy_id
    server
    port
    secret_type          # legacy | dd | ee | unknown — not the secret
    score                # NUMERIC 0–100 from the latest v1 snapshot
    reliability_1h
    reliability_6h
    reliability_24h
    latency_p50_ms
    latency_p95_ms
    sample_count_24h
    scoring_version
    scored_at            # ProxyScore.calculated_at
```

`to_public_dict()` / `json.dumps` emit only those fields. Decimals are
strings so ordering stays exact.

## Latest-score selection

`proxy_scores` is append-only (D-022). "Latest" for a proxy is:

```
DISTINCT ON (proxy_id)
  … WHERE scoring_version = 'v1'
  ORDER BY proxy_id, calculated_at DESC, id DESC
```

`id DESC` breaks equal timestamps. Historical snapshots are never averaged,
summed, or returned as extra listings. One proxy appears at most once.

If the latest v1 row is not serviceable (stale, empty window, inactive),
the proxy is **omitted**. An older generation is not used as a fallback.

Served by `ix_proxy_scores_proxy_id_calculated_at`. No extra index in this
task: EXPLAIN under `enable_seqscan = off` uses that index for the
DISTINCT ON.

## Eligibility

A proxy is listed only when **all** of the following hold on its latest v1
snapshot:

| Rule | Why |
|---|---|
| `proxies.is_active` | Retired identities are not served |
| A v1 `ProxyScore` exists | No score → excluded |
| `scoring_version = v1` | Current formula only (D-038) |
| `sample_count_24h > 0` | Empty-window `score=0` (NO_OBSERVATIONS_IN_WINDOW) is not serviceable |
| `calculated_at >= as_of - 24h` | Snapshot still overlaps the scoring lookback |

Failed-only history (samples > 0, low reliability) **is** eligible. Ranking
is not a second definition of "healthy": Task 005 already shrank those
scores. They sort to the bottom.

`last_success_at` and tester leases are ignored.

## Freshness

`MAX_AGE_HOURS = 24`, matching v1 `LOOKBACK_HOURS`. A snapshot older than
the window it was computed over is insufficiently recent to serve.

* Stale ≠ bad proxy. It means "we will not list it until it is rescored."
* Scores and observations are not mutated.
* `as_of` is injected. The service stamps `utcnow()` once when omitted.
* `as_of` must be timezone-aware. Naive values are rejected.
* Exact boundary `calculated_at == as_of - 24h` is included (`>=`).
* Future `calculated_at` (clock skew) is treated as fresh.

The constant lives in `modules/ranking/policy.py`, not env, so two callers
cannot silently fork eligibility.

## Ordering

Deterministic, no `random()`, no `hash()`, no wall clock in the sort:

```
score DESC
scored_at DESC          # calculated_at
proxy_id ASC
```

Equal score and timestamp → lower `proxy_id` first. Repeated queries over
the same committed state return the same order.

## Pagination

Bounded first page only.

| | |
|---|---|
| Default `limit` | 20 |
| Maximum `limit` | 100 |
| `limit` omitted | 20 |
| `limit < 1` or `> 100` | `ValueError` |
| OFFSET | not offered |
| Caller ORDER BY | not offered |
| Keyset cursor | deferred until a public transport exists |

The query applies `LIMIT` in SQL after latest-per-proxy + eligibility.
There is no serving protocol yet; inventing signed cursors would add a key
and a page token with no consumer. OFFSET is still rejected.

## Security exclusions

Never on a listing, in `repr`, JSON, logs, or ranking exceptions:

* MTProto secret / `ProxySecret.reveal()`
* `tg://` / `t.me/proxy` URLs
* fingerprint (identity includes the secret)
* Telegram API hash, DB DSN, bot tokens

`secret_type` is derived from decoded identity bytes and is not reversible
to the secret. Logs carry `proxy_id` list + counts.

## Concurrency

Read-only `SELECT`. Default READ COMMITTED: one statement sees a committed
snapshot. Two concurrent `list_top` calls against the same committed data
agree. A scorer inserting mid-call cannot tear a single statement. Ranking
never takes `FOR UPDATE` and never writes `next_test_at` / `test_lock_until`.

## Query shape

```
latest v1 score id per active proxy   -- DISTINCT ON, index
  ⋈ proxies
  filter sample_count_24h > 0
  filter calculated_at >= cutoff
  order score DESC, calculated_at DESC, proxy_id ASC
  limit N
```

Python `rank_snapshots` re-applies the same latest/eligibility/order rules
to the fetched page so unit tests and SQL share one definition.

## Limitations

* First page only. Deep pagination waits for a transport (Task 010).
* Fake-TLS (`ee`) proxies that the tester marks `UNSUPPORTED_TRANSPORT`
  score as dead and, if they have samples, may appear at the bottom.
* Serving 24 h freshness assumes the scorer is running. If it is down,
  listings empty out rather than showing aged snapshots.
* No public proxy URI is generated. That is a separate security decision.
