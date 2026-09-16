# Deterministic proxy scoring (`mtproto-scorer`)

## Purpose

Turn persisted `ProxyObservation` rows into an explainable 0–100 quality
snapshot. The score is for ranking, deciding who to keep testing, and spotting
stable versus flaky proxies. It is **not** an AI score and it does not call a
network.

```
Discovery → Parser → PostgreSQL → Tester → Raw observations
                                              ↓
                                   Deterministic scoring
                                              ↓
                                   ProxyScore history (append-only)
```

LLM / Qwen content generation is a later publishing concern. It must not enter
this path.

## Source of truth

Scoring reads `ProxyObservation.success` produced by the corrected tester
(D-037). Success means:

```
TCP_CONNECTED
    → MTProto_TRANSPORT_CONNECTED
    → REAL_TELEGRAM_API_RPC_VERIFIED   # help.getConfig → types.Config
    → success = true
```

`client.connect()` alone is not success. `is_user_authorized()` is not used.
Scoring never fabricates a latency or a success.

Raw observations are **never** updated or deleted.

## Inputs

Per observation, only:

| Field | Used for |
|---|---|
| `observed_at` | recency weight, 1h/6h/24h windows |
| `success` | reliability |
| `mtproto_connect_ms` | latency, **only if `success`** |
| `error_category` | failure tallies (explainability) |

`tcp_connect_ms` and `total_latency_ms` are ignored for the score. TCP is not
API verification. `total_latency_ms` includes DNS + TCP + Telethon overhead.

## Formula (`scoring_version = v1`)

Lookback: last **24 hours**. Older rows are dropped.

### Recency weight

```
age_hours = max(0, (now - observed_at) / 1h)
w_i       = exp(-ln(2) * age_hours / 6)
```

Half-life **6 hours**: the tester retries successes after 1 hour and failures
after 15 minutes, so a handful of recent attempts outweigh day-old ones. This
is an engineering parameter, not a fitted optimum. At 6 h, `w = 0.5`; at 24 h,
`w = 0.0625`.

### Reliability

```
weighted_rate     = Σ (w_i * success_i) / Σ w_i
reliability_score = 100 * (Σ w_i success_i + 1) / (Σ w_i + 2)
```

Laplace smoothing `(+1, +2)` stops `1/1` reading as a proven 100% and `0/1` as
a proven 0%. Window columns `reliability_{1h,6h,24h}` stored on `proxy_scores`
are **unweighted** raw rates in that window (NULL when the window is empty), so
they stay auditable.

### Latency

Successful `mtproto_connect_ms` only. Timeouts are not converted to "5000 ms".
Missing latency on a success is skipped, not invented.

```
L                 = Σ (w_i * mtproto_connect_ms_i) / Σ w_i     # successes with a value
latency_score     = 100 * clamp(1 - L / 8000, 0, 1)
```

`8000` ms is the tester's MTProto timeout budget. `0` ms maps to 100.

Telethon's ~2 s structural wait (D-036) is **inside** every successful
`mtproto_connect_ms`. We do not subtract 2000 ms — that would fabricate a
network RTT. The floor is common to all successes, so ranking is still
meaningful. Physical TCP RTT lives on `tcp_connect_ms` and is not this score.

If there is no successful latency sample: `latency_score = 0` (unproven speed
must not inflate the rank). Percentiles `latency_p50_ms` / `latency_p95_ms` are
NULL.

### Confidence / sample size

```
confidence_factor = n / (n + 10)
confidence_score  = 100 * confidence_factor
```

`n` is the number of observations in the 24 h window. `N0 = 10` is "about ten
hourly successes (or a couple of hours of 15-minute failure retries) to reach
50% confidence."

| History | confidence_factor |
|---|---|
| 0 observations | 0 |
| 1/1 | 1/11 ≈ 0.09 |
| 10/10 | 0.50 |
| 100/100 | 100/110 ≈ 0.91 |

So `1/1` cannot outrank `100/100`.

### Final score

```
combined    = 0.75 * reliability_score + 0.25 * latency_score
final_score = combined * confidence_factor
```

