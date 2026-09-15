# Decision Log

Every entry records a decision that constrains later work, why it was made, and
what evidence supports it. Newest first within a task. Superseded decisions are
kept with their replacement noted — this log is the project's memory.

Format: **ID · Decision · Context · Evidence · Consequences**

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
`telethon`, `pyrogram`, `asyncpg`.

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

No Redis. No Celery. No FastAPI. No Cloudflare Workers. No Kubernetes. No
microservice framework. No SOCKS5/HTTP/VLESS/VMess/Trojan/Xray/Shadowsocks —
**MTProto only**. PostgreSQL is the work-coordination mechanism
(`FOR UPDATE SKIP LOCKED`, Task 006/014).

**Rationale.** Stated in the brief and consistent with the core hypothesis being
about *measurement quality*, not infrastructure scale.

---

## Deferred to their own tasks

| Item | Task |
|---|---|
| PostgreSQL models, indexes, Alembic migrations, fingerprint uniqueness | 002 |
| `MTProtoProxy` domain type, link parsing, deterministic fingerprinting | 003 |
| `SourceFetcher` abstraction, source health metadata | 004, 018 |
| Telethon transport wrapper, three-phase test, error taxonomy, resource safety | 005 |
| Batch claiming with `FOR UPDATE SKIP LOCKED`, bounded concurrency | 006, 014 |
| Observation storage | 007 |
| Scoring formula and confidence adjustment | 008, 009 |
| Reporting, Telegram publishing | 010, 011 |
| `ContentProvider` abstraction, Qwen, deterministic fallback template | 012, 024 |
| Remaining configuration surface (tester timeouts, scoring interval, Telegram, Qwen) | 013 |
| systemd units, Docker Compose for Postgres | 020, 021 |
| Counter metrics (`proxies_discovered`, `tests_success`, …) | 022 |
