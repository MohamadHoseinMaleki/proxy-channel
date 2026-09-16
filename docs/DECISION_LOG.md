# Decision Log

Every entry records a decision that constrains later work, why it was made, and
what evidence supports it. Newest first within a task. Superseded decisions are
kept with their replacement noted — this log is the project's memory.

Format: **ID · Decision · Context · Evidence · Consequences**

---

## Task 008 — Production discovery worker

### D-042 · Wire `mtproto-discovery`; always-upsert; pin DNS; do not reset tester schedule

**Context.** Task 003 shipped parsing, SSRF-on-hop, fingerprinting, and append-only provenance, but `workers.discovery` remained a Task 001 placeholder (`implemented=False`). `persist_candidate` did SELECT-then-INSERT, so a concurrent miss could report `is_new=True` on the conflict path. HTTP built a new `httpx.AsyncClient` per hop, left `HTTPStatusError` unwrapped, and had a DNS-rebinding window between `validate_ssrf_url` and connect. There were no `DISCOVERY_*` settings.

**Decision.**

1. **Worker.** Same lifecycle as tester/scorer: `Database.from_settings`, unreachable DB warns and returns, fetch **outside** any transaction, persist, `implemented=True`, dispose client + engine. Empty `DISCOVERY_SOURCES` is an honest zero-counter tick, not fake work.
2. **Catalog.** `DISCOVERY_SOURCES` is semicolon-separated `telegram:<channel>` / `http:<url>` (default empty). Telegram channel names are identifiers (`[A-Za-z][A-Za-z0-9_]{4,31}`); path / `../` injection into `https://t.me/s/{channel}` is rejected. Invalid entries are skipped per source.
3. **Upsert.** Single `INSERT … ON CONFLICT (fingerprint) DO UPDATE` of `last_seen_at` and `is_active=True` only. `is_new` is `RETURNING (xmax = 0)`. Tester columns (`next_test_at`, `test_lock_*`, `last_test_*`, `test_attempts`) are never written on conflict. Naive datetimes raise.
4. **HTTP.** One worker-owned `SsrfSafeHttpClient`, `aclose` in `finally`. Status/timeout/transport errors wrap as `HttpFetchError` via `safe_error_message`. Resolved public IPs are pinned with a `ContextVar` + `getaddrinfo` wrapper for the request. Mixed public+private DNS is rejected. SSRF is not weakened; loopback is not allowlisted.
5. **Isolation.** Bounded `DISCOVERY_CONCURRENCY` (default 3). One raising source is logged and counted; the tick continues. `CancelledError` propagates.
6. **Tests.** No public Telegram and no `127.0.0.1` HTTP fixture (SSRF would block it). Smoke is `RawTextSource` or `httpx.MockTransport`.
7. **Out of scope.** Scoring v1, ranking SQL, Redis, schema migration (none required).

**Evidence.** Unit tests cover catalog injection, SSRF redirects, status wrap, size, timeout, log redaction, cancel, harvest isolation, empty-source honesty. Integration tests cover concurrent upsert `is_new`, rediscovery vs tester schedule, naive timestamps, and RawText harvest.

**Consequences.** D-011 placeholder honesty no longer applies to discovery. D-035's upsert intent is the conflict path; the SELECT-then-INSERT implementation is gone. D-023 (`next_test_at` default now) still applies on **insert**; rediscovery must not make a recently tested proxy immediately due again.

---

## Task 007 — Read-only ranking HTTP transport

### D-041 · FastAPI + Uvicorn as a **separate** ranking adapter; D-016/D-040 HTTP ban is superseded for this process only

**Context.** Task 006 shipped `RankingService.list_top` with no web framework
(D-040), matching D-016 ("No FastAPI"). Task 007 needs the smallest production
HTTP surface over that service. The ranking domain must not move into routes.

**Decision.**

1. **FastAPI + Uvicorn only.** No Flask, Django, Sanic, or aiohttp. `httpx` was
   already a project dependency; FastAPI/Uvicorn are added as runtime deps for
   this process.
2. **Separate OS process** (`mtproto-api` → `workers.api:main`). Not a fourth
   tick on `WorkerLifecycle`. Uvicorn owns SIGINT/SIGTERM; installing D-006
   handlers alongside it would race shutdown.
3. **Adapter only.** Routes call `RankingService.list_top(limit=…)`. No
   duplicate eligibility/order SQL. No public `as_of` — the service stamps
   `utcnow()`. Query `limit` is `1..100` (same bounds as ranking policy).
4. **Bind loopback by default** (`API_HOST=127.0.0.1`, `API_PORT=8080`). A
   public interface is an operator override, not the MVP.
5. **Ops vs ranking.** `GET /healthz` is liveness (no DB). `GET /readyz` is
   `Database.is_reachable` only — empty ranking is still ready; DB failure is
   `503` with no SQL/exception leak.
6. **Generic errors.** FastAPI `422` is rewritten to `400` `{"detail":"invalid
   request"}`. `500` is `{"detail":"internal error"}`. Bodies never echo
   rejected values, secrets, or tracebacks.
7. **No Redis, no rate-limit backend, no app-level ranking cache.** Optional
   `Cache-Control: public, max-age=30` on listings is a hint for a reverse
   proxy, not a store.
8. **Request IDs.** `x-request-id` echoed when it matches
   `[A-Za-z0-9._-]{1,64}`; otherwise minted. Oversized headers are discarded
   and never logged.

**Evidence.** Unit tests drive the ASGI app with `httpx.ASGITransport` and a
mocked database. Integration tests hit real PostgreSQL through the same app
factory. OpenAPI is asserted free of ORM models, secrets, fingerprints, and
`as_of`.

**Consequences.** D-016 and D-040 still forbid Redis/Celery/K8s and still
forbid putting ranking *logic* in a web framework. They no longer forbid this
HTTP adapter. Task 005 formula and Task 006 SQL are unchanged. Discovery,
tester, and scorer stay independent processes.

---

## Task 001 — Foundation

### D-001 · The repository did **not** contain the foundation described in the brief; Task 001 built it

**Context.** The task brief described an existing `src/core/{config,lifecycle,logger}.py`,
three worker entrypoints, four passing tests, `docs/`, `infra/`, `spike/` and a
root `pyproject.toml`. At commit `0b634fa` none of that existed.

**Evidence.** `git ls-files` at the start of the task returned exactly 11 files:

```
.gitignore  README.md  REPORT.md  requirements.txt
spike_pyrogram.py  spike_telethon.py
mtproto-platform/{.python-version,README.md,main.py,pyproject.toml,uv.lock}
```

`mtproto-platform/main.py` was `print("Hello from mtproto-platform!")`. There was
no `src/`, no `tests/`, no `docs/`, no `infra/`, no `alembic/`.

**Decision.** Task 001 was executed as *build the foundation to the described
shape*, not *audit an existing one*. This is recorded explicitly so the difference
between "audited" and "created" is never ambiguous.

**Consequences.** Nothing was rewritten — there was nothing to rewrite. The
pre-existing spike material was preserved (moved, not deleted) under `spike/`.

---

### D-002 · Project lives at the repository root; the `mtproto-platform/` scaffold was absorbed

**Context.** The scaffold nested a second project directory inside the repo.

**Decision.** Root-level `pyproject.toml` with a `src/` layout, matching the
target structure in the brief. `spike_telethon.py`, `spike_pyrogram.py`,
`REPORT.md` and `requirements.txt` moved into `spike/` via `git mv` (history
preserved). The nested scaffold's dependency intent (SQLAlchemy, asyncpg,
Alembic, pydantic-settings, structlog) was carried into the root `pyproject.toml`.

**Consequences.** One project, one lockfile, one virtualenv. `uv run pytest`,
`uv run ruff check .` and `uv run mypy .` all work from the repo root.

---

### D-003 · `src/` holds three top-level packages: `core`, `modules`, `workers`

**Context.** The brief specifies `src/core/config.py`, `src/modules/parsers.py`,
`src/workers/discovery.py`.

**Decision.** Three sibling top-level packages rather than one
`src/mtproto_platform/` namespace. Wired via:

