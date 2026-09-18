"""Worker process entrypoints.

Independent OS processes live here. They must never be merged into a
single long-running asyncio application: a crash, ``kill -9`` or OOM of one
worker must not affect the others. Coordination happens exclusively through
PostgreSQL, not through shared Python state.

======================  =========================================
Console script          Module
======================  =========================================
``mtproto-discovery``   :mod:`workers.discovery`
``mtproto-tester``      :mod:`workers.tester`
``mtproto-scorer``      :mod:`workers.scorer`
``mtproto-publisher``   :mod:`workers.publisher`
``mtproto-api``         :mod:`workers.api`
======================  =========================================
"""

from __future__ import annotations

__all__: list[str] = []
