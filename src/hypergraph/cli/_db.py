"""Database access helpers for CLI commands.

Creates a SqliteCheckpointer from a --db path and provides async helpers.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any


def _require_aiosqlite() -> None:
    """Check that aiosqlite is available."""
    try:
        import aiosqlite  # noqa: F401
    except ImportError:
        print("Error: aiosqlite is required for the CLI. Install with: pip install hypergraph[checkpoint]", file=sys.stderr)
        raise SystemExit(1) from None


def run_async(coro: Any) -> Any:
    """Run an async coroutine from sync CLI context."""
    return asyncio.run(coro)


async def open_checkpointer(db: str):
    """Open a SqliteCheckpointer, initialize it, and return it."""
    from hypergraph.checkpointers import SqliteCheckpointer

    cp = SqliteCheckpointer(db)
    await cp.initialize()
    return cp
