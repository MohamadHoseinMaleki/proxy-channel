"""Tests for the three worker process entrypoints.

What is asserted here:

* each worker module exposes ``WORKER_NAME``, ``tick`` and ``main``;
* the three processes are genuinely independent (no cross-imports, no shared
  runtime object);
* implemented workers warn and return when PostgreSQL is unreachable;
* the declared console scripts match the modules.
"""

from __future__ import annotations

import ast
import importlib
import json
import pathlib
from typing import Any

import pytest

from core.config import Settings
from core.lifecycle import WorkerLifecycle, run_worker

from .conftest import make_settings

WORKER_MODULES = ("workers.discovery", "workers.tester", "workers.scorer")

EXPECTED_WORKER_NAMES = {
    "workers.discovery": "discovery-worker",
    "workers.tester": "tester-worker",
    "workers.scorer": "scoring-worker",
}

ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture
def fast_settings() -> Settings:
    return make_settings(
        worker_poll_interval_seconds=0.001,
        heartbeat_interval_seconds=0.0,
    )


def load(name: str) -> Any:
    return importlib.import_module(name)


def module_imports(module: Any) -> set[str]:
    """Top-level package names imported by a module's source file."""
    source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


class TestEntrypointContract:
    @pytest.mark.parametrize("name", WORKER_MODULES)
    def test_exposes_required_attributes(self, name: str) -> None:
        module = load(name)
        assert hasattr(module, "WORKER_NAME")
        assert hasattr(module, "tick")
        assert hasattr(module, "main")
        assert callable(module.tick)
        assert callable(module.main)

    @pytest.mark.parametrize("name", WORKER_MODULES)
    def test_worker_name_matches_specification(self, name: str) -> None:
        actual = load(name).WORKER_NAME
        expected = EXPECTED_WORKER_NAMES[name]
        assert actual == expected

    def test_worker_names_are_distinct(self) -> None:
        names = [load(name).WORKER_NAME for name in WORKER_MODULES]
        assert len(set(names)) == 3

    @pytest.mark.parametrize("name", WORKER_MODULES)
    def test_module_is_runnable_as_a_script(self, name: str) -> None:
        source = pathlib.Path(load(name).__file__).read_text(encoding="utf-8")
        assert '__name__ == "__main__"' in source
        assert "main()" in source


class TestProcessIndependence:
    @pytest.mark.parametrize("name", WORKER_MODULES)
    def test_worker_does_not_import_other_workers(self, name: str) -> None:
        """Importing one worker must never drag in another process's code."""
        assert "workers" not in module_imports(load(name))

    @pytest.mark.parametrize("name", WORKER_MODULES)
    def test_workers_share_no_mutable_state(self, name: str) -> None:
        """Two lifecycles for the same worker must not share counters."""
        module = load(name)
        settings = make_settings(worker_poll_interval_seconds=0.001, heartbeat_interval_seconds=0.0)
        first = WorkerLifecycle(module.WORKER_NAME, settings=settings)
        second = WorkerLifecycle(module.WORKER_NAME, settings=settings)
        first.tick_count = 99
        assert second.tick_count == 0


class TestTickContract:
    @pytest.mark.parametrize("name", WORKER_MODULES)
    async def test_tick_works_outside_the_lifecycle_context(
        self, name: str, fast_settings: Settings
    ) -> None:
        """A bare tick must not depend on bound contextvars or installed handlers."""
        module = load(name)
        life = WorkerLifecycle(module.WORKER_NAME, settings=fast_settings)
        await module.tick(life)
        assert life.failure_count == 0


class TestWorkerRunIntegration:
    @pytest.mark.parametrize("name", WORKER_MODULES)
    async def test_runs_and_stops_cleanly(self, name: str, fast_settings: Settings) -> None:
        module = load(name)
        ticks = 0

        async def tick(life: WorkerLifecycle) -> None:
            nonlocal ticks
            await module.tick(life)
            ticks += 1
            if ticks >= 2:
                life.request_shutdown("test-complete")

        assert await run_worker(module.WORKER_NAME, tick, settings=fast_settings, interval=0.0) == 0
        assert ticks == 2