* `pyproject.toml` → `[tool.hatch.build.targets.wheel] packages = ["src/core", "src/modules", "src/workers"]`
* `[tool.pytest.ini_options] pythonpath = ["src"]`
* `[tool.mypy] mypy_path = "src"`, `explicit_package_bases = true`
* `[tool.ruff.lint.isort] known-first-party = ["core", "modules", "workers"]`

**Consequences.** Imports read `from core.config import Settings`. Console scripts
are declared as `workers.discovery:main` etc. Verified working: all three resolve
under `pytest`, `mypy` and as installed entrypoints.

---

### D-004 · Python 3.11, not the scaffold's 3.14

**Context.** `mtproto-platform/.python-version` said `3.14` and its
`pyproject.toml` said `requires-python = ">=3.14"`. The brief says "Python 3.11+".

**Decision.** `requires-python = ">=3.11"`, `.python-version` = `3.11`,
`target-version = "py311"` for ruff, `python_version = "3.11"` for mypy.

**Evidence.** The spike's own `REPORT.md` §1 records Python 3.11.4. Telethon was
verified importable on 3.11.2 across 1.34.0 → 1.45.0. A 3.14 floor would exclude
every deployment target named in the brief (Supabase/Neon/RDS-adjacent VPS images)
for no benefit.

**Consequences.** Widest wheel availability for asyncpg/SQLAlchemy; the managed
Python toolchain is not required.

---

### D-005 · Three independent OS processes; PostgreSQL is the only coordination mechanism

**Decision.** `discovery-worker`, `tester-worker`, `scoring-worker` are separate
processes with separate entrypoints (`mtproto-discovery`, `mtproto-tester`,
`mtproto-scorer`). They share **no** Python state, no asyncio application object,
no in-process bus. `core/lifecycle.py` deliberately holds no database handle.

**Evidence — measured, not assumed.** Three workers were started as real
processes; each got a distinct PID; `kill -9` on the tester left discovery and
scorer running and ticking for at least a further 2 s; `SIGTERM` and `SIGINT`
each produced a graceful `worker_stopped` with exit code 0:

```
### kill -9 tester (SIGKILL, cannot be caught)
  tester:    GONE
  discovery: ALIVE
  scorer:    ALIVE
  after 2s more -> discovery: ALIVE, scorer: ALIVE
### second signal forces immediate exit
  discovery exited after 1st SIGTERM: YES
  exit=0
```

**Consequences.** Acceptance criteria 12 and 13 of Task 025 are already satisfied
for the placeholder workers and must be re-verified once real work lands.

---

### D-006 · Signal escalation: first signal = graceful, second = immediate exit 130

**Decision.** `WorkerLifecycle` installs handlers for `SIGINT`/`SIGTERM`/`SIGHUP`
via `loop.add_signal_handler` where available, falling back to `signal.signal`
(Windows event loops raise `NotImplementedError`). The first signal sets a
shutdown event and the loop finishes its current tick. A second signal flushes
logging and calls `os._exit(130)`.

**Evidence — measured in a real process** with a deliberately 30 s tick:

```
sent SIGINT #1
  alive after 1st? YES (still in long tick - correct)
sent SIGINT #2
  alive after 2nd? NO (forced exit - correct)
  exit code: 130 (expect 130)
[info]    worker_shutdown_requested  reason=signal:SIGINT
[warning] worker_forced_exit         exit_code=130 signal=SIGINT
```

**Consequences.** In-flight work is never truncated by one signal, and an operator
always has an escape hatch. systemd `KillSignal=SIGTERM` plus a generous
`TimeoutStopSec` is the intended pairing (Task 020).

---

### D-007 · A failing tick never terminates a worker

**Decision.** `WorkerLifecycle.run()` catches `Exception` per tick, logs
`worker_tick_failed` with `exception_type`, increments `consecutive_failures`, and
applies exponential backoff from `WORKER_ERROR_BACKOFF_SECONDS` capped at
`WORKER_MAX_ERROR_BACKOFF_SECONDS`. `CancelledError`/`KeyboardInterrupt`/`SystemExit`
are re-raised so cooperative cancellation still works. An optional `tick_timeout`
bounds a hung tick.

**Evidence.** `tests/test_lifecycle.py::TestRunLoop::test_raising_tick_does_not_kill_the_worker`
asserts three ticks ran, two failed, and the loop recovered to exit cleanly.

**Consequences.** Task 018 (per-source failure isolation) and Task 006 (batch
testing) build on this rather than re-implementing it.

---

### D-008 · Secret redaction is a logging *processor*, not a caller convention

**Decision.** `core/logger.redact_secrets` runs inside
`structlog.stdlib.ProcessorFormatter` **after** `format_exc_info`. Two layers:

1. **Key-based** — values under credential-shaped keys are replaced wholesale.
   Matching is segment-wise (`(?:^|[_\-.])(secret|token|password|api_hash|api_key|
   authorization|database_url|dsn|session_string|private_key|…)(?:$|[_\-.])`), so
   `error_category`, `source_url` and `fingerprint` are never touched.
2. **Value-based** — strings are scrubbed for `secret=…`/`token=…` parameters
   (i.e. inside `tg://proxy?…` links), for `scheme://user:password@host` DSNs, and
   for `/bot<id>:<token>/` Bot API URLs.

Redaction recurses through mappings, lists, tuples and sets, and is bounded at
depth 12 so a pathological payload cannot hang or blow the stack.

**Rationale.** Running after `format_exc_info` is the load-bearing choice: it
scrubs secrets that arrive *inside exception text and tracebacks*, which a
caller-side convention cannot reach. Verified by
`test_secret_inside_exception_text_is_scrubbed`.

**Consequences.** Any later module that logs a proxy gets redaction for free.
`database_url` is additionally typed `SecretStr`, so `repr()`/`str()` of
`Settings` cannot leak the DSN password even without the processor.

**Known limit (documented, not hidden).** Redaction is pattern-based. A secret
concatenated into free text with no recognisable key or delimiter — e.g. a bare
62-char hex string in a prose sentence — will pass through. The defence against
that is to never construct such messages, which is why the tester logs
`proxy_id` + `fingerprint` rather than link strings (Task 005/007).

---

### D-009 · Third-party log level is configured separately from the application's

**Decision.** `LOG_LEVEL` controls the platform; `THIRD_PARTY_LOG_LEVEL`
(default `WARNING`) controls `asyncio`, `sqlalchemy.engine`, `sqlalchemy.pool`,
`telethon`, `pyrogram`, `asyncpg` (and, from Task 007, `uvicorn` / `fastapi`).

**Context.** This was found by a failing test, not by taste: setting
`LOG_LEVEL=DEBUG` let asyncio's `Using selector: EpollSelector` debug line into
the structured stream and broke the assertion that a tick emits exactly one event.

**Consequences.** Application `DEBUG` stays readable. Debugging Telethon during
Task 005 is one environment variable away rather than a code change. Regression
covered by `test_third_party_debug_noise_is_suppressed`.

---

### D-010 · `get_settings()` must **not** pass `_env_file=None`

**Context.** A real bug found and fixed during this task.

**Evidence.** Measured directly against pydantic-settings 2.15.0 with a `.env`
containing `LOG_LEVEL=DEBUG`:

```
Settings()                      -> DEBUG      (reads .env)
Settings(_env_file=None)        -> INFO       (.env IGNORED)
Settings(_env_file='.env')      -> DEBUG
```

`_env_file=None` **disables** dotenv loading; it does not mean "use the default".

**Decision.** A `_Unset` sentinel separates "argument omitted" from an explicit
`None`:

* `get_settings()` → `Settings()` → honours `model_config["env_file"] = (".env",)`
* `get_settings(env_file=None)` / `build_settings(env_file=None)` → dotenv off (tests)
* `get_settings(env_file=path)` → that file

`build_settings()` centralises the single `# type: ignore[call-arg]` needed
because pydantic-settings exposes this control as an underscore-prefixed init
keyword that type checkers do not model. `get_settings` is `lru_cache`d, so its
parameter is restricted to hashable types (`str | None | _Unset`) and
`reload_settings` normalises paths with `os.fspath`.

**Consequences.** Without this fix the deployed workers would have silently
ignored `.env` and run on development defaults. Covered by
`test_get_settings_reads_dotenv_by_default`.

