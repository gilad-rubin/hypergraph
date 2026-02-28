"""Database access helpers for CLI commands.

Creates a SqliteCheckpointer from a --db path for sync queries.
"""

from __future__ import annotations


def open_checkpointer(db: str):
    """Open a SqliteCheckpointer for sync reads."""
    from hypergraph.checkpointers import SqliteCheckpointer

    return SqliteCheckpointer(db)
