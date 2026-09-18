"""Publication counters: in-process API plus atomic PostgreSQL totals.

The 017 ``PublicationMetrics`` interface is unchanged. Persistence is a
separate, batched upsert so a failed metrics write cannot change a
publication outcome.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Final

from sqlalchemy import Select, select
from sqlalchemy.dialects.postgresql import Insert, insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import PublicationCounter, utcnow

__all__ = [
    "COUNTER_NAMES",
    "GLOBAL_CHANNEL_ID",
    "PublicationMetrics",
    "increment_counters_statement",
    "load_counters",
    "load_counters_statement",
    "persist_counter_deltas",
]

GLOBAL_CHANNEL_ID: Final = ""

COUNTER_NAMES: Final[tuple[str, ...]] = (
    "publications_scheduled_total",
    "publications_rejected_total",
    "publication_retries_total",
    "publication_failures_total",
    "publication_success_total",
    "telegram_rate_limits_total",
)

_KNOWN: Final[frozenset[str]] = frozenset(COUNTER_NAMES)


@dataclass(slots=True)
class PublicationMetrics:
    """Cumulative counters for one publisher process (or one test).

    Process-local totals stay on the instance (Task 017). ``drain_deltas``
    yields unflushed increments for a single atomic DB upsert.
    """

    publications_scheduled_total: int = 0
    publications_rejected_total: int = 0
    publication_retries_total: int = 0
    publication_failures_total: int = 0
    publication_success_total: int = 0
    telegram_rate_limits_total: int = 0
    _deltas: dict[str, int] = field(default_factory=dict, repr=False, compare=False)

    def inc_scheduled(self, n: int = 1) -> None:
        self.publications_scheduled_total += n
        self._bump("publications_scheduled_total", n)

    def inc_rejected(self, n: int = 1) -> None:
        self.publications_rejected_total += n
        self._bump("publications_rejected_total", n)

    def inc_retries(self, n: int = 1) -> None:
        self.publication_retries_total += n
        self._bump("publication_retries_total", n)

    def inc_failures(self, n: int = 1) -> None:
        self.publication_failures_total += n
        self._bump("publication_failures_total", n)

    def inc_success(self, n: int = 1) -> None:
        self.publication_success_total += n
        self._bump("publication_success_total", n)

    def inc_rate_limits(self, n: int = 1) -> None:
        self.telegram_rate_limits_total += n
        self._bump("telegram_rate_limits_total", n)

    def as_dict(self) -> dict[str, int]:
        return {
            "publications_scheduled_total": self.publications_scheduled_total,
            "publications_rejected_total": self.publications_rejected_total,
            "publication_retries_total": self.publication_retries_total,
            "publication_failures_total": self.publication_failures_total,
            "publication_success_total": self.publication_success_total,
            "telegram_rate_limits_total": self.telegram_rate_limits_total,
        }

    def drain_deltas(self) -> dict[str, int]:
        """Return and clear unflushed increments. Does not reset totals."""
        deltas = {name: n for name, n in self._deltas.items() if n > 0}
        self._deltas.clear()
        return deltas

    def _bump(self, name: str, n: int) -> None:
        if n <= 0:
            return
        self._deltas[name] = self._deltas.get(name, 0) + n


def increment_counters_statement(
    deltas: dict[str, int],
    *,
    channel_id: str | None = None,
    now: datetime | None = None,
) -> Insert | None:
    """``INSERT … ON CONFLICT DO UPDATE SET value = value + EXCLUDED.value``."""
    moment = now or utcnow()
    rows: list[dict[str, object]] = []
    scoped = (channel_id or "").strip()
    for name, n in deltas.items():
        if name not in _KNOWN or n <= 0:
            continue
        rows.append(
            {"name": name, "channel_id": GLOBAL_CHANNEL_ID, "value": int(n), "updated_at": moment}
        )
        if scoped:
            rows.append({"name": name, "channel_id": scoped, "value": int(n), "updated_at": moment})
    if not rows:
        return None
    statement = insert(PublicationCounter).values(rows)
    return statement.on_conflict_do_update(
        index_elements=["name", "channel_id"],
        set_={
            "value": PublicationCounter.value + statement.excluded.value,
            "updated_at": statement.excluded.updated_at,
        },
    )


def load_counters_statement(
    *,
    channel_id: str = GLOBAL_CHANNEL_ID,
) -> Select[tuple[str, int]]:
    """SELECT only. Used by health."""
    return select(PublicationCounter.name, PublicationCounter.value).where(
        PublicationCounter.channel_id == channel_id
    )


async def persist_counter_deltas(
    session: AsyncSession,
    deltas: dict[str, int],
    *,
    channel_id: str | None = None,
    now: datetime | None = None,
) -> None:
    """Atomically add ``deltas`` to stored totals. Empty input is a no-op."""
    statement = increment_counters_statement(deltas, channel_id=channel_id, now=now)
    if statement is None:
        return
    await session.execute(statement)


async def load_counters(
    session: AsyncSession,
    *,
    channel_id: str = GLOBAL_CHANNEL_ID,
) -> dict[str, int]:
    totals = dict.fromkeys(COUNTER_NAMES, 0)
    rows = (await session.execute(load_counters_statement(channel_id=channel_id))).all()
    for name, value in rows:
        if name in totals:
            totals[str(name)] = int(value)
    return totals