---

### D-011 · Placeholders announce themselves

**Decision.** The three Task 001 ticks log `implemented=False` plus the
`pending_task`/`pending_tasks` that will replace them, and emit zero-valued
counters (`proxies_found=0`, `tests_started=0`, `scores_calculated=0`) so a
dashboard or log query can never mistake a heartbeat for real work. Their module
docstrings carry an explicit `.. warning:: **Status: placeholder (Task 001)**`.

**Evidence.** Enforced by test, not convention:
`tests/test_workers.py::TestPlaceholderHonesty` asserts the flag is present in the
rendered JSON and that each module's source contains `placeholder` and `TODO`.
`test_worker_has_no_runtime_dependencies_yet` parses each module's AST and fails
if it imports `telethon`, `pyrogram`, `sqlalchemy`, `asyncpg`, `alembic`,
`socket`, `httpx`, `aiohttp` or `requests`.

**Consequences.** Satisfies "do not claim something is implemented if it is only
a placeholder" mechanically. These tests must be **updated, not deleted**, when
Tasks 004–009 replace the ticks.

---

### D-012 · `uv run <script>` spawns a wrapper — supervisors must exec the real binary

**Context.** Observed while verifying D-005.

**Evidence.** Launching workers as `uv run mtproto-tester &` and then
`kill -9` on that PID killed the **`uv` wrapper**; the Python child (PID 2243)
survived as an orphan and kept ticking after the test script exited. Re-running
with `.venv/bin/mtproto-tester` gave the true PID and `kill -9` behaved as
expected, with no orphans left behind.

**Decision.** systemd units (Task 020) must `ExecStart=` the virtualenv binary
directly — e.g. `/opt/mtproto-platform/.venv/bin/mtproto-tester` — not
`uv run …`. If `uv run` is unavoidable, it needs process-group signalling
(`KillMode=control-group`) so the child is not orphaned.

**Consequences.** Prevents a supervisor from believing a worker is dead while it
keeps running, and prevents duplicate testers competing for the same rows once
Task 006's `FOR UPDATE SKIP LOCKED` claiming exists.

---

### D-013 · Telethon is the protocol engine, pinned **≥ 1.35.0**; the spike report is corrected, not trusted

**Decision.** Telethon remains the engine, but the pin moves from the spike's
`1.34.0` to **`>=1.35.0`** (target current 1.45.x). `spike/REPORT.md` is kept
verbatim under a correction banner; `spike/AUDIT.md` is the authoritative record.

**Evidence.** See `spike/AUDIT.md` and the committed outputs in `spike/evidence/`.
In summary: `spike_pyrogram.py` is a non-executable stub (empty `TEST_CASES`,
`check_tcp` body is `pass`, no `main()`); `TelegramClient.connect()` is `-> None`
in both 1.34.0 and 1.45.0, so the spike's `if connected:` was always False;
all four spike fixtures use `ee` fake-TLS secrets that **1.34.0 rejects with
`ValueError` before opening a socket**; `TcpMTProxy.normalize_secret` — which
makes them structurally acceptable — first appears in **1.35.0** (probed across
published wheels 1.34.0 → 1.45.0).

**Open risk carried into Task 005.** `normalize_secret` truncates to 16 bytes and
**discards the SNI domain** ("until domain support is added"), and `MTProxyIO`
implements no TLS ClientHello. Whether real `ee`-secret proxies accept Telethon's
handshake is therefore **empirically unverified**. No claim is made either way.
It can only be settled by a live test (`pytest -m live`, Task 016).

**Consequences.** Nine binding constraints for Task 005 are listed in
`spike/AUDIT.md` §4 — notably: success means *"connect() raised nothing"*, never
a boolean; use `MemorySession`; budget ≥ 2 s for the transport's built-in
`_wait_for_data` wait; never emit a `WRONG_SECRET` category.

**Self-correction on the record.** An earlier pass of the audit inspected
`MTProxyIO.init_header` in isolation, bypassing `normalize_secret`, and wrongly
concluded that even the latest Telethon rejected `ee` secrets.
`spike/verify_telethon_contract.py` now decodes secrets through the same entry
point the transport uses, and `AUDIT.md` §2 F4 documents the error and its fix.

---

### D-014 · `spike/` is excluded from lint and type checking

**Decision.** `[tool.ruff] extend-exclude = ["spike", "alembic/versions"]`;
`[tool.mypy] exclude = ['^spike/', '^alembic/versions/']`.

**Rationale.** The spike scripts are known-broken historical artifacts (D-013).
Reformatting them to satisfy ruff would destroy their evidentiary value — the
audit quotes them verbatim. `alembic/versions` is excluded because migration
files are generated.

**Consequences.** `uv run ruff check .` and `uv run mypy .` stay green without
pretending the spike is production code. `spike/verify_telethon_contract.py` is
*also* excluded, so it is run explicitly rather than gated by CI.

---

### D-015 · Test markers: `integration`, `live`, `stress` — none run by default

**Decision.** `[tool.pytest.ini_options] addopts = "-ra --strict-markers --strict-config"`
with three registered markers:

| Marker | Meaning | Default |
|---|---|---|
| `integration` | needs a real local PostgreSQL (`DATABASE_URL`) | **not** run by plain `pytest` unless a DB is reachable |
| `live` | real network I/O against public endpoints | **never** in CI; `pytest -m live` only |
| `stress` | long-running resource/leak test | opt-in via `-m stress` |

`--strict-markers` makes a typo'd marker a hard error instead of a silent no-op.

**Consequences.** Prepares Task 016 (integration) and Task 017 (stress) and
enforces "do NOT hit real public proxies during unit tests". Task 001's 162 tests
are all unit-level: no database, no network.

---

### D-016 · Explicitly out of scope for the MVP

No Redis. No Celery. No FastAPI *(superseded for the ranking HTTP adapter by D-041)*. No Cloudflare Workers. No Kubernetes. No
microservice framework. No SOCKS5/HTTP/VLESS/VMess/Trojan/Xray/Shadowsocks —
**MTProto only**. PostgreSQL is the work-coordination mechanism
(`FOR UPDATE SKIP LOCKED`, Task 006/014).

**Rationale.** Stated in the brief and consistent with the core hypothesis being
about *measurement quality*, not infrastructure scale.

---

### D-017 · Four tables, four responsibilities

| Table | Role | Lifetime |
|---|---|---|
| `proxies` | **identity** — one row per distinct configuration | effectively permanent |
| `proxy_discoveries` | **provenance** — where/when an identity was sighted | append-only |
| `proxy_observations` | **measured behaviour** — one row per test attempt | append-only, the asset |
| `proxy_scores` | **derived state** — versioned snapshots | append-only history |

**Rationale.** These four have different write rates, different retention needs
and different deletion semantics. Collapsing them — most temptingly, folding
"current score" into `proxies` — would mean the scoring worker writes to the same
row the tester leases, turning the coordination mechanism into a contention point
and destroying score history in one move.

**Consequences.** "Latest score" is a query (`DISTINCT ON (proxy_id) ... ORDER BY
calculated_at DESC`), not a column. That query is indexed and cheap. Reporting
must never treat `proxies` as a source of score truth.

---

### D-018 · No CHECK constraint on `protocol`

`proxies.protocol` is `VARCHAR(16)` with a Python-side default of `"mtproto"` and
no database-level allowlist.

**Rationale.** The MVP is MTProto-only, but the brief asks for extensibility
without building a multi-protocol platform. A closed CHECK list would make every
future protocol a schema migration and a deploy-coordination problem. Correctness
is already protected: `protocol` is an input to the fingerprint, so a wrong value
yields a *different identity* rather than a collision or a silent merge.

**Consequences.** A typo'd protocol string is storable. That is accepted — it
cannot corrupt another proxy's history, and Task 003's parser validates the value
before it reaches the database.

---

### D-019 · Fingerprint = versioned SHA-256 over protocol, server, port, secret bytes

```
sha256("v1" \x1f protocol \x1f normalized_server \x1f port \x1f secret_bytes.hex())
```

* `\x1f` (US) as the field separator, not `:` or `|`.
* The secret is **decoded to bytes** (hex first, base64 fallback) and the full
  byte string is hashed — no truncation.