Reliability dominates: a fast proxy that rarely completes `help.getConfig` is
not useful. Quantized to 3 decimal places (`NUMERIC(6,3)`), clamped to `[0, 100]`.

## Constants

| Constant | Value | Why |
|---|---|---|
| `LOOKBACK_HOURS` | 24 | Matches `proxy_scores` 24 h window |
| `RECENCY_HALF_LIFE_HOURS` | 6 | Several tester intervals; not a fitted optimum |
| `LAPLACE_ALPHA`, `LAPLACE_BETA` | 1, 1 | Uniform prior |
| `CONFIDENCE_PRIOR_N` | 10 | 50% confidence at 10 samples |
| `RELIABILITY_WEIGHT` | 0.75 | Reachability first |
| `LATENCY_WEIGHT` | 0.25 | Speed second |
| `LATENCY_BEST_MS` | 0 | Honest zero |
| `LATENCY_WORST_MS` | 8000 | Tester MTProto timeout |

These live in `modules/scoring/calculator.py`, **not** in env vars, so two
workers cannot silently fork `scoring_version=v1`. The only scoring setting is
`SCORER_BATCH_SIZE` (how many proxies per tick).

Changing the formula requires bumping `SCORING_VERSION_V1`.

## Edge cases

| Case | Result |
|---|---|
| 0 observations in the window | `score = 0`, window reliabilities NULL, status `NO_OBSERVATIONS_IN_WINDOW` |
| Only failures | Low reliability, `latency_score = 0`, percentiles NULL |
| One success | High raw window rate, low `confidence_factor`, modest `score` |
| All observations older than 24 h | Same as empty window |
| All recent | Full recency weight |
| Successes without `mtproto_connect_ms` | Reliability still computed; `latency_score = 0` |
| Future `observed_at` | Age clamped to 0 |
| Input order shuffled | Identical score |

## Failure categories

`success` is binary and is the reliability input. Categories are counted for
explainability (`failure_counts`) and are **not** turned into fake latencies.

No extra severity weights. The tester already decided success vs failure; we
do not invent semantics the taxonomy does not support (`WRONG_SECRET` remains
absent).

`SSRF_BLOCKED`, `UNSUPPORTED_TRANSPORT`, `INVALID_SECRET` are ordinary
failures (`success = false`). Fake-TLS stays unscored-as-working until a
transport can actually verify it.

## Persistence

Each run inserts a new `proxy_scores` row (D-022). Historical snapshots stay.
"Latest score" is `ORDER BY calculated_at DESC LIMIT 1`, served by
`ix_proxy_scores_proxy_id_calculated_at`.

A proxy is due when it is active, `last_test_finished_at` is set, and no v1
score has `calculated_at >= last_test_finished_at`. Claiming uses
`SELECT … FOR UPDATE SKIP LOCKED` on `proxies`. There is **no** score lease
column: scoring has no network I/O, so the row lock is held only for the short
read-compute-insert transaction. `test_lock_until` is not reused.

## Worked examples (illustrative, not live measurements)

These numbers are produced by the pure function on synthetic observations.
No proxy was contacted.

* **10/10 recent successes, 2100 ms.** Confidence 0.5. High reliability after
  Laplace, strong latency score, mid-range final score — not 100, because ten
  samples are not a long history.
* **1/1 success.** Window reliability 100%, confidence ≈ 0.09, final score
  well below 20.
* **0/10 failures.** Latency score 0, low reliability, small final score.
* **5 recent failures + 5 old successes** scores worse than the reverse,
  because of exponential decay.

## Limitations

* 24 h lookback forgets older history on purpose. Long-term reputation is a
  later scoring_version if we need it.
* Telethon's 2 s floor is in every successful `mtproto_connect_ms`.
* Fake-TLS (`ee`) proxies fail the tester as `UNSUPPORTED_TRANSPORT` and
  therefore score as dead, which is honest given current Telethon.
* Laplace leaves a dead proxy slightly above a mathematical zero. Confidence
  still keeps it at the bottom of the ranking.
* No ML, no LLM, no online learning. Identical observations always produce
  the identical v1 snapshot (given the same `now`).
