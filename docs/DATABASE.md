# Database layer

Task 002. PostgreSQL is the platform's **only** coordination mechanism — there is
no Redis, no Celery, no queue. Three independent OS processes (discovery, tester,
scoring) share work through row locks. Everything below exists to make that safe.

> Nothing in this document is aspirational. Every constraint, index and query
> shape described here is created by `alembic/versions/0001_*.py` and enforced by
> a test that really runs against PostgreSQL 16.

---

## Contents

- [Quick start](#quick-start)
- [Schema overview](#schema-overview)
- [Identity: what makes two proxies the same](#identity-what-makes-two-proxies-the-same)
- [Secrets](#secrets)
- [Work claiming](#work-claiming)
- [Indexes](#indexes)
- [Constraints](#constraints)
- [Migrations](#migrations)
- [Testing](#testing)
- [Deliberately absent](#deliberately-absent)

---

## Quick start

Docker is **not** required. `scripts/dev_pg.py` provisions a real PostgreSQL from
a self-contained wheel, fetched ad hoc so it never becomes a project dependency:

```bash
# start a cluster and create the mtproto / mtproto_test databases
uv run --with pgserver python scripts/dev_pg.py up

# run the integration suite with DATABASE_URL/TEST_DATABASE_URL already exported
uv run --with pgserver python scripts/dev_pg.py run -- uv run pytest -m integration

# apply migrations
uv run --with pgserver python scripts/dev_pg.py run -- uv run alembic upgrade head

# stop it (add --delete to remove the data directory)
uv run --with pgserver python scripts/dev_pg.py down
```

The cluster lives in `$XDG_CACHE_HOME/mtproto-platform/devpg`, outside the
repository, because a PostgreSQL data directory is thousands of small files that
do not belong in a working tree. Override with `MTPROTO_DEVPG_DATA` or `--pgdata`.

Any other PostgreSQL works identically — point `DATABASE_URL` at Supabase, Neon,
RDS or a system package and never run that script.

Without a reachable server the integration suite **skips with instructions**
rather than failing, so `uv run pytest` stays green everywhere.

---

## Schema overview

Five tables, five responsibilities. They have different write rates, retention
needs and deletion semantics, which is why they are separate (D-017, D-047).

```
proxies ──┬──< proxy_discoveries    ON DELETE CASCADE
          ├──< proxy_observations   ON DELETE RESTRICT
          ├──< proxy_scores         ON DELETE CASCADE
          └──< proxy_publications   ON DELETE RESTRICT
```

| Table | Role | Written by | Lifetime |
|---|---|---|---|
| `proxies` | **identity** — one row per distinct configuration | discovery | effectively permanent |
| `proxy_discoveries` | **provenance** — where/when an identity was sighted | discovery | append-only |
| `proxy_observations` | **measured behaviour** — one row per test attempt | tester | append-only; the asset |
| `proxy_scores` | **derived state** — versioned snapshots | scorer | append-only history |
| `proxy_publications` | **outbox** — one row per ``(proxy_id, channel_id)`` | publisher | lifecycle (D-048) |

### `proxies`

Identity plus the scheduling/lease columns the tester claims against.

| Column | Type | Notes |
|---|---|---|
| `id` | `BIGINT` PK | |
| `protocol` | `VARCHAR(16)` | default `mtproto`; **no CHECK** (D-018) |
| `server` | `VARCHAR(255)` | normalised; must be non-blank |
| `port` | `INTEGER` | 1–65535 |
| `secret` | `TEXT` | plaintext by design (D-020); length-bounded |
| `fingerprint` | `VARCHAR(64)` | **UNIQUE** — the identity key |
| `is_active` | `BOOLEAN` | the soft-delete / retirement switch |
| `created_at`, `updated_at`, `first_seen_at` | `TIMESTAMPTZ` | `DEFAULT now()` |
| `last_seen_at` | `TIMESTAMPTZ?` | refreshed by discovery on every sighting |
| `last_success_at`, `last_failure_at` | `TIMESTAMPTZ?` | |
| `next_test_at` | `TIMESTAMPTZ` | **NOT NULL** `DEFAULT now()` (D-023) |
| `test_lock_until` | `TIMESTAMPTZ?` | lease expiry — the crash-recovery mechanism |
| `test_attempts` | `INTEGER` | incremented server-side on every claim |
| `last_test_started_at` | `TIMESTAMPTZ?` | |
| `last_test_finished_at` | `TIMESTAMPTZ?` | the canonical "last tested at" |
| `last_error_category` | `VARCHAR(32)?` | denormalised, advisory only |

`last_seen_at` ("seen in a channel") and `last_test_finished_at` ("tested by us")
are unrelated facts and are deliberately separate columns.

### `proxy_observations`

One row per test attempt — **failures included**. Dropping failures would inflate
every score.

`success`, three separate latency phases (`tcp_connect_ms`, `mtproto_connect_ms`,
`total_latency_ms`), `error_category`, `error_message_safe`, plus `tester_version`
and `test_location`.

The three phases are separate measurements because TCP success says nothing about
MTProto validity. `tester_version` is mandatory in effect: without it a
methodology change is indistinguishable from a proxy behaviour change.
`test_location` exists because latency is meaningless without a vantage point.

### `proxy_scores`

An append-only **snapshot**, not an authoritative current value (D-022). `score`
plus `reliability_{1h,6h,24h}`, `latency_p{50,95}_ms`, `sample_count_{1h,6h,24h}`
and a mandatory `scoring_version`.

"Latest score per proxy" is a query, not a column:

```sql
SELECT DISTINCT ON (proxy_id) * FROM proxy_scores
ORDER BY proxy_id, calculated_at DESC;
```

---

## Identity: what makes two proxies the same

`fingerprint` is `sha256` over a versioned, unambiguously-separated payload
(D-019):

```
sha256("v1" \x1f protocol \x1f normalized_server \x1f port \x1f secret_bytes.hex())
```

```python
from core.identity import compute_fingerprint

fp = compute_fingerprint(server="Proxy.Example.COM", port=443, secret="ee...")
```

Three properties are load-bearing and each is pinned by a test:

1. **`\x1f` as the separator.** A naive `:` join makes
   `server="x:1", port=2, secret="y"` and `server="x", port=1, secret="2:y"` both
   render as `x:1:2:y` — one row instead of two, a candidate silently lost.
   `\x1f` (US) cannot appear in a host, port or secret.
2. **The secret is part of the hash.** Several distinct MTProto secrets routinely
   share one `server:port`; hashing the endpoint alone would merge them.
3. **Full secret bytes, not Telethon's 16.** Telethon ≥1.35 truncates because it
   cannot use the fake-TLS SNI domain. For *identity* the domain matters, so
   `ee<key>google.com` and `ee<key>telegram.org` are different proxies.

`normalize_server` strips whitespace, lowercases, removes IPv6 brackets and
removes a trailing root dot. It is **total** — it never raises and never resolves
DNS — because identity must be defined for every input, including ones Task 003's
parser will later reject.

Secrets are decoded hex-first with a base64 fallback, so both spellings of the
same bytes produce one fingerprint. Hex must win: a 32-character lowercase hex
string is *also* valid base64, and choosing the other order would silently
re-fingerprint every existing row.

---

## Secrets

Stored in plaintext, protected by type and scrubbing (D-020, D-027). There is no
at-rest encryption: the tester needs the real value to connect, and encryption
without a key-management strategy is theatre.

```python
# for secret = "ee" + "a1" * 15  (32 hex chars, the canonical 16-byte form)
proxy.secret  # ProxySecret — str()/repr()/f-string all yield "eea1...a1a1"
proxy.secret.reveal()  # the plaintext, for handing to the MTProto transport only
proxy.secret.masked  # "eea1...a1a1"
```

`ProxySecret` is deliberately **not** a `str` subclass — a subclass would leak
through `str.__format__` and through any `isinstance(x, str)` serialisation path.
`json.dumps` on it raises `TypeError` rather than emitting the value.

`SecretText` wraps every value read from the database, and a `@validates` hook
wraps every value *assigned* in Python. The second half matters: without it a
freshly constructed `Proxy` — the one most likely to be printed while debugging —
would hold a bare `str`.

### The two leak paths into error text

Found empirically. When a CHECK constraint rejects a row, PostgreSQL appends
`DETAIL: Failing row contains (...)` and echoes **every column**, secret included.
Both paths are closed:

| Path | Defence |
|---|---|
| SQLAlchemy appends `[parameters: (...)]` to DBAPI errors | `hide_parameters=True` on the engine (`DB_HIDE_PARAMETERS`) |
| PostgreSQL echoes the failing row in `DETAIL` | shape-based scrubber: any bare run of ≥32 hex chars is masked |

`hide_parameters` does **not** cover the second — only the scrubber does. Use
`safe_error_message()` for anything you intend to log or persist:

```python
from core.logger import safe_error_message

observation.error_message_safe = safe_error_message(exc)
# -> "IntegrityError: ... violates check constraint \"ck_proxies_port_range\"
#     DETAIL: Failing row contains (2, mtproto, , 0, ***REDACTED***,
#     ***REDACTED***, t, 2026-09-15 16:23:53.515655+00, ...)"
#
# Verbatim from PostgreSQL 16.2: both the secret and the 64-char fingerprint are
# masked, while the constraint name and host stay readable.
```

The accepted cost: 64-character fingerprints are masked in error text too. A
fingerprint is derivable from the row and rarely belongs in an error message; a
secret in a log aggregator is not recoverable. The **raw** SQLAlchemy exception
still contains the secret, so never log or store it directly.

---

## Work claiming

`modules.scheduling.claim_due_proxies` is the primitive that replaces a message
broker (D-024). One statement, one round trip:

```sql
WITH claim_candidates AS (
  SELECT id FROM proxies
  WHERE is_active AND next_test_at <= :now
    AND (test_lock_until IS NULL OR test_lock_until < :now)
  ORDER BY next_test_at, id LIMIT :limit
  FOR UPDATE SKIP LOCKED
)
UPDATE proxies SET test_lock_until = :lease, last_test_started_at = :now,
                   test_attempts = test_attempts + 1
FROM claim_candidates WHERE proxies.id = claim_candidates.id
RETURNING *;
```

```python
async with db.session_scope() as session:  # commits on exit
    proxies = await claim_due_proxies(session, limit=25)
results = await test_all(proxies)  # NO transaction held
```

Three rules:

1. **`SKIP LOCKED`, never plain `FOR UPDATE`.** Plain `FOR UPDATE` makes a second
   worker *block* until the first commits — if the first is mid-handshake, the
   second idles for seconds. `SKIP LOCKED` makes it take different rows.
2. **Never hold a transaction across network I/O.** Claim, commit, test, then
   write results in a second transaction. `RETURNING *` means no follow-up read.
3. **The lease makes a crash self-healing.** A worker killed with `kill -9` never
   clears its claim; `test_lock_until` expires and the row becomes claimable
   again. Without it, one crash strands proxies permanently.

`test_attempts` increments **server-side** (`SET x = x + 1`), so concurrent claims
cannot lose an update. It is the trail left by a proxy that keeps killing workers.

### Two ordering guarantees, not one

`UPDATE … FROM claim_candidates … RETURNING` does **not** preserve the CTE's
`ORDER BY` — PostgreSQL returns rows in join order, which tracks heap layout.
Verified on PostgreSQL 16.2: six due rows inserted scrambled, `limit=3`, and the
three *oldest* were correctly leased but came back as `p1, p0, p2`.

| Guarantee | Enforced by | Status |
|---|---|---|
| The oldest N rows are the ones leased (no starvation) | the CTE's `ORDER BY … LIMIT` in SQL | always held |
| The returned list is sorted by due time | `claimed.sort(...)` in Python | needed the fix |

It matters because a tester working the batch in order should reach the
most-overdue proxies first: if it is killed mid-batch, the most-starved rows were
already done. This was caught by a test that had been passing *by luck* while heap
layout happened to coincide with due order.

### Hazard: rollback expires ORM attributes

`expire_on_commit=False` makes the commit path safe — read attributes freely after
committing. A **rollback** still expires everything, and re-reading one fires an
implicit lazy refresh that cannot run under asyncio (`MissingGreenlet`). Pull
`id`/`server`/`port`/`secret` out of a claimed proxy immediately.

Relationships are all `lazy="raise"` for the same reason: an implicit load under
asyncio is surprise I/O, so it fails loudly instead. Use `selectinload` explicitly.

Verified with four independent engines (separate pools, standing in for separate
processes) claiming 40 rows concurrently: every row claimed exactly once, no
blocking, no deadlock.

---

## Indexes

Each is tied to a concrete query. An index that serves no query is write
amplification.

| Index | Definition | Serves |
|---|---|---|
| `uq_proxies_fingerprint` | `UNIQUE (fingerprint)` | identity — 10,000 sightings → 1 row |
| `ix_proxies_due` | `(next_test_at) WHERE is_active` | the claim query |
| `ix_proxies_last_success_at` | `(last_success_at)` | "what worked in the last 6h" |
| `ix_proxies_server` | `(server)` | debugging, abuse investigation |
| `ix_proxy_observations_proxy_id_observed_at` | `(proxy_id, observed_at)` | scoring windows — the hottest index |
| `ix_proxy_observations_observed_at` | `(observed_at)` | retention sweeps |
| `ix_proxy_observations_success_observed_at` | `(observed_at) WHERE success` | latency aggregation |
| `ix_proxy_scores_proxy_id_calculated_at` | `(proxy_id, calculated_at)` | latest score per proxy |
| `ix_proxy_discoveries_*` | `(proxy_id)`, `(discovered_at)`, `(source_type)` | provenance lookups |
| `uq_proxy_publications_proxy_channel` | `UNIQUE (proxy_id, channel_id)` | one outbox row per proxy per channel |
| `ix_proxy_publications_due` | `(next_attempt_at, id) WHERE status = 'pending'` | publisher claim |
| `ix_proxy_publications_sending_lease` | `(lease_until) WHERE status = 'sending'` | stale-lease recovery |
| `ix_proxy_publications_proxy_id` | `(proxy_id)` | audit by identity |
| `ix_proxy_publications_created_at` | `(created_at)` | time-range sweeps |

Notes worth knowing before changing any of them:

- **`ix_proxies_due` is partial** on `is_active`, so retiring 10,000 proxies
  shrinks the index rather than leaving dead rows in it.
- **The lease predicate cannot be indexed at all.** Partial index predicates must
  be `IMMUTABLE` and `now()` is only `STABLE`, so
  `test_lock_until < now()` stays a residual filter. It is cheap because the index
  already narrowed the rows.
- **`next_test_at` is NOT NULL** precisely so this index needs no `NULLS FIRST`
  variant (D-023). A plain ASC btree stores NULLs *last*, so a nullable column
  would force an explicit one — and `postgresql_nulls_first` is not a valid
  SQLAlchemy `Index` kwarg, so the obvious way to express it does not exist.
- **`(observed_at) WHERE success`** rather than the suggested
  `(success, observed_at)` composite: a boolean leading column roughly doubles the
  index while serving exactly the same query (D-025).
- **`ix_proxy_observations_observed_at`** is not redundant with the composite:
  `proxy_id` leads that one, so it cannot serve `DELETE WHERE observed_at < x`.
- The `proxy_id_calculated_at` index is plain ASC — PostgreSQL scans a btree
  backwards, so it also serves `ORDER BY calculated_at DESC`.

Both partial-index claims are verified with `EXPLAIN` under `enable_seqscan=off`,
including the discriminating direction: a query that drops `is_active` correctly
*cannot* use `ix_proxies_due`.

---

## Constraints

These do real work rather than duplicating Pydantic.

| Constraint | Why |
|---|---|
| `ck_proxies_port_range` | a port outside 1–65535 is not connectable |
| `ck_proxies_server_not_blank`, `ck_proxies_secret_length` | an unconnectable identity is not an identity |
| `ck_proxies_test_attempts_non_negative` | the counter must not wrap |
| `ck_proxy_observations_failure_needs_category` | a failure with no category is unusable for scoring |
| `ck_proxy_observations_*_non_negative` | negative latency is a broken tester |
| `ck_proxy_observations_error_message_bounded` | caps persisted error text at 500 chars |
| `ck_proxy_scores_score_range` | 0–100 |
| `ck_proxy_scores_reliability_*_range` | 0–100 |
| `ck_proxy_scores_reliability_*_needs_samples` | reliability must be **NULL, not zero**, with no samples |
| `ck_proxy_scores_p95_at_least_p50` | percentiles that violate their own ordering mean the scorer is broken |
| `ck_proxy_scores_samples_*_non_negative` | |
| `ck_proxy_scores_scoring_version_not_blank` | every score must be attributable to a formula |
| `ck_proxy_discoveries_source_name_not_blank` | provenance must name its source |

The `reliability_*_needs_samples` family is the most important: **"no data" and
"0% success" are different facts**, and conflating them is exactly how a
never-tested proxy acquires a score, or how 1/1 success ends up looking perfect.
It is enforced at the storage layer, independent of whatever the scorer computes.

Two guards can fire on the same mistake with different exceptions: `score` is
`NUMERIC(6,3)`, so `1000` overflows the *type* as a `DBAPIError` before the
`score <= 100` CHECK can raise `IntegrityError`. Both are pinned in tests so
nobody "fixes" one and assumes the other covers it.

---

## Migrations

Alembic runs **async** against asyncpg. `alembic.ini` contains no DSN — the URL is
resolved in `alembic/env.py` from `core.config.Settings`, so no credential is ever
tracked in git and there is no second source of truth to drift.

```bash
uv run alembic upgrade head              # uses DATABASE_URL / .env
uv run alembic -x test=1 upgrade head    # uses TEST_DATABASE_URL / resolved_test_url
uv run alembic -x url=... upgrade head   # explicit DSN, highest precedence
uv run alembic upgrade head --sql        # emit SQL for review, no connection
uv run alembic check                     # fail if models and DB have drifted
uv run alembic downgrade base
```

`env.py` deliberately does **not** call `fileConfig()`: that would replace the root
handlers installed by `core.logger` and migration output would stop being
structured JSON like the rest of the platform.

Autogenerate is configured with `compare_type=True` and
`compare_server_default=True`. Without them it silently produces incomplete
migrations, and a partial migration that applies cleanly is worse than one that
fails.

### Writing a migration

```bash
uv run --with pgserver python scripts/dev_pg.py run -- \
    uv run alembic revision --autogenerate -m "add per-host throttling"
```

Then **read it**. Autogenerate is a first draft: it cannot know that a new index
should be partial, that a column needs a backfill, or that a constraint needs a
data-cleaning step first. Post-write hooks run `ruff format` and `ruff check
--fix` automatically.

Migrations should stay decoupled from application code. `0001` writes `sa.Text()`
rather than `core.models.SecretText()` — `SecretText.impl` *is* `Text`, so the DDL
is identical, `alembic check` reports no drift, and a frozen migration does not
depend on an import that could change underneath it.

---

## Testing

| Suite | Database | Count |
|---|---|---|
| `tests/` (unit) | none — DDL compiled with the PostgreSQL dialect | 493 |
| `tests/integration/` | real PostgreSQL 16.2 | 161 |

```bash
uv run pytest                                          # unit + integration (skips if no DB)
uv run pytest --ignore=tests/integration               # unit only
uv run pytest -m integration                           # integration only
uv run --with pgserver python scripts/dev_pg.py run -- uv run pytest -m integration
```

The split is deliberate:

- **Unit** asserts *declared intent* — naming convention, column types, index
  predicates, compiled DDL. Runs in ~1s with no server.
- **Integration** asserts *enforced behaviour* — that PostgreSQL actually accepts
  and rejects what we claimed. A CHECK constraint, an `ON DELETE` action and a
  UNIQUE index are database behaviour; compiling DDL cannot prove enforcement.

Integration specifics (D-031):

- Tables are `TRUNCATE … RESTART IDENTITY CASCADE`d per test, **not** rolled back
  in a savepoint — a savepoint cannot exercise `SKIP LOCKED` across concurrent
  sessions, which is the whole point.
- The migration lifecycle module creates and drops its own `<testdb>_lifecycle`
  database, because `downgrade base` drops every table.
- Fixtures run the **real Alembic CLI**, not `create_all`. Using `create_all`
  would let the migration rot while every test still passed.
- `alembic check` is asserted empty after upgrade — that is what catches a model
  change never turned into a migration.
- A `pytest_runtest_makereport` hook scrubs credentials out of failure output,
  because pytest prints fixture values verbatim and these fixtures hold DSNs.
- Tests run as a **non-superuser** role, so permission bugs surface here rather
  than in production.

The URL is captured at import time, before the root `conftest.py`'s autouse
fixture strips `DATABASE_URL` from the environment for unit-test hermeticity.

---

## Deliberately absent

| Absent | Why |
|---|---|
| Repository / unit-of-work layer | Queries live with the code that owns them, so the SQL stays visible. A `Database` that only manages connections is the whole abstraction. |
| Module-level engine singleton | Four processes each build their own; a global invites binding a pool to an event loop a later `asyncio.run()` replaced (D-028). |
| `ON UPDATE` trigger for `updated_at` | `onupdate=func.now()` is applied by SQLAlchemy. Every writer in this platform goes through SQLAlchemy, so a trigger would be dead weight that also stamps ad-hoc maintenance fixes. Documented and pinned by a test. |
| CHECK on `protocol` | Extensibility; the fingerprint already covers correctness (D-018). |
| Native enum for `error_category` | The taxonomy will evolve; a native enum makes every new category a migration (D-026). |
| `WRONG_SECRET` category | MTProxy drops bad-secret payloads without RST or error, so it is indistinguishable from a blackholed endpoint. Claiming to detect it would fabricate a diagnosis. |
| At-rest secret encryption | No key-management strategy exists; a half-solution creates false confidence (D-020). |
| Read replicas, partitioning, materialised views | Not needed at MVP scale. `proxy_observations` is the table that will eventually need partitioning by `observed_at`; the retention index is already in place for it. |
| Redis / Celery / Docker | D-016, D-030. |

---

## Follow-ups this creates

| Item | When |
|---|---|
| Retention/pruning job for `proxy_observations` | before sustained production load |
| Partition `proxy_observations` by `observed_at` | when the table passes ~10M rows |
| Key management, if secrets must ever be encrypted at rest | before any multi-tenant or hosted deployment |
| Per-host throttling (uses `ix_proxies_server`) | Task 006+ |
| Counter metrics (`proxies_discovered`, `tests_success`, …) | Task 022 |
