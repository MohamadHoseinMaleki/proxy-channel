# Proxy reporting & selection

## Purpose

Answer: **which proxies are currently eligible to be published?**

Reporting selects already-verified proxies from PostgreSQL. It does **not**
discover, probe, or rescore them. A high historical `ProxyScore` alone does
**not** make a proxy publishable.

```
PostgreSQL  (proxies ⋈ latest v1 proxy_scores ⋈ latest non-CANCELLED observation)
        ↓
ReportingService.select_top(as_of, limit)
        ↓
Report / ReportItem     JSON (internal payload, includes secret)
                        TXT  (one tg://proxy?... URL per line)
```

Ranking (`docs/RANKING.md`) is a different contract: secret-free listings that
may include failed-only histories. Do not reuse ranking output as a publisher
feed.

A future Telegram publisher can consume `Report.to_json()` / `Report.to_txt()`.
This module does not talk to Telegram.

## Selection rules

A proxy is publishable only when **all** of the following hold:

| Rule | Why |
|---|---|
| `proxies.is_active` | Retired identities are not published |
| Latest `ProxyScore` has `scoring_version = v1` | Current formula only |
| `sample_count_24h > 0` | Empty-window snapshots are not evidence |
| `0 ≤ score ≤ 100` and finite | Invalid scores are dropped |
| Latest **non-`CANCELLED`** observation is `success = true` | GetConfig verification, not TCP, not infra interrupt |
| That success is `observed_at >= as_of - 6h` | Recent verification, not an old high score |
| Secret type is `legacy` or `dd` | Fake-TLS (`ee`) is unverifiable (D-044) |

`CANCELLED` is skipped when deciding “latest meaningful observation” so a
killed tester tick cannot masquerade as success **or** as a failure that
hides a still-recent GetConfig. A later real failure (`TCP_TIMEOUT`, …)
**does** hide an older success.

`UNSUPPORTED_TRANSPORT` is a real tester verdict (`success = false`). It never
satisfies recent success. Fake-TLS identities are also excluded by secret type
even if a row were somehow marked successful.

Discovery timestamps, `created_at`, and `ProxyScore.calculated_at` are **not**
the recent-success clock. The clock is `proxy_observations.observed_at` on a
successful GetConfig.

## Interaction with scoring v1

The scorer may keep a high score for hours after failures start (Laplace +
24 h lookback + recency decay). Reporting still refuses the proxy once the
latest meaningful observation is a failure, or the last success is older than
6 h. Scoring v1 is unchanged (D-038 / D-045).

## Freshness

| Label | Last GetConfig success |
|---|---|
| `RECENT` | younger than 6 h |
| `AGING` | 6–24 h |
| `STALE` | older than 24 h |

Eligibility uses `REPORT_MAX_SUCCESS_AGE_HOURS` (default **6**, matching
`RECENT`). With the default, every selected item is `RECENT`. Labels are
explainability; they do not change `score`.

`as_of` is injected. The service stamps `utcnow()` once when omitted. Naive
datetimes are rejected. Exact boundary `observed_at == as_of - 6h` is included.

## Deterministic ranking

After eligibility:

```
score DESC
last_success_at DESC
latency_p50_ms ASC NULLS LAST
fingerprint ASC
```

No `random()`, no `hash()`, no wall clock in the sort. Identical committed
state → identical order.

## Top-N

| | |
|---|---|
| Default `limit` | `REPORT_DEFAULT_LIMIT` = 20 |
| Maximum `limit` | `REPORT_MAX_LIMIT` = 100 |
| `limit < 1` or `> max` | `ValueError` |
| Fewer eligible than `limit` | return all eligible |
| None eligible | empty report |
| Fabricated fillers | never |

## JSON schema

UTF-8 object. Field order is insertion order. Decimals are strings.

```json
{
  "generated_at": "2026-09-16T12:00:00+00:00",
  "scoring_version": "v1",
  "max_success_age_hours": 6.0,
  "limit": 20,
  "count": 1,
  "proxies": [
    {
      "server": "1.1.1.1",
      "port": 443,
      "secret": "ddababab…",
      "protocol": "mtproto",
      "secret_type": "dd",
      "score": "43.594",
      "scoring_version": "v1",
      "reliability_24h": "100.000",
      "sample_count_24h": 10,
      "latency_p50_ms": "2100.000",
      "latency_p95_ms": "2100.000",
      "last_success_at": "2026-09-16T11:30:00+00:00",
      "freshness": "RECENT"
    }
  ]
}
```

`generated_at` is report generation, not verification. Empty report: `count = 0`,
`proxies = []`. No placeholder proxies.

This JSON **includes the MTProto secret**. It is an internal publishing payload,
not a public API. `repr(Report)` / logs never include it.

## TXT format

One canonical URL per line, discovery-parser dialect:

```
tg://proxy?server=1.1.1.1&port=443&secret=dd…
```

Query encoding uses RFC 3986 `quote` (not `quote_plus`) so base64 `+` survives.
Empty report is the empty string (no header). Each line must round-trip through
`parse_proxy_url`. Fake-TLS URLs never appear.

A row that cannot round-trip is **skipped** (logged as `proxy_id` only); the
rest of the report is kept.

## Secret handling

* Internal objects wrap secrets in `ProxySecret`.
* JSON/TXT reveal plaintext only for the publisher.
* Logs carry `proxy_id` lists. No URLs, no secrets, no fingerprints.
* Generated reports are not written to the repository.

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `REPORT_DEFAULT_LIMIT` | 20 | page size when `limit` is omitted |
| `REPORT_MAX_LIMIT` | 100 | hard cap |
| `REPORT_MAX_SUCCESS_AGE_HOURS` | 6 | GetConfig recency; max 24 |

`scoring_version` is not a setting.

## Query / indexes

Latest score: `DISTINCT ON (proxy_id) … ORDER BY proxy_id, calculated_at DESC, id DESC`
on `ix_proxy_scores_proxy_id_calculated_at`. Latest non-cancelled observation:
the same pattern on `proxy_observations` (`ix_proxy_observations_proxy_id_observed_at`).
No new index. No N+1. One short read-only transaction. No network inside it.

## Limitations

* 6 h recency is conservative. Operators may raise it up to 24 h; they cannot
  publish successes older than the v1 lookback.
* Fake-TLS will be absent until a tester can actually verify it.
* Reporting is not HTTP and not a worker. Call `ReportingService.select_top`.
