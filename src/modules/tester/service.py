"""Tester service coordinating concurrency, claiming, execution, and observation logging.

Architectural invariants:
1. Short transactions: claiming and observation recording take brief transactions.
   Actual network probing occurs strictly OUTSIDE any database transaction.
2. Bounded concurrency: governed by an asyncio.Semaphore to prevent socket exhaustion.
3. Leases are cleared on test completion so expired locks can self-heal.
4. Observations are append-only and never deleted or overwritten.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import timedelta

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import Database
from core.logger import get_logger
from core.models import DEFAULT_LEASE_SECONDS, Proxy, ProxyObservation, utcnow
from modules.discovery.normalizer import validate_and_parse_secret
from modules.scheduling import claim_due_proxies
from modules.tester.models import TesterResult
from modules.tester.probe import probe_proxy

__all__ = ["TesterService"]

_logger = get_logger("modules.tester.service")


class TesterService:
    """Orchestrates MTProto proxy testing rounds with bounded concurrency."""

    def __init__(
        self,
        db: Database,
        *,
        api_id: int | None = None,
        api_hash: str | None = None,
        concurrency: int = 10,
        tcp_timeout_seconds: float = 3.0,
        mtproto_timeout_seconds: float = 8.0,
        total_timeout_seconds: float = 15.0,
        batch_size: int = 25,
    ) -> None:
        self.db = db
        self.api_id = api_id
        self.api_hash = api_hash
        self.concurrency = concurrency
        self.tcp_timeout_seconds = tcp_timeout_seconds
        self.mtproto_timeout_seconds = mtproto_timeout_seconds
        self.total_timeout_seconds = total_timeout_seconds
        self.batch_size = batch_size
        self._semaphore = asyncio.Semaphore(concurrency)

    async def test_proxy(self, proxy: Proxy) -> TesterResult:
        """Run a 3-phase connectivity probe on a single Proxy instance under concurrency bounds."""
        raw_secret = proxy.secret.reveal()

        # Identify secret type from secret format
        try:
            _, secret_type, _ = validate_and_parse_secret(raw_secret)
        except Exception:
            from modules.discovery.models import SecretType

            secret_type = SecretType.LEGACY

        async with self._semaphore:
            return await probe_proxy(
                proxy_id=proxy.id,
                server=proxy.server,
                port=proxy.port,
                secret=raw_secret,
                secret_type=secret_type,
                api_id=self.api_id,
                api_hash=self.api_hash,
                tcp_timeout_seconds=self.tcp_timeout_seconds,
                mtproto_timeout_seconds=self.mtproto_timeout_seconds,
                total_timeout_seconds=self.total_timeout_seconds,
            )

    async def record_result(self, session: AsyncSession, result: TesterResult) -> None:
        """Persist a single test observation and update proxy scheduling state."""
        now = utcnow()
        error_cat_str = str(result.error_category) if result.error_category else None

        # 1. Insert append-only observation record
        obs_insert = pg_insert(ProxyObservation).values(
            proxy_id=result.proxy_id,
            observed_at=now,
            success=result.success,
            tcp_connect_ms=result.tcp_connect_ms,
            mtproto_connect_ms=result.mtproto_connect_ms,
            total_latency_ms=result.total_latency_ms,
            error_category=error_cat_str,
            error_message_safe=result.error_message_safe,
            tester_version="v1",
        )
        await session.execute(obs_insert)

        # 2. Update proxy state and schedule next test time
        next_due = now + (timedelta(hours=1) if result.success else timedelta(minutes=15))
        update_values: dict[str, object] = {
            "last_test_finished_at": now,
            "last_error_category": error_cat_str,
            "test_lock_until": None,  # Release claim lease
            "next_test_at": next_due,
        }
        if result.success:
            update_values["last_success_at"] = now
        else:
            update_values["last_failure_at"] = now

        await session.execute(
            update(Proxy)
            .where(Proxy.id == result.proxy_id)
            .values(**update_values)
        )

    async def run_batch(self) -> list[TesterResult]:
        """Claim a batch of due proxies, test them concurrently, and record outcomes."""
        # Transaction 1: Claim due proxies atomically
        claimed_proxies: list[Proxy] = []
        async with self.db.session_scope() as session:
            claimed_proxies = await claim_due_proxies(
                session,
                limit=self.batch_size,
                lease_seconds=DEFAULT_LEASE_SECONDS,
            )

        if not claimed_proxies:
            return []

        _logger.info("tester_batch_claimed", count=len(claimed_proxies))

        # Network Probing: strictly OUTSIDE database transactions
        tasks = [self.test_proxy(p) for p in claimed_proxies]
        results: Sequence[TesterResult] = await asyncio.gather(*tasks, return_exceptions=False)

        # Transaction 2: Record observations and update scheduling
        async with self.db.session_scope() as session:
            for res in results:
                await self.record_result(session, res)

        _logger.info(
            "tester_batch_completed",
            total=len(results),
            successes=sum(1 for r in results if r.success),
        )
        return list(results)
