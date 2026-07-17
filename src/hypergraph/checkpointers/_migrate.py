"""Schema management for checkpointer databases."""

from __future__ import annotations

from typing import Any

SCHEMA_VERSION = 4


def detect_schema_version(conn: Any) -> int:
    """Return the schema version of an existing database.

    Returns:
        0 — empty database (no tables)
        3 — v3 schema (pre attempt ledger)
        4 — current v4 schema (attempt ledger tables present)
    """
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

    if "_schema_version" in tables:
        row = conn.execute("SELECT version FROM _schema_version").fetchone()
        return row[0] if row else 0

    return 0


def create_v4_schema(conn: Any) -> None:
    """Create a fresh v4 schema on an empty database."""
    conn.execute(_CREATE_RUNS)
    conn.execute(_CREATE_STEPS)
    conn.execute(_CREATE_ATTEMPT_SERIES)
    conn.execute(_CREATE_ATTEMPT_RECORDS)
    _create_indexes(conn)
    _create_attempt_indexes(conn)
    _create_fts(conn)

    conn.execute("CREATE TABLE IF NOT EXISTS _schema_version (version INTEGER NOT NULL)")
    conn.execute("INSERT INTO _schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
    conn.commit()


def ensure_schema(conn: Any) -> None:
    """Detect schema version and create/migrate schema as needed."""
    version = detect_schema_version(conn)

    if version == SCHEMA_VERSION:
        _ensure_v3_columns(conn)
        _ensure_v4_objects(conn)
        return
    if version == 0:
        create_v4_schema(conn)
        return
    if version == 2:
        _migrate_v2_to_v3(conn)
        _migrate_v3_to_v4(conn)
        return
    if version == 3:
        _migrate_v3_to_v4(conn)
        return
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
    forked_from TEXT REFERENCES runs(id),
    fork_superstep INTEGER,
    retry_of TEXT REFERENCES runs(id),
    retry_index INTEGER,
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
    partial INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    completed_at TEXT,
    attempt_series_id TEXT REFERENCES attempt_series(id),
    UNIQUE(run_id, superstep, node_name)
)
"""

_CREATE_ATTEMPT_SERIES = """
CREATE TABLE IF NOT EXISTS attempt_series (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id),
    node_name TEXT NOT NULL,
    policy_fingerprint TEXT NOT NULL,
    max_attempts INTEGER NOT NULL,
    opened_at TEXT NOT NULL,
    deadline_at TEXT,
    committed_superstep INTEGER,
    closed_at TEXT
)
"""

_CREATE_ATTEMPT_RECORDS = """
CREATE TABLE IF NOT EXISTS attempt_records (
    series_id TEXT NOT NULL REFERENCES attempt_series(id),
    attempt_number INTEGER NOT NULL,
    scheduled_superstep INTEGER NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    error_type TEXT,
    error_message TEXT,
    retry_not_before TEXT,
    sampled_delay REAL,
    deadline_elapsed INTEGER NOT NULL DEFAULT 0,
    cancellation_requested INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (series_id, attempt_number)
)
"""


def _create_indexes(conn: Any) -> None:
    """Create indexes for common CLI query patterns."""
    conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_graph ON runs(graph_name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_retry_of ON runs(retry_of)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_forked_from ON runs(forked_from)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_steps_run ON steps(run_id, step_index)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_steps_run_time ON steps(run_id, completed_at, created_at, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_steps_time ON steps(completed_at, created_at, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_steps_node ON steps(node_name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_steps_status ON steps(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_parent ON runs(parent_run_id)")


def _create_attempt_indexes(conn: Any) -> None:
    """Create attempt-ledger indexes.

    The partial unique index enforces at most one OPEN series per
    (run_id, node_name); closed history may accumulate.
    """
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_attempt_series_open ON attempt_series(run_id, node_name) WHERE closed_at IS NULL")


def _ensure_v3_columns(conn: Any) -> None:
    """Ensure v3 lineage columns exist (safe idempotent guard)."""
    existing_runs = {row[1] for row in conn.execute("PRAGMA table_info(runs)").fetchall()}
    if "forked_from" not in existing_runs:
        conn.execute("ALTER TABLE runs ADD COLUMN forked_from TEXT REFERENCES runs(id)")
    if "fork_superstep" not in existing_runs:
        conn.execute("ALTER TABLE runs ADD COLUMN fork_superstep INTEGER")
    if "retry_of" not in existing_runs:
        conn.execute("ALTER TABLE runs ADD COLUMN retry_of TEXT REFERENCES runs(id)")
    if "retry_index" not in existing_runs:
        conn.execute("ALTER TABLE runs ADD COLUMN retry_index INTEGER")

    existing_steps = {row[1] for row in conn.execute("PRAGMA table_info(steps)").fetchall()}
    if "partial" not in existing_steps:
        conn.execute("ALTER TABLE steps ADD COLUMN partial INTEGER NOT NULL DEFAULT 0")

    _create_indexes(conn)
    conn.commit()


def _ensure_v4_objects(conn: Any) -> None:
    """Ensure attempt-ledger tables/column exist (safe idempotent guard).

    Existing tables are only extended additively: the new steps column is a
    nullable append, so pre-ledger rows keep their exact byte layout.
    """
    conn.execute(_CREATE_ATTEMPT_SERIES)
    conn.execute(_CREATE_ATTEMPT_RECORDS)

    existing_steps = {row[1] for row in conn.execute("PRAGMA table_info(steps)").fetchall()}
    if "attempt_series_id" not in existing_steps:
        conn.execute("ALTER TABLE steps ADD COLUMN attempt_series_id TEXT REFERENCES attempt_series(id)")

    existing_attempt_records = {row[1] for row in conn.execute("PRAGMA table_info(attempt_records)").fetchall()}
    if "deadline_elapsed" not in existing_attempt_records:
        conn.execute("ALTER TABLE attempt_records ADD COLUMN deadline_elapsed INTEGER NOT NULL DEFAULT 0")
    if "cancellation_requested" not in existing_attempt_records:
        conn.execute("ALTER TABLE attempt_records ADD COLUMN cancellation_requested INTEGER NOT NULL DEFAULT 0")

    _create_attempt_indexes(conn)
    conn.commit()


def _migrate_v2_to_v3(conn: Any) -> None:
    """In-place migration from schema v2 to v3."""
    _ensure_v3_columns(conn)
    conn.execute("UPDATE _schema_version SET version = 3")
    conn.commit()


def _migrate_v3_to_v4(conn: Any) -> None:
    """In-place migration from schema v3 to v4 (adds the attempt ledger)."""
    _ensure_v3_columns(conn)
    _ensure_v4_objects(conn)
    conn.execute("UPDATE _schema_version SET version = 4")
    conn.commit()


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
