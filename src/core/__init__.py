"""Core package: configuration, structured logging and process lifecycle.

The three workers are independent OS processes. Nothing in this package may
create cross-process coupling (no shared singletons that assume one process,
no in-process task bus).
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