* The scheme version `v1` is part of the hashed payload.
* `normalize_server`: strip, lowercase, remove IPv6 brackets, remove a trailing
  root dot. No DNS resolution, no validation.

**Rationale.** The UNIQUE index on `fingerprint` is what makes 10,000 sightings of
one configuration collapse into one row, so the definition of "same proxy" is the
load-bearing decision in the schema.

Three specifics were verified rather than assumed:

1. **A `:` separator really does collide.** `server="x:1", port=2, secret="y"` and
   `server="x", port=1, secret="2:y"` both join to `x:1:2:y`. `\x1f` cannot
   appear in a host, port or secret, so the split is unambiguous. Pinned by
   `test_field_separator_prevents_concatenation_collisions`.
2. **The secret must be in the hash.** Several distinct MTProto secrets routinely
   share one `server:port`. Hashing the endpoint alone would merge different
   proxies and lose candidates.
3. **Full bytes, not Telethon's 16.** Telethon ≥1.35 `normalize_secret` truncates
   to 16 bytes because it cannot use the fake-TLS SNI domain (see
   `spike/AUDIT.md`). For *identity* the domain matters: `ee<key>google.com` and
   `ee<key>telegram.org` are different proxies. Truncating would merge them.

Including the version means a future scheme change is explicit: mixed schemes in
one table would silently split or merge identities, which is unrecoverable.

**Consequences.** `core/identity.py` has no SQLAlchemy and no I/O, so Task 003's
parser, the ORM and the tests all share one definition. A golden digest is pinned
in the tests; changing the scheme forces a conscious version bump.

---

### D-020 · Secrets stored in plaintext, protected by type and scrubbing

The `proxies.secret` column stores the real value. There is no at-rest encryption.

**Rationale.** The tester needs the plaintext to connect, so the value must be
recoverable. Application-level encryption without a key-management strategy is
theatre — it moves the secret into a key that then needs protecting, and the MVP
has nowhere to put one. Inventing a half-solution would create a false sense of
security, which is worse than an honest one.

Protection is layered and each layer is tested:

1. `ProxySecret` — a non-`str` wrapper. `str()`, `repr()`, `format()` (with *any*
   format spec) and f-strings all yield a masked form. `json.dumps` raises rather
   than emitting. Plaintext requires an explicit `.reveal()`, giving a reviewer
   one thing to grep for.
2. `SecretText` (a `TypeDecorator`) wraps every value read from the database, and
   a `@validates` hook wraps every value *assigned* in Python. Without the second
   half, a freshly constructed `Proxy` — the one most likely to be printed while
   debugging — would hold a bare `str`.
3. `core.logger.scrub_secrets` masks credentials in every log line and in every
   value persisted to `proxy_observations.error_message_safe`.

It is deliberately **not** a `str` subclass: a subclass would leak through
`str.__format__` and through any `isinstance(x, str)` serialisation path.

**Consequences.** Anyone who can read the database can read the secrets. That is
accepted for the MVP and must be revisited before any multi-tenant or hosted
deployment — see the follow-up table below.

---

### D-021 · `ON DELETE RESTRICT` for observations, `CASCADE` for discoveries and scores

`proxy_observations.proxy_id` → `RESTRICT`. `proxy_discoveries` and `proxy_scores`
→ `CASCADE`.

**Rationale.** Measurement history is the asset this platform exists to build; it
is also the only data that cannot be regenerated (a proxy that disappeared cannot
be re-tested). Deleting it as a side effect of removing an identity row would be
the worst failure mode available, so the database refuses. Scores and provenance
are derivable or meaningless without the identity, so they cascade.

**Consequences.** Retiring a proxy is `is_active = false`, not `DELETE`. Hard
deletion requires removing observations first, which makes sanitisation an
explicit, reviewable act. Retention pruning is a separate job, never a cascade.

---

### D-022 · `proxy_scores` is append-only history

One row per scoring run, not one row per proxy. There is no UNIQUE constraint on
`proxy_id`.

**Rationale.** Scores are derived and recomputed. Storing history means a change
to the scoring formula never destroys the ability to explain why a proxy ranked
differently last week — which matters because the MVP's whole purpose is
evaluating whether the scoring approach finds good candidates. `scoring_version`
is mandatory for the same reason `tester_version` is on observations: without it
a methodology change is indistinguishable from a behaviour change.

**Consequences.** `ix_proxy_scores_proxy_id_calculated_at` serves "latest per
proxy". The table grows; it is a snapshot table and can be pruned by age without
losing the underlying observations.

---

### D-023 · `next_test_at` is `NOT NULL DEFAULT now()`

**Rationale.** Two problems disappear at once:

* A freshly discovered proxy is immediately due, with no special case in the
  claim query.
* A nullable column would force `ORDER BY next_test_at NULLS FIRST` to put
  never-tested proxies at the front of the queue — and a plain ASC btree stores
  NULLs *last*, so neither a forward nor a backward scan could serve that
  ordering. An explicit `NULLS FIRST` index would be required.

This was found the hard way: `postgresql_nulls_first` is **not** a valid `Index`
dialect kwarg in SQLAlchemy 2.x, so the obvious way to express it does not exist.
Making the column NOT NULL removes the need entirely rather than fighting the ORM.

**Consequences.** "Never tested" and "due now" are the same state. That is the
desired semantics for a discovery-driven queue.

---

### D-024 · Claiming: CTE + `FOR UPDATE SKIP LOCKED` + `UPDATE … RETURNING`, lease-based

One statement, one round trip:

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

Three rules follow from it:

1. **`SKIP LOCKED`, never plain `FOR UPDATE`.** With plain `FOR UPDATE` a second
   worker *blocks* until the first commits. If the first is sitting in an
   8-second MTProto handshake, the second is idle for 8 seconds. `SKIP LOCKED`
   makes it take different rows, so N workers partition the work.
2. **Never hold a transaction open across network I/O.** Claim in one
   transaction, commit, test, then write results in a second transaction. The
   claim query returns everything the tester needs (`RETURNING *`), so no
   follow-up read is required.
3. **The lease (`test_lock_until`) is what makes a crash self-healing.** A worker
   killed with `kill -9` never clears its claim; the lease expires and the row
   becomes claimable again. Without it, one crash would strand proxies
   permanently.

`test_attempts` is incremented **server-side** (`SET x = x + 1`), not read-modify-
written in Python, so concurrent claims cannot lose an update. The counter is the
trail left by a proxy that repeatedly kills workers.

The `ORDER BY … , id` tiebreak makes the ordering total, so two workers cannot
both see the same "first" row when timestamps are equal.

**Two guarantees, not one — and the second had to be moved into Python.**
`UPDATE … FROM claim_candidates … RETURNING` does **not** preserve the CTE's
`ORDER BY`. PostgreSQL joins the CTE to the target table and returns rows in join
order, which tracks heap layout. Verified against PostgreSQL 16.2: with six due
rows inserted in scrambled order and `limit=3`, the three *oldest* were correctly
leased, but came back as `p1, p0, p2`.

So:

* **Selection fairness is enforced by the database** and was never at risk — the
  CTE's `ORDER BY next_test_at, id LIMIT n` decides *which* rows get leased, so no
  proxy can be starved by a stream of new arrivals.
* **The returned list order is enforced in Python** (`claimed.sort(...)`), because
  relying on the SQL would make it depend on what else happens to be in the table.

This was found by a test that had previously passed *by luck*: the assertion on
output order held only while heap layout happened to coincide with due order. A
fresh cluster exposed it. It matters because a tester working the batch in order
should reach the most-overdue proxies first — if it is killed mid-batch, the
most-starved rows were already done. Cost is negligible: at most `limit` rows.

**Consequences.** PostgreSQL is the only coordination mechanism — no Redis, no
Celery, matching D-016. Verified with four concurrent engines (separate pools,
standing in for separate processes) claiming 40 rows: every row claimed exactly
once, no blocking, no deadlock.

One hazard was found and is now documented in the tests: a **rollback expires
every ORM attribute**, and re-reading one fires a lazy refresh that cannot run
under asyncio. `expire_on_commit=False` protects the commit path only. Workers
must read `id`/`server`/`port`/`secret` out of a claimed proxy immediately.