class TestDiscoveryWorker:
    async def test_discovery_worker_tick_when_db_unreachable(
        self, fast_settings: Settings, json_logs: pytest.CaptureFixture[str]
    ) -> None:
        """When database is unreachable, discovery warns and exits cleanly."""
        import workers.discovery as discovery_module

        async with WorkerLifecycle(discovery_module.WORKER_NAME, settings=fast_settings) as life:
            await discovery_module.tick(life)

        records = [
            json.loads(line) for line in json_logs.readouterr().out.splitlines() if line.strip()
        ]
        warn_records = [r for r in records if r["event"] == "discovery_tick_db_unreachable"]
        assert len(warn_records) == 1
        assert warn_records[0]["worker"] == "discovery-worker"
        assert life.failure_count == 0

    async def test_empty_sources_is_an_honest_tick_not_fake_work(
        self, fast_settings: Settings, json_logs: pytest.CaptureFixture[str]
    ) -> None:
        from unittest.mock import AsyncMock, patch

        import workers.discovery as discovery_module

        fake_db = AsyncMock()
        fake_db.is_reachable = AsyncMock(return_value=True)
        fake_db.dispose = AsyncMock()

        async with WorkerLifecycle(discovery_module.WORKER_NAME, settings=fast_settings) as life:
            with patch("workers.discovery.Database.from_settings", return_value=fake_db):
                await discovery_module.tick(life, db=fake_db)

        records = [
            json.loads(line) for line in json_logs.readouterr().out.splitlines() if line.strip()
        ]
        ticks = [r for r in records if r["event"] == "discovery_tick"]
        assert len(ticks) == 1
        record = ticks[0]
        assert record["implemented"] is True
        assert record["sources_processed"] == 0
        assert record["proxies_found"] == 0
        assert record["new_proxies"] == 0

    async def test_invalid_source_is_skipped_not_fatal(
        self, json_logs: pytest.CaptureFixture[str]
    ) -> None:
        from unittest.mock import AsyncMock, patch

        import workers.discovery as discovery_module

        settings = make_settings(
            worker_poll_interval_seconds=0.001,
            heartbeat_interval_seconds=0.0,
            discovery_sources="telegram:../etc; telegram:ProxyList",
        )
        fake_db = AsyncMock()
        fake_db.is_reachable = AsyncMock(return_value=True)
        fake_db.dispose = AsyncMock()

        harvest = AsyncMock()
        harvest.return_value.sources_attempted = 1
        harvest.return_value.source_failures = 0
        harvest.return_value.total_candidates = 0
        harvest.return_value.new_proxies = 0
        harvest.return_value.updated_proxies = 0
        harvest.return_value.discoveries_recorded = 0

        async with WorkerLifecycle(discovery_module.WORKER_NAME, settings=settings) as life:
            with (
                patch("workers.discovery.Database.from_settings", return_value=fake_db),
                patch("workers.discovery.DiscoveryService.harvest", harvest),
            ):
                await discovery_module.tick(life, db=fake_db)

        records = [
            json.loads(line) for line in json_logs.readouterr().out.splitlines() if line.strip()
        ]
        invalid = [r for r in records if r["event"] == "discovery_source_invalid"]
        assert len(invalid) == 1
        harvest.assert_awaited_once()
        assert harvest.await_args is not None
        called = harvest.await_args.args
        sources = called[1] if len(called) > 1 else called[0]
        assert len(sources) == 1
        assert sources[0].source_name == "@ProxyList"


class TestTesterWorker:
    async def test_tester_worker_tick_when_db_unreachable(
        self, fast_settings: Settings, json_logs: pytest.CaptureFixture[str]
    ) -> None:
        """When database is unreachable, tester worker warns and exits cleanly without error."""
        import workers.tester as tester_module

        async with WorkerLifecycle(tester_module.WORKER_NAME, settings=fast_settings) as life:
            await tester_module.tick(life)

        records = [
            json.loads(line) for line in json_logs.readouterr().out.splitlines() if line.strip()
        ]
        warn_records = [r for r in records if r["event"] == "tester_tick_db_unreachable"]
        assert len(warn_records) == 1
        assert warn_records[0]["worker"] == "tester-worker"
        assert life.failure_count == 0


class TestScorerWorker:
    async def test_scorer_worker_tick_when_db_unreachable(
        self, fast_settings: Settings, json_logs: pytest.CaptureFixture[str]
    ) -> None:
        """When database is unreachable, scorer warns and exits cleanly without error."""
        import workers.scorer as scorer_module

        async with WorkerLifecycle(scorer_module.WORKER_NAME, settings=fast_settings) as life:
            await scorer_module.tick(life)

        records = [
            json.loads(line) for line in json_logs.readouterr().out.splitlines() if line.strip()
        ]
        warn_records = [r for r in records if r["event"] == "scorer_tick_db_unreachable"]
        assert len(warn_records) == 1
        assert warn_records[0]["worker"] == "scoring-worker"
        assert life.failure_count == 0

    async def test_scoring_exception_does_not_kill_the_worker(self) -> None:
        from unittest.mock import AsyncMock, patch

        import workers.scorer as scorer_module
        from core.lifecycle import run_worker

        settings = make_settings(
            worker_poll_interval_seconds=0.001,
            heartbeat_interval_seconds=0.0,
            worker_error_backoff_seconds=0.0,
            worker_max_error_backoff_seconds=0.0,
        )
        ticks = 0

        async def tick(life: WorkerLifecycle) -> None:
            nonlocal ticks
            ticks += 1
            if ticks >= 2:
                life.request_shutdown("test-complete")
                return
            fake_db = AsyncMock()
            fake_db.is_reachable = AsyncMock(return_value=True)
            fake_db.dispose = AsyncMock()
            with (
                patch("workers.scorer.Database.from_settings", return_value=fake_db),
                patch(
                    "workers.scorer.ScoringService.run_batch",
                    side_effect=RuntimeError("scoring boom"),
                ),
            ):
                await scorer_module.tick(life)

        assert (
            await run_worker(scorer_module.WORKER_NAME, tick, settings=settings, interval=0.0) == 0
        )
        assert ticks == 2


class TestConsoleScripts:
    def test_pyproject_declares_one_script_per_worker(self) -> None:
        import tomllib

        pyproject = ROOT / "pyproject.toml"
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        scripts: dict[str, str] = data["project"]["scripts"]

        assert scripts == {
            "mtproto-discovery": "workers.discovery:main",
            "mtproto-tester": "workers.tester:main",
            "mtproto-scorer": "workers.scorer:main",
            "mtproto-api": "workers.api:main",
        }

    def test_api_process_is_not_a_tick_worker(self) -> None:
        """Uvicorn owns signals; merging the API into WorkerLifecycle races D-006."""
        module = load("workers.api")
        imported = module_imports(module)
        assert "uvicorn" in imported
        assert "core.lifecycle" not in imported
        source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom) and node.module and "lifecycle" in node.module:
                msg = "API process must not import WorkerLifecycle"
                raise AssertionError(msg)
        assert "run_worker" not in source
        assert "uvicorn.run" in source
        assert "access_log=False" in source

    def test_every_declared_script_resolves(self) -> None:
        import tomllib

        data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        for target in data["project"]["scripts"].values():
            module_name, _, attr = target.partition(":")
            module = load(module_name)
            assert callable(getattr(module, attr)), target
