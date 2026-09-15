# mtproto-platform

Empirical discovery, validation, measurement and scoring of public **MTProto**
proxies.

**Core hypothesis of the MVP:**

> Can we continuously discover, validate, measure and score public MTProto
> proxies well enough to identify high-quality usable candidates?

The MVP is deliberately small and reliable: three independent Python processes
coordinated only through PostgreSQL. No Redis, no Celery, no FastAPI, no
Kubernetes, no message broker. MTProto only — no SOCKS5, HTTP, VLESS, VMess,
Trojan, Xray or Shadowsocks.

---

## ⚠️ Current status: Task 001 complete — foundation only

**Nothing in this repository discovers, tests or scores a proxy yet.**

The three workers start, configure structured logging, handle signals, run a
heartbeat loop and shut down cleanly. Their ticks are **placeholders** and say so
in every log line (`implemented=False`). There is no database layer, no parser,
no tester and no scorer.

This is enforced mechanically, not by convention — `tests/test_workers.py` parses
each worker's AST and fails if it imports `telethon`, `sqlalchemy`, `asyncpg` or
`socket`, and asserts the placeholder flag is present in the rendered output.

**No proxy has been tested from this environment. No latency, success-rate or
uptime figure in this repository was measured against a real proxy.** See
[`spike/AUDIT.md`](spike/AUDIT.md).

### Task progress

| Task | Scope | Status |
|---|---|---|
| 001 | Foundation: config, logging, lifecycle, worker entrypoints, tests | ✅ **complete** |
| 002 | PostgreSQL layer: 5 models, indexes, Alembic | ⬜ not started |
| 003 | MTProto link parsing, normalisation, fingerprinting | ⬜ not started |
| 004 | Discovery engine + source abstraction | ⬜ not started |
| 005 | Telethon MTProto tester (3 phases) | ⬜ not started — **read `spike/AUDIT.md` §4 first** |
| 006–009 | Tester worker, observations, scoring, scorer worker | ⬜ not started |
| 010–012 | Reporting, Telegram publishing, AI content | ⬜ not started |
| 013–025 | Config expansion, concurrency, tests, security, infra, acceptance | ⬜ not started |

---

## Architecture

```
                        Managed PostgreSQL
                        /       |       \
                       /        |        \
                      /         |         \
             Discovery       Tester      Scorer
               worker        worker      worker
                 │              │           │
                 └──────────────┴───────────┘
              three INDEPENDENT OS processes
        coordinated only by SQL (FOR UPDATE SKIP LOCKED)
```

A crash, `kill -9` or OOM in one worker must not affect the others. They share no
Python state and are never merged into one asyncio application. Verified: see
[D-005](docs/DECISION_LOG.md).

```
src/
├── core/                    # shared infrastructure
│   ├── config.py            # Pydantic Settings; SecretStr for credentials
│   ├── logger.py            # structlog + mandatory secret redaction
│   └── lifecycle.py         # signals, graceful shutdown, the worker loop
├── modules/                 # domain logic (populated by Tasks 003–012)
└── workers/
    ├── discovery.py         # Process A — placeholder
    ├── tester.py            # Process B — placeholder
    └── scorer.py            # Process C — placeholder

tests/                       # 162 unit tests; no network, no database
spike/                       # protocol engine evaluation + its audit
docs/DECISION_LOG.md         # every constraining decision, with evidence
```

---

## Quick start

