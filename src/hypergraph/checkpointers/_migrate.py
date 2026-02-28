"""Schema management for checkpointer databases."""

from __future__ import annotations

from typing import Any

SCHEMA_VERSION = 2


def detect_schema_version(conn: Any) -> int:
    """Return the schema version of an existing database.

    Returns:
        0 — empty database (no tables)
        2 — current v2 schema (_schema_version table with version=2)
    """
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

    if "_schema_version" in tables:
        row = conn.execute("SELECT version FROM _schema_version").fetchone()
        return row[0] if row else 0

    return 0


def create_v2_schema(conn: Any) -> None:
    """Create a fresh v2 schema on an empty database."""
    conn.execute(_CREATE_RUNS)
    conn.execute(_CREATE_STEPS)
    _create_v2_indexes(conn)
    _create_fts(conn)

    conn.execute("CREATE TABLE IF NOT EXISTS _schema_version (version INTEGER NOT NULL)")
    conn.execute("INSERT INTO _schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
    conn.commit()


def ensure_schema(conn: Any) -> None:
    """Detect schema version and create schema if the database is empty."""
    version = detect_schema_version(conn)

    if version == SCHEMA_VERSION:
        return
    if version == 0:
        create_v2_schema(conn)
    else:
        raise ValueError(f"Unsupported database schema version {version} (current: {SCHEMA_VERSION}). Please upgrade hypergraph.")


# === SQL Definitions ===

_CREATE_RUNS = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    graph_name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    duration_ms REAL,
    node_count INTEGER DEFAULT 0,
    error_count INTEGER DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    completed_at TEXT,
    parent_run_id TEXT REFERENCES runs(id),
    config TEXT
)
"""

_CREATE_STEPS = """
CREATE TABLE IF NOT EXISTS steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(id),
    step_index INTEGER NOT NULL,
    superstep INTEGER NOT NULL,
    node_name TEXT NOT NULL,
    node_type TEXT,
    status TEXT NOT NULL,
    duration_ms REAL NOT NULL DEFAULT 0.0,
    cached INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    decision TEXT,
    input_versions TEXT,
    values_data BLOB,
    child_run_id TEXT REFERENCES runs(id),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    completed_at TEXT,
    UNIQUE(run_id, superstep, node_name)
)
"""


def _create_v2_indexes(conn: Any) -> None:
    """Create indexes for common CLI query patterns."""
    conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_graph ON runs(graph_name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_steps_run ON steps(run_id, step_index)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_steps_node ON steps(node_name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_steps_status ON steps(status)")


def _create_fts(conn: Any) -> None:
    """Create FTS5 virtual table and sync triggers for full-text search."""
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS steps_fts USING fts5(
            node_name, error, content='steps', content_rowid='id'
        )
    """)

    # Auto-sync triggers
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS steps_fts_insert AFTER INSERT ON steps BEGIN
            INSERT INTO steps_fts(rowid, node_name, error)
            VALUES (new.id, new.node_name, new.error);
        END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS steps_fts_update AFTER UPDATE ON steps BEGIN
            INSERT INTO steps_fts(steps_fts, rowid, node_name, error)
            VALUES ('delete', old.id, old.node_name, old.error);
            INSERT INTO steps_fts(rowid, node_name, error)
            VALUES (new.id, new.node_name, new.error);
        END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS steps_fts_delete AFTER DELETE ON steps BEGIN
            INSERT INTO steps_fts(steps_fts, rowid, node_name, error)
            VALUES ('delete', old.id, old.node_name, old.error);
        END
    """)