---

### D-025 · Partial index `(observed_at) WHERE success`, not composite `(success, observed_at)`

The Task 002 outline suggested a composite on `(success, observed_at)`. Shipped
instead: `ix_proxy_observations_success_observed_at` on `(observed_at)` with a
`WHERE success` predicate.

**Rationale.** Latency aggregation only ever reads successes, and the filter value
is a constant. A boolean leading column roughly doubles the index size while
serving exactly the same query. The partial form is smaller and equally usable.

**Consequences.** A query filtering on `success = false` cannot use it — correct,
since failure analysis aggregates over `proxy_id`+`observed_at`, which the
composite `ix_proxy_observations_proxy_id_observed_at` serves. Verified with
`EXPLAIN` under `enable_seqscan=off`: the claim query does use `ix_proxies_due`,
and a query that drops `is_active` correctly *cannot*, which is the discriminating
proof that the index really is partial.

---

### D-026 · `error_category` is `VARCHAR(32)` + a Python `StrEnum`, not a PostgreSQL enum

**Rationale.** The failure taxonomy will evolve as Task 005 meets real Telethon
behaviour. A native enum makes every new category a migration (`ALTER TYPE … ADD
VALUE`, which cannot run inside a transaction block before PG 12 and is awkward
after). A `StrEnum` in Python gives the same ergonomics — tab completion,
exhaustive matching — with no schema coupling.

`WRONG_SECRET` is deliberately **absent**. MTProxy drops bad-secret payloads
without RST or error, so a wrong secret is indistinguishable from a blackholed
endpoint; claiming to detect it would fabricate a diagnosis. Those cases surface
as `MT_PROTO_TIMEOUT` (see `spike/AUDIT.md` §3).

The outline's names map as follows so Task 005 does not reinvent them:
`TCP_UNREACHABLE`→`TCP_ERROR`, `MTPROXY_TIMEOUT`→`MT_PROTO_TIMEOUT`,
`MTPROXY_PROTOCOL_ERROR`→`PROTOCOL_ERROR`,
`TELEGRAM_CONNECTION_ERROR`→`TELEGRAM_RPC_ERROR`, `INVALID_PROXY`→
`PROTOCOL_ERROR`, `UNKNOWN`→`UNKNOWN_ERROR`.

**Consequences.** The database will accept an unlisted category string. That is
the point — a new category is a code change, not a deploy-coordinated migration.

---

### D-027 · Two defences against secrets in database error text

Discovered empirically, not assumed. When a CHECK constraint rejects a row,
PostgreSQL appends `DETAIL: Failing row contains (...)` and echoes **every
column** — including the plaintext secret. That text reaches
`safe_error_message`, which would have persisted it to
`proxy_observations.error_message_safe` and logged it.

Two independent leak paths, two independent fixes:

1. **`hide_parameters=True`** on the engine. SQLAlchemy appends
   `[parameters: (...)]` to DBAPI errors. Off by default in this project;
   toggleable via `DB_HIDE_PARAMETERS` for local debugging.
2. **A shape-based scrubber** in `core.logger`: any bare run of ≥32 hex
   characters is masked. Key-based patterns (`secret=…`) cannot reach a value
   that arrives with no key in front of it. This is the *only* thing that covers
   PostgreSQL's own `DETAIL` output — `hide_parameters` does not.

**Cost, accepted deliberately:** 64-character fingerprints are masked in error
text too. A fingerprint is derivable from the row and rarely belongs in an error
message; a secret in a log aggregator is not recoverable. Shorter hex runs
(ports, ids, latencies) are untouched, so ordinary debugging output survives.

**Consequences.** `safe_error_message` output is safe to persist by construction.
The raw SQLAlchemy exception still contains the secret — so it must never be
logged or stored directly, only through the helper.

---

### D-028 · No module-level engine singleton

Each process builds its own `Database` and disposes it at shutdown.

**Rationale.** A global would be per-process anyway, but keeping it explicit
removes any temptation to share state across processes and — the concrete bug —
avoids binding a connection pool to an event loop that a later `asyncio.run()`
has already replaced.

**Consequences.** Worker entrypoints own `Database` lifetime. Integration tests
build one engine per test, which is why the fixture is function-scoped.

---

### D-029 · `TEST_DATABASE_URL` is derived, and refused in production

When unset, `resolved_test_url` appends `_test` to the database component of
`DATABASE_URL`. Under `ENV=production` with no explicit value, it **raises**.

**Rationale.** The migration lifecycle test runs `downgrade base`, which DROPS
EVERY TABLE. Derivation means a contributor never points the destructive suite at
their development data by accident; the production refusal means a production
database can never become a test target by inference.

Derivation uses `urllib.parse`, not string surgery, because asyncpg carries the
Unix-socket directory in `?host=` and that must survive untouched — losing it
would make the suite dial a TCP port with no server on it. Idempotent: an
already-`_test` database is not doubled.

**Consequences.** Production integration testing requires an explicit
`TEST_DATABASE_URL`. That friction is intentional.

---

### D-030 · Local PostgreSQL via `pgserver`, not Docker

`scripts/dev_pg.py` provisions a real PostgreSQL 16.2 from a self-contained wheel:

```
uv run --with pgserver python scripts/dev_pg.py up
uv run --with pgserver python scripts/dev_pg.py run -- uv run pytest -m integration
uv run --with pgserver python scripts/dev_pg.py down
```

**Rationale.** Task 002 requires the integration suite to genuinely run, and
Docker must not become a local-development requirement. `pgserver` is fetched ad
hoc — exactly like the spike's `uv run --with telethon` — so it is **not** a
project dependency and does not appear in `uv.lock`.

It provisions a **non-superuser** role (`mtproto`, with `CREATEDB`) so migrations
and tests exercise real privileges instead of silently relying on superuser rights
production will not grant. The cluster lives in `$XDG_CACHE_HOME`, outside the
repository: a PostgreSQL data directory is thousands of small files and does not
belong in a working tree.

Any other PostgreSQL works — point `DATABASE_URL` at Supabase/Neon/RDS/a system
package and never run this script.

**Consequences.** No Docker dependency. `pyproject.toml` and `uv.lock` are
unchanged. The integration suite still skips cleanly when no server is present.

---

### D-031 · Integration tests truncate; migration tests get their own database

* Tables are `TRUNCATE … RESTART IDENTITY CASCADE`d before each test rather than
  rolled back in a savepoint.
* The migration lifecycle module creates and drops a dedicated
  `<testdb>_lifecycle` database.

**Rationale.** A savepoint rollback cannot exercise `FOR UPDATE SKIP LOCKED`
across concurrent sessions, which is the single most important behaviour in this
layer — the whole point is what *other* transactions see. And `downgrade base`
drops every table, so it must never share a database with anything else.

Fixtures run the real Alembic CLI rather than `Base.metadata.create_all`: the
point is that the *migration* produces a working schema. `create_all` would let
the migration rot while every test still passed. `alembic check` is asserted
empty after upgrade, which is what catches a model change never turned into a
migration.

**Consequences.** A `pytest_runtest_makereport` hook scrubs credentials out of
failure output, because pytest prints fixture values verbatim and these fixtures
hold DSNs — and a failure report is exactly what gets pasted into an issue.

---

### D-032 · `Float` for latencies, `Numeric` for scores

`proxy_observations.*_ms` → `DOUBLE PRECISION`. `proxy_scores.score` /
`reliability_*` / `latency_p*` → `NUMERIC`.

**Rationale.** Observations are sensor readings at millions of rows, where storage
and comparison speed matter more than exact decimal semantics — but sub-millisecond
precision is real (a LAN handshake can be 0.4 ms), so an integer column would be
lossy. Scores are ranked and compared, where exact decimal semantics keep ordering
deterministic and float accumulation would not.

**Consequences.** `NUMERIC(6,3)` caps `score` at 999.999, so a value of 1000
overflows the *type* as a `DBAPIError` before the `score <= 100` CHECK can fire.
Two independent guards, different exceptions — both pinned in tests so nobody
"fixes" one and assumes the other covers it.

---

