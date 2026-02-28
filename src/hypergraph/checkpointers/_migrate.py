"""Schema migration for checkpointer databases.

Detects schema version and migrates from v1 (workflows/steps with BLOB)
to v2 (runs/steps with FTS5 and proper indexes).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("hypergraph.checkpointers")

SCHEMA_VERSION = 2


def detect_schema_version(conn: Any) -> int:
    """Detect the schema version of an existing database.

    Returns:
        0 — empty database (no tables)
        1 — v1 schema (workflows + steps tables, no _schema_version)
        2 — v2 schema (_schema_version table present with version=2)
    """
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

    if "_schema_version" in tables:
        row = conn.execute("SELECT version FROM _schema_version").fetchone()
        return row[0] if row else 0

    if "workflows" in tables:
        return 1

    return 0


def migrate_v1_to_v2(conn: Any) -> None:
    """Migrate a v1 database to v2.

    - Renames 'workflows' → 'runs', adds new columns
    - Renames 'workflow_id' → 'run_id' in steps (via table rebuild)
    - Adds FTS5 and indexes
    - Sets schema version to 2
    """
    logger.warning("Migrating checkpointer database from schema v1 to v2. This renames 'workflows' to 'runs' and adds indexes.")

    # Rename workflows → runs and add new columns
    conn.execute("ALTER TABLE workflows RENAME TO runs")
    _add_column_if_missing(conn, "runs", "duration_ms", "REAL")
    _add_column_if_missing(conn, "runs", "node_count", "INTEGER DEFAULT 0")
    _add_column_if_missing(conn, "runs", "error_count", "INTEGER DEFAULT 0")
    _add_column_if_missing(conn, "runs", "parent_run_id", "TEXT")
    _add_column_if_missing(conn, "runs", "config", "TEXT")

    # Rebuild steps table to rename workflow_id → run_id and add new columns
    _rebuild_steps_table(conn)

    # Create indexes and FTS
    _create_v2_indexes(conn)
    _create_fts(conn)

    # Record schema version
    conn.execute("CREATE TABLE IF NOT EXISTS _schema_version (version INTEGER NOT NULL)")
    conn.execute("DELETE FROM _schema_version")
    conn.execute("INSERT INTO _schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
    conn.commit()

    logger.info("Migration to schema v2 complete.")


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
    """Detect schema version and create/migrate as needed."""
    version = detect_schema_version(conn)

    if version == SCHEMA_VERSION:
        return
    if version == 0:
        create_v2_schema(conn)
    elif version == 1:
        migrate_v1_to_v2(conn)


# === SQL Definitions ===

_CREATE_RUNS = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    graph_name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'running',
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


def _add_column_if_missing(conn: Any, table: str, column: str, col_type: str) -> None:
    """Add a column to a table if it doesn't already exist."""
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")


def _rebuild_steps_table(conn: Any) -> None:
    """Rebuild steps table to rename columns and add new ones.

    SQLite doesn't support RENAME COLUMN before 3.25, so we rebuild.
    """
    # Check if the old schema has 'workflow_id' or already 'run_id'
    columns = {row[1] for row in conn.execute("PRAGMA table_info(steps)").fetchall()}
    has_old_schema = "workflow_id" in columns

    if not has_old_schema:
        # Already has run_id — just add missing columns
        _add_column_if_missing(conn, "steps", "node_type", "TEXT")
        _add_column_if_missing(conn, "steps", "id", "INTEGER")
        return

    # Full rebuild: rename columns workflow_id → run_id, idx → step_index,
    # child_workflow_id → child_run_id, add node_type and autoincrement id
    conn.execute("ALTER TABLE steps RENAME TO _steps_old")
    conn.execute(_CREATE_STEPS)

    conn.execute("""
        INSERT INTO steps (run_id, step_index, superstep, node_name, status,
                          duration_ms, cached, error, decision, input_versions,
                          values_data, child_run_id, created_at, completed_at)
        SELECT workflow_id, idx, superstep, node_name, status,
               duration_ms, cached, error, decision, input_versions,
               values_data, child_workflow_id, created_at, completed_at
        FROM _steps_old
    """)

    conn.execute("DROP TABLE _steps_old")
