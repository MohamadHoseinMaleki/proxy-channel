# mtproto-platform

Empirical discovery, validation, measurement and scoring of public **MTProto**
proxies.

**Core hypothesis of the MVP:**

> Can we continuously discover, validate, measure and score public MTProto
> proxies well enough to identify high-quality usable candidates?

The MVP is deliberately small and reliable: three independent worker processes
coordinated only through PostgreSQL, plus a read-only ranking HTTP process.
No Redis, no Celery, no Kubernetes, no message broker. MTProto only — no
SOCKS5, HTTP proxies, VLESS, VMess, Trojan, Xray or Shadowsocks.

---

## ⚠️ Current status: Tasks 001–008 complete

Discovery, testing, deterministic scoring, ranking, and a **read-only HTTP
API** are implemented. All three tick workers do real work against PostgreSQL
when configured. HTTP is an adapter over `RankingService.list_top` (D-041);
it does not rediscover, retest, or rescore. Discovery sources default to
empty (`DISCOVERY_SOURCES=`) so an unconfigured worker ticks honestly.

**No live public proxy was measured in this environment.** Unit and integration
scores are computed from persisted (often synthetic) observations. See
[`spike/AUDIT.md`](spike/AUDIT.md) and [`docs/SCORING.md`](docs/SCORING.md).

### Task progress

| Task | Scope | Status |
|---|---|---|
| 001 | Foundation: config, logging, lifecycle, worker entrypoints, tests | ✅ **complete** |
| 002 | PostgreSQL layer: 5 models, indexes, Alembic | ✅ **complete** |
| 003 | MTProto link parsing, normalisation, discovery layer | ✅ **complete** |
| 004 | Telethon MTProto tester (3 phases, `help.getConfig`) | ✅ **complete** |
| 005 | Deterministic scoring engine + scorer worker | ✅ **complete** |
| 006 | Ranking & serving layer over latest `ProxyScore` | ✅ **complete** |
| 007 | Read-only ranking HTTP transport (FastAPI + Uvicorn) | ✅ **complete** |
| 008 | Production discovery worker, upsert, SSRF/HTTP hardening | ✅ **complete** |
| 009 | Remaining tester/scoring operational work as originally numbered | superseded by 004–005 where overlapping |
| 010–012 | Reporting, Telegram publishing, AI content | ⬜ not started |
| 013–025 | Config expansion, concurrency, tests, security, infra, acceptance | ⬜ not started |

---

## Architecture

```
                        Managed PostgreSQL
                   /      |       |       \
                  /       |       |        \
           Discovery   Tester  Scorer   ranking API
             worker    worker  worker   (mtproto-api)
                 │        │       │          │
                 └────────┴───────┴──────────┘
        independent OS processes; workers coordinate
        only by SQL (FOR UPDATE SKIP LOCKED). The API
        is read-only and never claims rows.
```

A crash, `kill -9` or OOM in one worker must not affect the others. They share no
Python state and are never merged into one asyncio application. Verified: see
[D-005](docs/DECISION_LOG.md).

```
src/
├── core/                    # shared infrastructure
│   ├── config.py            # Pydantic Settings; SecretStr for credentials
│   ├── logger.py            # structlog + mandatory secret redaction
│   ├── lifecycle.py         # signals, graceful shutdown, the worker loop
│   ├── identity.py          # proxy fingerprinting, normalisation, ProxySecret
│   ├── models.py            # SQLAlchemy 2.x ORM: 4 tables, constraints, indexes
│   └── database.py          # async engine, session_scope, teardown
├── modules/                 # domain logic (populated by Tasks 003–012)
│   ├── scheduling.py        # FOR UPDATE SKIP LOCKED claim primitive
│   ├── ranking/             # latest-score ranking, secret-safe listings
│   └── api/                 # FastAPI adapter over RankingService
└── workers/
    ├── discovery.py         # Process A — source fetch + upsert (D-042)
    ├── tester.py            # Process B — MTProto probe + observations
    ├── scorer.py            # Process C — deterministic ProxyScore snapshots
    └── api.py               # Process D — uvicorn ranking HTTP (not a tick loop)

alembic/                     # async migrations; no DSN in alembic.ini
scripts/dev_pg.py            # local PostgreSQL without Docker (pgserver, ad hoc)
infra/docker/                # optional compose file, for people who run Docker
tests/                       # 782 unit tests; no network, no database
tests/integration/           # 195 tests against a real PostgreSQL 16
spike/                       # protocol engine evaluation + its audit
docs/DATABASE.md             # schema, identity, secrets, claiming, indexes
docs/DISCOVERY.md            # parsing, normalization, SSRF, persistence
docs/TESTER.md               # three-phase probe, help.getConfig, Fake-TLS limit
docs/SCORING.md              # v1 formula, confidence, recency, limitations
docs/RANKING.md              # serving contract, eligibility, freshness, order
docs/API.md                  # HTTP transport, health/ready, secret-free errors
docs/DECISION_LOG.md         # every constraining decision, with evidence
```