### D-033 · Cross-platform failures are reproduced on Linux, not skipped

Four cross-platform issues were found only because the suite was run on Windows.
None was a Windows bug in the strict sense — each was a *narrow assumption* that
happened to hold on the only platform being tested.

**(a) Clock granularity.** `time.monotonic()` on Linux has 1 ns resolution
(`clock_gettime(CLOCK_MONOTONIC)`); on Windows it advances in ~15.6 ms quanta
(1/64 s, the system timer tick). A worker loop run with `interval=0.0` finishes
three ticks in microseconds, so on Windows the measured uptime is *exactly*
`0.0` and `assert life.uptime_seconds > 0.0` fails.

The test was wrong, not the code: `uptime_seconds` already clamps with
`max(0.0, ...)`, which documents zero as a legal value. The assertion was
restated as `>= 0.0`.

**(b) An exception list that was not closed.** `is_reachable()` and
`database_exists()` both documented "never raises" but caught only
`(SQLAlchemyError, OSError, ValueError)`. The project's own development DSN
carries a Unix socket directory in `?host=`, and on Windows the driver refuses
Unix sockets with `NotImplementedError` — none of the three. The blast radius
was larger than one test: integration fixtures call `database_exists()` to
decide whether to skip, so on Windows the whole suite would have died during
*collection* rather than skipping cleanly.

Both handlers are now `except Exception`. This is not laziness — it is what the
docstring already promised, and `CancelledError` derives from `BaseException` so
cooperative cancellation still propagates.

**(c) Windows signal delivery via `os.kill`.** `os.kill(os.getpid(), signal.SIGTERM)`
on Linux sends a catchable POSIX signal to the process; on Windows, `os.kill` for
any signal other than `SIGINT`/`SIGBREAK` calls `TerminateProcess()` directly,
terminating the runner process unconditionally without running any Python signal
handlers. Even for `SIGINT`, `os.kill` delegates to `GenerateConsoleCtrlEvent`,
which requires a console process group ID and does not map to event loop signal
listeners. The tests `test_real_sigterm_requests_graceful_shutdown` and
`test_running_loop_stops_on_real_signal` specifically test POSIX signal
integration through the event loop; on Windows they now skip cleanly with an
explanatory message, while `test_fallback_handler_used_when_loop_cannot`
continues to verify the Windows fallback signal mechanism.

**(d) IANA timezone database unavailable on Windows without tzdata.** On Linux,
Python's standard library `zoneinfo` reads timezone files from `/usr/share/zoneinfo`.
On Windows, no such directory exists; `zoneinfo` falls back to the optional `tzdata`
PyPI package. When `tzdata` is not installed, `ZoneInfo("Asia/Tehran")` raises
`ZoneInfoNotFoundError`. The unit test `test_accepts_a_non_utc_timezone` in
`tests/test_scheduling.py` needed a timezone-aware datetime with a non-UTC offset
to verify that `claim_due_proxies` handles non-UTC datetimes; it now uses
`timezone(timedelta(hours=3, minutes=30), name="Asia/Tehran")` from `datetime`,
which is built-in, requires zero external packages or OS files, and behaves
identically on every platform.

**Rationale.** A guard that only fails on an untested platform is worse than no
guard, because CI keeps reporting green. So each failure was converted into a
test that reproduces the platform *here*:

| Platform condition | Reproduced on Linux by |
|---|---|
| 15.6 ms monotonic quanta | `monkeypatch`ing `time.monotonic` to `int(t / 0.015625) * 0.015625` |
| Driver rejecting a Unix-socket DSN | `monkeypatch`ing `AsyncEngine.connect` to raise `NotImplementedError` |
| Windows `os.kill` terminating process | `monkeypatch`ing `sys.platform` to `win32` and asserting clean skips |
| Windows without `tzdata` package | `datetime.timezone` with explicit offset and name |

All are exact stand-ins for the real thing, so `test_survives_a_coarse_monotonic_clock`,
`test_is_reachable_survives_a_failure_type_nobody_enumerated`,
`test_returns_false_when_the_driver_rejects_the_dsn_for_platform_reasons`, and
`test_skips_real_os_kill_on_windows` now run in every CI job rather than only on
the machine that discovered them. Running the whole unit suite under the quantised
clock yields **500 passed** — the same as without it, which is the point.

**Consequences.** `ping()`'s docstring now states the failure-type list is
open-ended and points at `is_reachable()` as the safe entry point, so the next
caller does not re-derive a three-type `except` and reintroduce the bug.
`scripts/dev_pg.py` remains unverified on Windows — it drives `pgserver`'s Unix
socket path, and nothing in this decision claims otherwise.

---

### D-034 · What was taken from the parallel `wip/mtproto-platform-local` draft

A second, independent draft of the same project existed uncommitted on a
developer machine (`4239c5c`, 25 files, ~700 lines) under a `mtproto-platform/`
subdirectory. It was reviewed file by file rather than merged wholesale or
dismissed. Three things came out of it.

**Taken.**

*`infra/docker/docker-compose.yml`* — an optional containerised PostgreSQL.
Adopted with two corrections: the draft hardcoded `POSTGRES_PASSWORD: password`
and committed it, which puts a live credential in version control and in every
`docker inspect`, so credentials are now interpolated and the password is
*required* (`${POSTGRES_PASSWORD:?…}`); and the port is bound to `127.0.0.1`
only. The obsolete `version:` key was dropped. This does not reverse D-030 —
`dev_pg.py` is still the primary path and Docker is still not required by
anything.

*The normalisation intent behind `tests/test_fingerprint.py`* — the draft
asserted that `" MTproto"`, `" 1.2.3.4  "`, `"EE000 "` and their clean
lower-case equivalents are one identity. Checked dimension by dimension against
`compute_fingerprint`: protocol case, server whitespace, secret whitespace and
secret case all already agree. The one divergence is the draft's own fixture —
`"EE000"` is odd-length and therefore not valid hex, so it lands in the opaque
fallback where case is *preserved*. That is deliberate and is now pinned by
`test_case_folding_applies_to_hex_but_not_to_opaque_fallbacks`, because the
opaque branch is where non-hex base64 secrets land and base64 is case-sensitive:
folding there would merge two different proxies into one row and lose one
forever. A duplicate row wastes a test; a false merge loses a proxy.

**Logged for Task 004, not taken now.**

*`ProxySource` as a first-class table* — the draft normalises sources into their
own table with `last_scraped_at`, where this schema carries provenance inline on
`proxy_discoveries` (`source_type`, `source_name`, `source_url`). Their shape is
the better fit for the source-health metadata Task 004/018 needs, and for
deduplicating source URLs. It is a schema change, though, and Task 002 is
migrated and green; retrofitting it now would churn a committed schema for a
requirement that has not been designed yet. Added to the deferred table.

**Rejected, with reasons.**

| Draft | Why not |
|---|---|
| `echo=(settings.env == "dev")` | SQLAlchemy `echo` logs statements **with bound parameters**. In dev this writes every MTProto secret and the DB password to stdout. The single most serious issue in the draft. |
| Module-level `engine` / `settings` at import time | Importing `core.database` then requires a valid `DATABASE_URL` and builds a pool nobody asked for. D-028 rejected this already. |
| No lease columns on `Proxy` | No `locked_by` / `locked_until` / `next_test_at` / `is_active`, so `FOR UPDATE SKIP LOCKED` claiming is impossible and three processes would grab the same proxy. That is the coordination mechanism the brief mandates. |
| `secret: Mapped[str] = mapped_column(String)` | Unbounded and unmasked; no `ProxySecret`. Combined with `echo=True` there is nothing between a secret and a log file. |
| `ProxyScore` one-to-one, `proxy_id` as PK | Overwrites each snapshot. "Did the score improve?" and scoring regressions become unanswerable; D-022 chose append-only history. |
| `cascade="all, delete-orphan"` on observations | Deleting a proxy silently destroys its measurement history. D-021 chose RESTRICT. |
| `Index(..., "is_success")` | A btree over a two-valued column is near-useless. D-025 chose a partial index `WHERE success`. |
| `except NotImplementedError: pass` around `add_signal_handler` | On Windows this installs **no** handler at all, so Ctrl+C hard-kills the worker instead of shutting it down. Ours falls back to `signal.signal()`. Same fragility class as D-033. |
| `logger.py` with no scrubbing | No equivalent of `scrub_secrets()`; D-027's two defences would not exist. |
| `database_url` default with `postgres:password@localhost` | A credential-shaped default baked into source. |
| `Base` with no naming convention | Unnamed constraints cannot be reliably dropped or altered by later Alembic revisions. |
| `tests/test_fingerprint.py` importing `src.modules.fingerprint` | That module does not exist in the draft, so its suite errors during collection. |
| `main.py` = `print("Hello from mtproto-platform!")` | The untouched `uv init` stub. |