Requires [uv](https://docs.astral.sh/uv/) and Python 3.11+.

```bash
uv sync                        # create .venv and install everything
cp .env.example .env           # optional; development defaults already work

uv run pytest                  # 162 passed
uv run ruff check .            # All checks passed
uv run ruff format --check .   # 17 files already formatted
uv run mypy .                  # Success: no issues found in 15 source files
```

### Run the workers

Each is a separate process. Start them in three terminals:

```bash
uv run mtproto-discovery
uv run mtproto-tester
uv run mtproto-scorer
```

or as modules: `uv run python -m workers.tester`.

Stop one with `Ctrl-C` or `kill -TERM <pid>` — it finishes the current tick, logs
`worker_stopped` and exits `0`. Send the signal **twice** to force an immediate
exit (`130`). The other two workers are unaffected either way.

> **Deployment note.** `uv run <script>` inserts a wrapper process: `kill -9` on
> that PID orphans the real worker. Supervisors must exec the virtualenv binary
> directly (`.venv/bin/mtproto-tester`). See [D-012](docs/DECISION_LOG.md).

### Example output

Development renders human-readable console logs; `ENV=staging|production` (or
`LOG_FORMAT=json`) renders one JSON object per line:

```json
{"event": "tester_tick", "implemented": false, "level": "info",
 "logger": "worker.tester-worker", "pending_tasks": ["task-005-mtproto-tester",
 "task-006-tester-worker"], "proxies_claimed": 0, "run_id": "04cd4483e41b",
 "tests_failed": 0, "tests_started": 0, "tests_success": 0, "timeouts": 0,
 "timestamp": "2026-09-15T14:51:17.579211Z", "worker": "tester-worker"}
```

Every record carries `worker`, `event`, `run_id` and `timestamp`.

---

## Configuration

All settings live in [`src/core/config.py`](src/core/config.py); the documented
surface is [`.env.example`](.env.example). Environment variables are
case-insensitive and a `.env` file is read automatically.

| Variable | Default | Notes |
|---|---|---|
| `ENV` | `development` | `production` refuses to boot on dev defaults |
| `LOG_LEVEL` | `INFO` | application log level |
| `THIRD_PARTY_LOG_LEVEL` | `WARNING` | asyncio / SQLAlchemy / Telethon / asyncpg |
| `LOG_FORMAT` | *(derived)* | `console` in development, `json` otherwise |
| `DATABASE_URL` | local dev DSN | `postgresql://` is normalised to `postgresql+asyncpg://` |
| `DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, `DB_POOL_TIMEOUT_SECONDS`, `DB_POOL_RECYCLE_SECONDS`, `DB_ECHO` | see `.env.example` | `DB_ECHO=true` is rejected in production |
| `SHUTDOWN_GRACE_SECONDS` | `10` | |
| `HEARTBEAT_INTERVAL_SECONDS` | `60` | `0` disables heartbeats |
| `WORKER_POLL_INTERVAL_SECONDS` | `5` | idle delay between ticks |
| `WORKER_ERROR_BACKOFF_SECONDS`, `WORKER_MAX_ERROR_BACKOFF_SECONDS` | `2`, `60` | exponential, capped |

Tester timeouts, scoring interval, Telegram and Qwen credentials are **not**
defined yet — they arrive with Tasks 005, 009, 011 and 013.

### Secrets

Credentials are typed `SecretStr`, so `repr(Settings)` cannot leak the database
password. Logging adds a redaction processor that masks credential-shaped keys
**and** scrubs `secret=…` parameters, DSN passwords and `/bot<token>/` URLs from
any string — including inside exception tracebacks. See
[D-008](docs/DECISION_LOG.md). `.env` is git-ignored; `.env.example` is not.

---

## The protocol engine, and why the spike report is not trusted

The original spike recommended **Telethon** over Pyrogram. That recommendation
stands. Its stated *evidence* does not:

* `spike/spike_pyrogram.py` is a **non-executable stub** — empty `TEST_CASES`,
  `check_tcp()` body is `pass`, no `main()`. It cannot produce any result.
* `TelegramClient.connect()` is annotated **`-> None`** in Telethon 1.34.0 *and*
  1.45.0 and never returns `True`. The spike's `if connected:` was therefore
  **always False**: a genuinely successful connection would have been logged as
  `LIBRARY_ERROR`.
* All four spike fixtures use **`ee` fake-TLS secrets**, which the pinned
  **Telethon 1.34.0 rejects with `ValueError` before opening a socket**. Support
  arrived in **1.35.0** via `TcpMTProxy.normalize_secret`.
* The spike wrote **`.session` SQLite files to disk** while claiming an in-memory
  session.
* No results file was ever committed.

Full analysis, with the parts that *are* verified and nine binding constraints
for the tester: **[`spike/AUDIT.md`](spike/AUDIT.md)**.

Re-derive the facts yourself — no network, no credentials needed:

```bash
uv run --with telethon==1.34.0 python spike/verify_telethon_contract.py
uv run --with telethon          python spike/verify_telethon_contract.py
```

Committed output lives in [`spike/evidence/`](spike/evidence/).

### The largest open risk

Telethon ≥ 1.35.0 accepts `ee` secrets only by truncating them to 16 bytes and
**discarding the SNI domain**; `MTProxyIO` implements no TLS ClientHello. Since
most public MTProto proxies publish `ee` fake-TLS secrets, whether Telethon can
actually handshake with them is **empirically unverified**. It cannot be settled
by reading source — it needs a live test against a real proxy (Task 005/016).
Until then, no such claim is made anywhere in this repository.

---

## Development

```bash
uv run pytest                       # unit tests only: no network, no database
uv run pytest -m integration        # needs a local PostgreSQL (Task 002)
uv run pytest -m live               # real network I/O — never in CI
uv run pytest -m stress             # resource/leak stress test (Task 017)

uv run ruff check .
uv run ruff format .
uv run mypy .
```

`--strict-markers` makes a typo'd marker a hard error. Typing is strict:
`disallow_untyped_defs`, `warn_return_any`, `warn_unreachable`,
`disallow_untyped_decorators`, `strict_equality`.

Work lands as one logical commit per task (`task-002-database`,
`task-003-parser`, …), each leaving all three gates green.

---

## License

MIT