---

## Quick start

Requires [uv](https://docs.astral.sh/uv/) and Python 3.11+.

```bash
uv sync                        # create .venv and install everything
cp .env.example .env           # optional; development defaults already work

uv run pytest                  # 817 passed, 198 skipped (no database)
uv run ruff check .            # All checks passed
uv run ruff format --check .   # files already formatted
uv run mypy .                  # Success: no issues found in 82 source files
```

To also run the 189 integration tests, provision a local PostgreSQL — Docker is
**not** required:

```bash
uv run --with pgserver python scripts/dev_pg.py run -- uv run alembic upgrade head
uv run --with pgserver python scripts/dev_pg.py run -- uv run pytest
                               # 1015 passed
```

`pgserver` is fetched ad hoc and is never added to the project dependencies. Any
other PostgreSQL works too — point `DATABASE_URL` at it and skip the script. See
[docs/DATABASE.md](docs/DATABASE.md).

If you would rather run a container, `infra/docker/docker-compose.yml` is there
for that, and **Docker is still not required by anything**:

```bash
POSTGRES_PASSWORD=... docker compose -f infra/docker/docker-compose.yml up -d
```

It binds to `127.0.0.1` only and refuses to start without an explicit password —
no credential is committed. See [D-034](docs/DECISION_LOG.md).

**Platform notes.** The 641 unit tests are cross-platform and verified on both
Linux and Windows — no database, no network, no filesystem assumptions. The 167
integration tests need a real PostgreSQL; without one they **skip with a
message**, they never fail. `scripts/dev_pg.py` has only been exercised on Linux,
so on Windows point `DATABASE_URL` at a PostgreSQL you installed yourself:

```powershell
$env:DATABASE_URL = "postgresql+asyncpg://user:pass@localhost:5432/mtproto"
uv run alembic upgrade head
uv run pytest -m integration
```

See [D-033](docs/DECISION_LOG.md) for the two Windows-only bugs this uncovered
and how they are now reproduced on Linux so CI catches them.

### Run the workers

Each is a separate process. Start them in four terminals:

```bash
uv run mtproto-discovery
uv run mtproto-tester
uv run mtproto-scorer
uv run mtproto-api            # 127.0.0.1:8080 — GET /healthz /readyz /v1/proxies
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
| `DB_POOL_PRE_PING` | `true` | validates pooled connections; a stale one would otherwise surface as an `InterfaceError` mid-loop |
| `DB_HIDE_PARAMETERS` | `true` | keeps bind values out of exception text — this schema stores a secret in plaintext. Debug only |
| `TEST_DATABASE_URL` | *(derived)* | `DATABASE_URL` with `_test` suffixed. **Mandatory** under `ENV=production`, where derivation is refused |
| `SHUTDOWN_GRACE_SECONDS` | `10` | |
| `HEARTBEAT_INTERVAL_SECONDS` | `60` | `0` disables heartbeats |
| `WORKER_POLL_INTERVAL_SECONDS` | `5` | idle delay between ticks |
| `WORKER_ERROR_BACKOFF_SECONDS`, `WORKER_MAX_ERROR_BACKOFF_SECONDS` | `2`, `60` | exponential, capped |
| `DISCOVERY_SOURCES` | *(empty)* | `telegram:<channel>;http:<url>`; invalid entries skipped |
| `DISCOVERY_TIMEOUT_SECONDS`, `DISCOVERY_CONNECT_TIMEOUT_SECONDS` | `15`, `5` | per-request HTTP bounds |
| `DISCOVERY_MAX_RESPONSE_BYTES`, `DISCOVERY_MAX_REDIRECTS` | `2 MiB`, `3` | body / redirect caps |
| `DISCOVERY_CONCURRENCY` | `3` | bounded source fetches per tick |

Telegram publisher and Qwen credentials are **not** defined yet — they arrive
with later reporting tasks.

### Secrets

Credentials are typed `SecretStr`, so `repr(Settings)` cannot leak the database
password. Logging adds a redaction processor that masks credential-shaped keys
**and** scrubs `secret=…` parameters, DSN passwords, `/bot<token>/` URLs and any
bare run of ≥32 hex characters from any string — including inside exception
tracebacks and pytest failure output. See [D-008](docs/DECISION_LOG.md). `.env` is
git-ignored; `.env.example` is not.

MTProto proxy secrets are stored in plaintext (the tester needs the real value to
connect) but wrapped in a `ProxySecret` type whose `str()`, `repr()` and f-string
renderings are all masked, and which `json.dumps` refuses to serialise. The
plaintext requires an explicit `.reveal()`. Two independent leak paths into
database error text were found and closed — see
[D-020](docs/DECISION_LOG.md), [D-027](docs/DECISION_LOG.md) and
[docs/DATABASE.md](docs/DATABASE.md#secrets).

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
