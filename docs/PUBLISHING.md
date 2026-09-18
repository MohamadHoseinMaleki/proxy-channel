# Telegram channel publishing

Task 013. Posts **currently publishable** proxies to a Telegram channel.

This module does **not** discover, probe, rescore, or invent eligibility. It
calls `ReportingService.select_top` (Task 012 / D-046) and publishes that list.

```
ReportingService.select_top
        ↓
PublishingService.publish_cycle
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

Deterministic plain text. Same `ReportItem` → same string.

```
MTProto proxy
server: 1.1.1.1
port: 443
secret: dd…
score: 85.000
reliability_24h: 90.00
sample_count_24h: 10
latency_p50_ms: 2100.000
freshness: RECENT
secret_type: dd

tg://proxy?server=1.1.1.1&port=443&secret=dd…
```

The last line is the Task 012 canonical URL. No HTML `parse_mode` (server
strings must not become markup). Fake-TLS identities are refused even if a
caller forges a `Report`.

## Duplicate protection

A **successful** post is unique per `(proxy_id, channel_id)`
(`uq_proxy_publications_success`). Re-running the worker does not send the
same proxy again.

A **failure** is appended and **may** be retried on a later tick if the proxy
is still selected. One tick never retries the same item after a failure.

Telegram I/O is outside a database transaction. Each attempt is committed
before the next send, so one failure cannot roll back earlier successes.

## Audit table

`proxy_publications` (migration `0002`):

| Column | Notes |
|---|---|
| `proxy_id` | FK, `ON DELETE RESTRICT` |
| `channel_id` | destination, not a secret |
| `status` | `success` or `failure` |
| `telegram_message_id` | required on success |
| `error_message_safe` | required on failure; scrubbed, ≤500 chars |
| `created_at` | `TIMESTAMPTZ` |

The channel message body (MTProto secret) is **not** stored.

## Secrets

* Token is `SecretStr`. `repr(Settings)` / `safe_dump()` mask it.
* Logs carry `proxy_id` and `telegram_message_id`. No URLs, no secrets, no token.
* The Bot API path contains the token; log redaction already masks `/bot<id>:<token>/` (D-008).

## Limitations

* Crash after Telegram accepted a message but before the audit row commits can
  produce one duplicate post. The unique success index then stops further repeats.
* Fake-TLS is never posted (Telethon cannot verify it, D-044).
* This is not a content bot: no Qwen, no captions beyond the standard template.
