"""Domain modules shared by the workers.

Populated by later tasks: ``parsers`` (Task 003), ``discovery`` (Task 004),
``protocol`` / ``mtproto_tester`` (Task 005), ``metrics`` (Task 008),
``reporting`` (Task 010), ``publisher`` (Task 011) and ``content`` (Task 012).

Modules here must stay free of worker-loop concerns: they are pure, testable
building blocks that the three processes compose.
"""

from __future__ import annotations

__all__: list[str] = []
