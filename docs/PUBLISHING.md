# Telegram channel publishing

Task 013. Posts **currently publishable** proxies to a Telegram channel.

This module does **not** discover, probe, rescore, or invent eligibility. It
calls `ReportingService.select_top` (Task 012 / D-046) and publishes that list.

```
ReportingService.select_top
        ↓
validate_publication
        ↓
PublicationFormatter
        ↓
PublishingService (outbox claim / send)
        ↓
TelegramPublisher.publish(message)
        ↓
Telegram Bot API  (https://api.telegram.org/bot<token>/sendMessage)
```

Qwen, Cloudflare, a public API, frontend, monetization, and growth are out of
scope. Scoring v1 and reporting selection are unchanged.

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | *(unset)* | Bot API token. `SecretStr`. Blank = idle worker |
| `TELEGRAM_CHANNEL_ID` | *(unset)* | `@channel` or numeric chat id. Required with the token |
| `PUBLISHER_TIMEOUT_SECONDS` | 15 | HTTP read/write timeout |
| `PUBLISHER_CONNECT_TIMEOUT_SECONDS` | 5 | TCP connect timeout |
| `TELEGRAM_PUBLICATION_LEASE_SECONDS` | 60 | stale `sending` recovery |
| `TELEGRAM_MAX_RETRIES` | 8 | send attempts before `failed` |
| `TELEGRAM_RETRY_BASE_SECONDS` | 2 | exponential base |
| `TELEGRAM_RETRY_MAX_SECONDS` | 300 | backoff cap |

Neither value is hard-coded. The Bot API host is **not** configurable
(`https://api.telegram.org` only) so a channel id cannot become an SSRF target.

The publisher worker is idle when token or channel is missing: it logs
`skipped=unconfigured` and posts nothing.

```bash
uv run mtproto-publisher
# or
uv run python -m workers.publisher
```

A real channel post is an operator action with real credentials. Unit and
integration tests inject `FakeTelegramPublisher` or `httpx.MockTransport`.
They never call Telegram.

## Message format

Deterministic plain text (Task 015 / D-049). Same `ReportItem` → same string.
No `parse_mode`. External fields are stripped of control characters and
markup. Country/location is **omitted** — the schema does not store it.

```
MTProto proxy
proxy: 1.1.1.1:443
protocol: mtproto
status: RECENT
last_checked: 2026-09-16T11:30:00+00:00
quality: score 85.000
reliability_24h: 90.00
sample_count_24h: 10
latency_p50_ms: 2100.000
secret_type: dd

tg://proxy?server=1.1.1.1&port=443&secret=dd…
```

The last line is the canonical URL and is never dropped. Missing optional
metrics are omitted, not invented. The MTProto secret is **not** a labeled
field; it appears only inside `tg://proxy?...` (users need it to connect).
Internal ids, fingerprints, bot tokens, and error traces never appear.

Telegram `sendMessage` is capped at 4096 characters. Optional quality lines
are dropped from the bottom until the message fits; the URL stays.

## Pre-publication validation

`validate_publication` runs **after** `select_top` and **before** enqueue/send.
It does not change scoring, ranking, or selection. Invalid items are logged
(`publication_rejected`, `reason=…`) and never posted.

| Reason | Meaning |
|---|---|
| `invalid_port` | not an integer in 1..65535 |
| `invalid_host` | empty, private, loopback, or not a public host |
| `invalid_protocol` | not `mtproto` |
| `invalid_secret` | not a legacy/`dd` MTProto secret |
| `fake_tls` | `ee` / Fake-TLS (Telethon cannot verify it) |
| `malformed_proxy` | cannot build or parse a canonical `tg://proxy` |

## Duplicate protection

One outbox row per `(proxy_id, channel_id)`
(`uq_proxy_publications_proxy_channel`). Re-running the worker does not
enqueue a second row. Only `pending` rows whose lease is free are claimed
(`FOR UPDATE SKIP LOCKED`).

## Telegram Publishing Reliability

Lifecycle (migration `0003`, D-048):

```
pending  →  sending  →  published
                ↓
            pending     (transient error, next_attempt_at)
                ↓
            failed      (permanent error or retries exhausted)
```

A crashed worker leaves `sending` with `lease_until`. The next tick
**recovers** stale sending rows to `pending` and may claim them again.

| Variable | Default | Notes |
|---|---|---|
| `TELEGRAM_PUBLICATION_LEASE_SECONDS` | 60 | Must exceed Bot API timeout |
| `TELEGRAM_MAX_RETRIES` | 8 | Send attempts before `failed` |
| `TELEGRAM_RETRY_BASE_SECONDS` | 2 | Exponential base |
| `TELEGRAM_RETRY_MAX_SECONDS` | 300 | Cap |

Retry delay is `min(base * 2**(attempt-1), max)`, then at least Telegram
`parameters.retry_after` or the `Retry-After` header. The worker does **not**
sleep; `next_attempt_at` defers the row until a later tick.

Transient: timeout, connect error, HTTP 429, HTTP 5xx, malformed Telegram
JSON. Permanent: HTTP 400/401/403/404, empty message, Fake-TLS (never
enqueued).

### Duplicate semantics / exactly-once limitation

Telegram Bot API `sendMessage` has **no idempotency key**. Delivery is
**at-least-once internally**:

| Window | Behaviour |
|---|---|
| Crash before HTTP | lease expires → resend. No Telegram duplicate. |
| Crash after Telegram accepted, before `published` commit | recovery resends. **One duplicate Telegram message is possible.** |
| After `published` commits | unique row, no further send. |

Do not claim exactly-once delivery to Telegram.

## Audit table

`proxy_publications`:

| Column | Notes |
|---|---|
| `proxy_id` | FK, `ON DELETE RESTRICT` |
| `channel_id` | destination, not a secret |
| `status` | `pending` / `sending` / `published` / `failed` |
| `telegram_message_id` | required on `published` |
| `error_message_safe` | last error; required on `failed` |
| `attempt_count` | send attempts |
| `last_attempt_at` | last claim/send |
| `next_attempt_at` | when a `pending` row is due |
| `lease_until` | required on `sending` |
| `created_at` | `TIMESTAMPTZ` |

The channel message body (MTProto secret) is **not** stored.

## Secrets

* Token is `SecretStr`. `repr(Settings)` / `safe_dump()` mask it.
* Logs carry `proxy_id` and `telegram_message_id`. No URLs, no secrets, no token.
* The Bot API path contains the token; log redaction already masks `/bot<id>:<token>/` (D-008).

## Limitations

* Fake-TLS is never posted (Telethon cannot verify it, D-044).
* This is not a content bot: no Qwen, no captions beyond the standard template.
