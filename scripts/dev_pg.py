#!/usr/bin/env python3
"""Provision a throwaway local PostgreSQL for development and integration tests.

Why this exists
---------------
The brief for Task 002 says Docker must not become a requirement for local
development, and the integration suite must genuinely run rather than silently
skip. This script starts a real PostgreSQL server from a self-contained wheel,
creates the development and test databases, and can run an arbitrary command with
``DATABASE_URL`` / ``TEST_DATABASE_URL`` already exported.

``pgserver`` is **not** a project dependency. It is fetched ad hoc, exactly like
the spike's ``uv run --with telethon``:

    uv run --with pgserver python scripts/dev_pg.py up
    uv run --with pgserver python scripts/dev_pg.py run -- uv run pytest -m integration
    uv run --with pgserver python scripts/dev_pg.py down

Any other PostgreSQL works too -- this is a convenience, not an architecture.
Point ``DATABASE_URL`` at Supabase/Neon/RDS/a system package and skip this file
entirely.

The cluster lives outside the repository by default (see ``_default_pgdata``), so
nothing here adds files to the working tree.
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent


def _default_pgdata() -> Path:
    """Where the throwaway cluster lives.

    Deliberately **outside** the repository. A PostgreSQL data directory is
    thousands of small files; putting it in the working tree pollutes status
    output, editors and any artifact upload, and `.gitignore` alone does not stop
    those. Override with ``MTPROTO_DEVPG_DATA`` or ``--pgdata``.
    """
    override = os.environ.get("MTPROTO_DEVPG_DATA")
    if override:
        return Path(override).expanduser() / "pgdata"
    cache = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(cache) / "mtproto-platform" / "devpg" / "pgdata"


DEFAULT_PGDATA = _default_pgdata()

#: Non-superuser role, so migrations and tests exercise real privileges rather
#: than silently relying on superuser rights that production will not grant.
DEV_ROLE = "mtproto"
DEV_PASSWORD = "mtproto"  # noqa: S105 - throwaway local dev credential, not a secret
DEV_DATABASE = "mtproto"
TEST_DATABASE = "mtproto_test"

_PSQL: Path | None = None


def _pgserver() -> Any:
    try:
        # An optional, ad-hoc dependency fetched with `uv run --with pgserver`.
        # It is never installed in the project environment, so mypy cannot see it.
        import pgserver  # type: ignore[import-not-found]
    except ImportError:
        print(
            "pgserver is not installed. It is deliberately not a project dependency.\n"
            "Run this script through uv so it is fetched for this invocation only:\n\n"
            "    uv run --with pgserver python scripts/dev_pg.py up\n",
            file=sys.stderr,
        )
        raise SystemExit(2) from None
    return pgserver


def _psql_binary() -> Path:
    """Locate the ``psql`` that ships inside the pgserver wheel.

    ``pgserver`` exposes its binaries as callables rather than paths, so the
    install tree is derived from the package location.
    """
    global _PSQL
    if _PSQL is not None:
        return _PSQL
    module = _pgserver()
    candidate = Path(module.__file__).resolve().parent / "pginstall" / "bin" / "psql"
    if not candidate.exists():
        msg = f"could not locate psql inside pgserver (expected {candidate})"
        raise RuntimeError(msg)
    _PSQL = candidate
    return _PSQL


def _server(pgdata: Path) -> Any:
    return _pgserver().get_server(str(pgdata), cleanup_mode=None)


def _psql(uri: str, sql: str) -> str:
    """Run SQL with psql. Returns stdout; raises on failure.

    No ``-d`` flag: passing one after a libpq URI makes psql drop the URI's query
    parameters, which is where the Unix-socket ``host=`` lives (verified
    empirically -- the connection then falls back to ``/tmp/.s.PGSQL.5432``). The
    target database is encoded in the URI instead.
    """
    cmd: list[str] = [str(_psql_binary()), uri, "-v", "ON_ERROR_STOP=1", "-Atc", sql]
    # S603: a fixed executable with fixed arguments, never a shell and never
    # derived from untrusted input.
    completed = subprocess.run(cmd, capture_output=True, text=True, check=False)  # noqa: S603
    if completed.returncode != 0:
        msg = f"psql failed ({completed.returncode}): {completed.stderr.strip()}"
        raise RuntimeError(msg)
    return str(completed.stdout.strip())


def _uri_for(uri: str, database: str) -> str:
    """Rewrite the database component of a libpq URI, keeping query params."""
    from urllib.parse import urlsplit, urlunsplit

    return urlunsplit(urlsplit(uri)._replace(path=f"/{database}"))


def _database_exists(uri: str, database: str) -> bool:
    quoted = database.replace("'", "''")
    # S608: a literal value cannot be a bind parameter here, because psql -Atc
    # receives one string. The name comes from a constant in this file and is
    # single-quote escaped, never from user input.
    query = f"SELECT count(*) FROM pg_database WHERE datname = '{quoted}'"  # noqa: S608
    return _psql(_uri_for(uri, "postgres"), query) != "0"


def _role_exists(uri: str, role: str) -> bool:
    quoted = role.replace("'", "''")
    query = f"SELECT count(*) FROM pg_roles WHERE rolname = '{quoted}'"  # noqa: S608
    return _psql(_uri_for(uri, "postgres"), query) != "0"


def _dsn(socket_dir: Path, database: str) -> str:
    """A SQLAlchemy asyncpg DSN for a Unix-socket server.

    asyncpg takes the socket directory as the ``host`` query parameter; there is
    no TCP listener, which also means the cluster is unreachable from outside
    this machine.
    """
    return f"postgresql+asyncpg://{DEV_ROLE}:{DEV_PASSWORD}@/{database}?host={socket_dir}"


def _provision(pgdata: Path) -> tuple[str, str]:
    # pgserver refuses to init a cluster whose parent directory is missing.
    pgdata.parent.mkdir(parents=True, exist_ok=True)
    server = _server(pgdata)
    base_uri = server.get_uri()  # superuser, socket-based
    admin_uri = _uri_for(base_uri, "postgres")
    socket_dir = Path(pgdata).resolve()

    if not _role_exists(admin_uri, DEV_ROLE):
        quoted = DEV_PASSWORD.replace("'", "''")
        # CREATEDB is granted so the integration suite can create and drop its
        # own throwaway databases (the migration lifecycle test DROPs every
        # table, so it must not share a database with anything else).
        _psql(admin_uri, f"CREATE ROLE {DEV_ROLE} LOGIN CREATEDB PASSWORD '{quoted}'")
        print(f"created role {DEV_ROLE}")
    else:
        # Idempotent: an existing cluster provisioned before CREATEDB was needed
        # is upgraded in place rather than requiring a wipe.
        _psql(admin_uri, f"ALTER ROLE {DEV_ROLE} CREATEDB")
        print(f"role {DEV_ROLE} already exists (ensured CREATEDB)")

    for database in (DEV_DATABASE, TEST_DATABASE):
        if _database_exists(admin_uri, database):
            print(f"database {database} already exists")
            continue
        _psql(admin_uri, f"CREATE DATABASE {database} OWNER {DEV_ROLE}")
        print(f"created database {database} (owner {DEV_ROLE})")

    return _dsn(socket_dir, DEV_DATABASE), _dsn(socket_dir, TEST_DATABASE)


def cmd_up(pgdata: Path) -> int:
    dev_url, test_url = _provision(pgdata)
    version = _psql(
        test_url.replace("postgresql+asyncpg://", "postgresql://"), "SHOW server_version"
    )
    print(f"\nPostgreSQL {version} ready at {pgdata}")
    print("\nExport into your shell with:")
    print(f"  export DATABASE_URL={shlex.quote(dev_url)}")
    print(f"  export TEST_DATABASE_URL={shlex.quote(test_url)}")
    print("\nOr let this script set them for a single command:")
    print("  uv run --with pgserver python scripts/dev_pg.py run -- uv run pytest -m integration")
    return 0


def cmd_run(pgdata: Path, command: list[str]) -> int:
    if not command:
        print("nothing to run; pass a command after --", file=sys.stderr)
        return 2
    dev_url, test_url = _provision(pgdata)
    env = {**os.environ, "DATABASE_URL": dev_url, "TEST_DATABASE_URL": test_url}
    print(f"$ {' '.join(shlex.quote(part) for part in command)}", flush=True)
    # S603: running an operator-supplied command IS this subcommand's whole
    # purpose. No shell is involved and every argument is passed separately.
    return subprocess.run(command, env=env, check=False).returncode  # noqa: S603


def cmd_down(pgdata: Path, *, delete: bool) -> int:
    if not pgdata.exists():
        print(f"nothing to do: {pgdata} does not exist")
        return 0
    try:
        server = _server(pgdata)
    except Exception as exc:
        print(f"could not attach to the cluster ({exc}); leaving files in place", file=sys.stderr)
        return 1
    try:
        server.cleanup()
        print("stopped PostgreSQL")
    except Exception as exc:
        print(f"stop failed: {exc}", file=sys.stderr)
    if delete:
        import shutil

        shutil.rmtree(pgdata, ignore_errors=True)
        print(f"deleted {pgdata}")
    return 0


def cmd_status(pgdata: Path) -> int:
    if not pgdata.exists():
        print(f"not provisioned ({pgdata} absent)")
        return 1
    try:
        dev_url, test_url = _provision(pgdata)
    except Exception as exc:
        print(f"unhealthy: {exc}", file=sys.stderr)
        return 1
    print("healthy")
    print(f"  DATABASE_URL      = {dev_url}")
    print(f"  TEST_DATABASE_URL = {test_url}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--pgdata", type=Path, default=DEFAULT_PGDATA, help="cluster data directory"
    )
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("up", help="start the cluster and create the dev/test databases")
    sub.add_parser("status", help="report cluster health and the DSNs")
    down = sub.add_parser("down", help="stop the cluster")
    down.add_argument("--delete", action="store_true", help="also delete the data directory")
    run = sub.add_parser("run", help="run a command with DATABASE_URL/TEST_DATABASE_URL exported")
    run.add_argument("command", nargs=argparse.REMAINDER)

    args = parser.parse_args(argv)
    pgdata: Path = args.pgdata

    if args.action == "up":
        return cmd_up(pgdata)
    if args.action == "status":
        return cmd_status(pgdata)
    if args.action == "down":
        return cmd_down(pgdata, delete=args.delete)

    command = [part for part in args.command if part != "--"]
    return cmd_run(pgdata, command)


if __name__ == "__main__":
    raise SystemExit(main())