**Rationale.** The draft has the right module *names* and one genuinely better
schema idea, but it is early-stage: no identity implementation behind its
fingerprint test, no scheduling columns, no secret handling, and an `echo=True`
that would leak secrets in the exact environment developers stare at. Merging
code would have been a regression; merging the two ideas and the one
counter-example was not.

**Consequences.** Reviewing it produced a test this project would not otherwise
have had — the hex/opaque case-folding asymmetry was correct but undocumented,
and would have looked like a bug to the next reader. `ProxySource` is now on the
deferred table for Task 004 instead of being rediscovered there.

---

### D-035 · Discovery, parsing, secret taxonomy, and SSRF architecture

Task 003 implements the discovery layer (`src/modules/discovery`). Key decisions:

1. **Domain Model (`MTProtoProxy`):**
   * Immutable dataclass with `slots=True`.
   * Directly uses `core.identity.ProxySecret` for the secret; `repr` and `str` never expose plaintext.
   * `server` is automatically canonicalised (`normalize_server`) and `fingerprint` is precomputed on initialization.
   * Preserves fake-TLS `sni_domain` and `SecretType` for downstream tester use.

2. **Parsing Rules:**
   * Accepts `tg://proxy?...`, `https://t.me/proxy?...`, and `http://t.me/proxy?...`.
   * Reordered query parameters yield the identical canonical proxy identity.
   * Duplicate parameters with identical values are tolerated; conflicting values raise `ProxyParseError`.
   * HTML unescaping and trailing punctuation stripping allow safe extraction from raw text and HTML attributes.
   * Parser never crashes on malformed external input and exception messages are strictly scrubbed of secrets.

3. **Secret Validation:**
   * MTProto secrets are decoded (hex first, then base64 fallback).
   * Categorised strictly into `LEGACY` (16 bytes), `SECURE_RANDOMIZED` (17 bytes starting `0xdd`), and `FAKE_TLS` (>=17 bytes starting `0xee`).
   * For `FAKE_TLS`, trailing bytes are decoded as ASCII and validated against hostname rules.
   * Telethon >= 1.35.0 transport limitation is documented: it normalises `ee` secrets by truncating to 16 bytes and dropping SNI, so wire-level fake-TLS connectivity remains empirically unverified until live testing in Task 005.

4. **Server Validation & SSRF Protection:**
   * Server strings are validated syntactically without DNS resolution during parsing.
   * Disallows loopback, RFC 1918 private, link-local (including 169.254.169.254), multicast, unspecified, documentation, and internal domains (`.localhost`, `.local`, `.internal`).
   * `SsrfSafeHttpClient` performs DNS resolution before connecting and after every redirect hop to block SSRF and DNS rebinding to internal networks.

5. **Persistence & Lifecycle:**
   * Uses PostgreSQL `INSERT INTO proxies ... ON CONFLICT (fingerprint) DO UPDATE`.
   * Discovery sets `is_active=True`, `last_seen_at=now`, and `next_test_at=now`.
   * Discovery **never** sets `WORKING` status or generates fake observations; network health is strictly the tester's responsibility.

---

### D-036 · MTProto tester architecture, Telethon audit findings, and Fake-TLS limitations

Task 004 implements the real MTProto connectivity tester (`src/modules/tester` and `src/workers/tester.py`). Key decisions and verified audit findings:

1. **Telethon Transport Architecture & Audit:**
   * Telethon is pinned at `>=1.35.0` (probed and verified against 1.45.0).
   * Transport classes audited: `ConnectionTcpMTProxyRandomizedIntermediate` (mandatory for `0xdd` secure randomized secrets, and optimal for 16-byte legacy secrets), `ConnectionTcpMTProxyIntermediate` (4-byte length), and `ConnectionTcpMTProxyAbridged` (1-byte length).
   * `select_transport()` maps `SECURE_RANDOMIZED` (`0xdd`) and `LEGACY` to `ConnectionTcpMTProxyRandomizedIntermediate` for maximum obfuscation and firewall resistance.

2. **Fake-TLS (0xee) Findings & Honest Limitation:**
   * Telethon's `TcpMTProxy.normalize_secret` truncates `0xee` secrets to 16 bytes and explicitly discards the SNI domain (`"until domain support is added"`).
   * Telethon does **not** implement wire-level TLS emulation (no TLS `ClientHello` is emitted, no SNI is sent). MTProxy servers enforcing real Fake-TLS handshakes drop these connections.
   * Rather than faking support or generating false failure diagnoses, `select_transport()` classifies Fake-TLS secrets as unsupported (`UNSUPPORTED_TRANSPORT`) with an honest, documented diagnostic message explaining Telethon's lack of wire-level TLS emulation.

3. **Latency Floor Discovery in Telethon:**
   * Audited `TcpMTProxy._connect()`: contains an unconditional 2-second latency wait (`self._wait_for_data('proxy')`) as a workaround for upstream issue #1134 (server dropping rapid payloads).
   * Consequently, all Telethon-measured MTProto connects have a structural ~2000 ms floor. We measure raw TCP handshake latency separately in Phase 2 (`tcp_connect_ms`), providing accurate physical network latency alongside the MTProto session latency (`mtproto_connect_ms`).

4. **Ephemeral Session Handling:**
   * Ephemeral test client strictly uses `telethon.sessions.MemorySession()`.
   * Never passes a filename string or session name, ensuring zero SQLite `.session` files are created on disk.
   * `TelegramClient.disconnect()` is explicitly awaited in a `finally` block to release file descriptors and transport sockets immediately.

5. **SSRF and DNS Rebinding Protection:**
   * `resolve_and_validate_destination` resolves hostnames and evaluates all returned IPv4/IPv6 addresses against strict private, loopback, link-local (e.g. AWS metadata `169.254.169.254`), multicast, and reserved ranges.
   * The destination is pinned to a single validated IP address literal for subsequent TCP and Telethon connections, entirely eliminating the window for DNS rebinding attacks.

6. **Transaction Discipline & Scheduling:**
   * Claims rows via `claim_due_proxies` (`FOR UPDATE SKIP LOCKED`) in Transaction 1, commits, releases locks.
   * MTProto network probing is executed strictly **outside** any database transaction (D-024).
   * Transaction 2 appends immutable `ProxyObservation` rows, clears leases (`test_lock_until = None`), updates `last_test_finished_at`, and schedules next test times (1h forward for successes, 15m forward for failures).

---

### D-037 · Phase 3 success is unauthenticated `help.getConfig`, not `is_user_authorized()`

**Context.** Task 004.1 audited the Phase 3 success criterion against Telethon 1.45.0 source. The shipped probe called `client.connect()` then `client.is_user_authorized()` and treated a non-raising call as full MTProto/API verification.

**Evidence — Telethon 1.45.0 `UserMethods.is_user_authorized`:**

```python
if self._authorized is None:
    try:
        # Any request that requires authorization will work
        await self(functions.updates.GetStateRequest())
        self._authorized = True
    except errors.RPCError:
        self._authorized = False
return self._authorized
```

* The RPC is `updates.GetStateRequest`, which **requires user authorization**.
* Every `RPCError` is swallowed. A fresh `MemorySession` always takes the except branch and returns `False`.
* Returning `False` is therefore the *normal* unauthenticated outcome, not proof that Telegram answered a usable API method, and not a proxy failure.
* If `_authorized` is already set, **no RPC is sent at all**.

`TelegramClient.connect()` already sends `InvokeWithLayer(InitConnection(help.GetConfigRequest))` and awaits the future from `MTProtoSender.send`. That is transport + init, bundled inside Telethon. It is not our explicit success criterion.

