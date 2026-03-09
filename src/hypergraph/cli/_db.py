"""Database access helpers for CLI commands.

Creates a SqliteCheckpointer from a --db path for sync queries.
"""

from __future__ import annotations


def open_checkpointer(db: str | None = None):
    """Open a SqliteCheckpointer, resolving the path if not explicit."""
    from hypergraph.checkpointers import SqliteCheckpointer
    from hypergraph.cli._config import resolve_db_path

    return SqliteCheckpointer(resolve_db_path(db))


def open_run_inspector(db: str | None = None):
    """Open the default run inspection adapter for CLI queries."""
    from hypergraph.checkpointers import SqliteRunInspector

    return SqliteRunInspector(open_checkpointer(db))