**Decision.** Phase 3 success requires an explicit unauthenticated high-level RPC after `connect()`:

* Request: `telethon.tl.functions.help.GetConfigRequest` (`help.getConfig`, constructor `0xc4f9186b`).
* Success: the result is a `telethon.tl.types.Config` with a non-empty `dc_options` list.
* Why this RPC: it works without user login, without a stored session, without phone verification, and without a bot token; it is the same method Telegram clients use during init; a valid `Config` is a real API object from Telegram, not session state.

Stages that are **not** success:

* TCP connect (`tcp_connect_ms` only)
* `client.connect()` returning without error (MTProto transport / init)
* `is_user_authorized() is False`

`is_user_authorized()` is not called. `False` from it would not fail the probe.

Timeouts on this RPC stay `MT_PROTO_TIMEOUT`. Raised `RPCError` stays `TELEGRAM_RPC_ERROR`. A non-`Config` result is `PROTOCOL_ERROR`. Cleanup via `client.disconnect()` in `finally` is unchanged.

**Consequences.** A proxy is marked working only when Telegram answered `help.getConfig` through it. User authorization is never the connectivity criterion.

---

### D-038 · Deterministic v1 scoring; no schema change; no score lease column

**Context.** Task 005 converts persisted `ProxyObservation` rows into quality snapshots. `proxy_scores` already exists as append-only history (D-022) with `score`, windowed `reliability_*`, `latency_p{50,95}_ms`, `sample_count_*`, and `scoring_version`. `ProxyObservation.success` is the Task 004.1 API-verification bit.

**Decision — no migration.** Existing columns already store what ranking and reporting need. Intermediate components (`reliability_score`, `latency_score`, `confidence_score`) are computed in process and are recoverable from observations, so they are not duplicated on the table.

**Decision — formula (`scoring_version = v1`).** Pure function, no I/O, no RNG. 24 h lookback. Recency `w = exp(-ln(2) * age_hours / 6)` (true half-life 6 h: several tester intervals; not a fitted optimum). Reliability is Laplace-smoothed decay-weighted success rate. Latency uses successful `mtproto_connect_ms` only, linearly mapped so 0 ms → 100 and 8000 ms (tester MTProto timeout) → 0. Telethon's ~2 s floor is **not** subtracted. Timeouts are not turned into millisecond values. Confidence `n / (n + 10)` so `1/1` cannot outrank `100/100`. Final:

```
score = (0.75 * reliability_score + 0.25 * latency_score) * n / (n + 10)
```

Constants live in code, not env, so two workers cannot silently fork v1. Only `SCORER_BATCH_SIZE` is a setting.

**Decision — claiming.** A proxy is due when `is_active`, `last_test_finished_at` is set, and no v1 `ProxyScore` has `calculated_at >= last_test_finished_at`. Workers `SELECT … FOR UPDATE SKIP LOCKED`. Scoring has **no network I/O**, so the row lock is held only for the short read-compute-insert transaction. No `score_lock_until` / `next_score_at` columns, and `test_lock_until` is not reused (that lease belongs to the tester). A crash rolls back; another worker can take the row immediately.

**Decision — no ML / no LLM.** Scoring is an engineering metric over measurements. Qwen stays out of this path.

**Consequences.** Observations remain immutable. Each run inserts a new snapshot. Empty 24 h windows persist `score = 0` with NULL window reliabilities so the proxy is not claimed again until the next test.

---

### D-039 · Task 005 audit: keep v1 formula; pin as-of time; document concurrency

**Context.** Adversarial audit of `8dc15ec`. The brief asked whether Laplace + `n/(n+10)` is too conservative (1/1 cannot outrank 100/100) and whether `mtproto_connect_ms` should have the Telethon ~2 s floor subtracted.

**Decision — formula unchanged.** Conservatism is intentional (D-038): one GetConfig success is not a proven proxy. Confidence is sample-size only, so `1/1` and `0/1` share `1/11` while reliability still separates them. Subtracting ~2000 ms from latency, or swapping in `tcp_connect_ms`, would fabricate a network figure the tester did not store. `mtproto_connect_ms` remains Phase-3 wall time (client construction + `connect()` + GetConfig), not TCP RTT.

**Decision — `ScoreBreakdown.calculated_at` is required.** The dataclass had `default_factory=utcnow`, which would silently stamp the wall clock if a caller omitted it. The calculator already injects `now`; the default was a footgun, not a feature.

**Decision — no UNIQUE on score generations.** Duplicate snapshots of one `(proxy, last_test_finished_at)` generation are prevented by `FOR UPDATE SKIP LOCKED` plus the claim predicate, matching D-022 (append-only, no unique `proxy_id`). Scorer does not write tester scheduling columns.

**Consequences.** Recency weights (0.5 @ 6 h, 0.25 @ 12 h, 0.0625 @ 24 h) and the confidence matrix are pinned in unit tests. Docs name `now` as the as-of timestamp. No schema change.

---

### D-040 · Ranking is a read-only first page over latest v1 scores; no HTTP framework

**Context.** Task 006 needs a deterministic serving contract on append-only `ProxyScore` snapshots. D-016 forbids FastAPI/Redis. D-022 already indexed "latest score per proxy" as `ix_proxy_scores_proxy_id_calculated_at`.

**Decision — no web framework in Task 006.** `RankingService.list_top` is the serving API. Introducing FastAPI here would reverse D-016 for no consumer. **HTTP transport is D-041** (Task 007): a separate FastAPI process *calls* `list_top`; it does not replace this module.

**Decision — latest = max `(calculated_at, id)` among `scoring_version = v1`.** PostgreSQL `DISTINCT ON (proxy_id) ORDER BY proxy_id, calculated_at DESC, id DESC`. One listing per proxy. An unserviceable latest row (stale, empty window) does **not** fall back to an older snapshot.

**Decision — eligibility is not a second health score.** Active + v1 + `sample_count_24h > 0` + `calculated_at >= as_of - 24h`. Failed-only histories remain eligible and rank at the bottom via the Task 005 score. 24 h matches v1 lookback (D-038): a snapshot older than its own window is not current enough to serve. Constant in code, not env. `as_of` is injected.

**Decision — order.** `score DESC, calculated_at DESC, proxy_id ASC`. No random, no hash, no wall clock in the sort.

**Decision — pagination.** Default 20, max 100, reject otherwise. No OFFSET. No keyset cursor yet: there is no public transport and no extra HMAC secret in the stack. SQL `LIMIT` after the latest/eligibility filters.

**Decision — no migration.** The existing composite index serves DISTINCT ON. Listings omit secret, fingerprint, and URLs; `secret_type` is `legacy`/`dd`/`ee`/`unknown`.

**Consequences.** Ranking never writes proxies, observations, or scores. Tests pin latest-snapshot selection, ties, freshness boundaries, and secret absence in `repr`/JSON/logs.

---

## Deferred to their own tasks

| Item | Task |
|---|---|
| ~~PostgreSQL models, indexes, Alembic migrations, fingerprint uniqueness~~ | **002 — delivered** |
| ~~`MTProtoProxy` domain type, link parsing, deterministic fingerprinting, discovery layer~~ | **003 — delivered** |
| `SourceFetcher` abstraction, source health metadata — and with it the D-034 question of whether `ProxySource` becomes a first-class table instead of inline provenance on `proxy_discoveries` | 004, 018 |
| Telethon transport wrapper, three-phase test, error taxonomy, resource safety | 005 |
| Claim primitive with `FOR UPDATE SKIP LOCKED` shipped in 002 (D-024); bounded concurrency and worker wiring remain | 006, 014 |
| Observation *schema* shipped in 002; write path and retention remain | 007 |
| Scoring formula and confidence adjustment | 008, 009 |
| Reporting, Telegram publishing | 010, 011 |
| `ContentProvider` abstraction, Qwen, deterministic fallback template | 012, 024 |
| Remaining configuration surface (tester timeouts, scoring interval, Telegram, Qwen) | 013 |
| systemd units, Docker Compose for Postgres | 020, 021 |
| Counter metrics (`proxies_discovered`, `tests_success`, …) | 022 |
