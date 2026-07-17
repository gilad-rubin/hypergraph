"""Schema management for checkpointer databases."""

from __future__ import annotations

from typing import Any

SCHEMA_VERSION = 5


def detect_schema_version(conn: Any) -> int:
    """Return the schema version of an existing database.

    Returns:
        0 — empty database (no tables)
        3 — v3 schema (pre attempt ledger)
        4 — v4 schema (attempt ledger tables, false cross-store FKs)
        5 — current v5 schema (cross-store lineage columns carry no FK)
    """
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

    if "_schema_version" in tables:
        row = conn.execute("SELECT version FROM _schema_version").fetchone()
        return row[0] if row else 0

    return 0


def create_v5_schema(conn: Any) -> None:
    """Create a fresh v5 schema on an empty database."""
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
        create_v5_schema(conn)
        return
    if version == 2:
        _migrate_v2_to_v3(conn)
        _migrate_v3_to_v4(conn)
        _migrate_v4_to_v5(conn)
        return
    if version == 3:
        _migrate_v3_to_v4(conn)
        _migrate_v4_to_v5(conn)
        return
    if version == 4:
        _migrate_v4_to_v5(conn)
        return
    raise ValueError(f"Unsupported database schema version {version} (current: {SCHEMA_VERSION}). Please upgrade hypergraph.")


# === SQL Definitions ===
#
# runs.parent_run_id and steps.child_run_id deliberately carry NO foreign key:
# delegated child runners (#235/#279) store cross-database lineage, so the
# referenced run may live in a different sqlite file. All remaining REFERENCES
# clauses are same-store by contract and enforced via PRAGMA foreign_keys=ON.

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
    parent_run_id TEXT,
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
    child_run_id TEXT,
    partial INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    completed_at TEXT,
    attempt_series_id TEXT REFERENCES attempt_series(id),
    UNIQUE(run_id, superstep, node_name)
)
"""

_RUNS_COPY_COLS = (
    "id, graph_name, status, duration_ms, node_count, error_count, created_at, completed_at, "
    "parent_run_id, forked_from, fork_superstep, retry_of, retry_index, config"
)
# Explicit id: preserves FTS rowids and keeps AUTOINCREMENT continuing past it.
_STEPS_COPY_COLS = (
    "id, run_id, step_index, superstep, node_name, node_type, status, duration_ms, cached, error, "
    "decision, input_versions, values_data, child_run_id, partial, created_at, completed_at, attempt_series_id"
)

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


def _rebuild_table(conn: Any, table: str, create_sql: str, copy_cols: str) -> None:
    """Rebuild one table using the documented sqlite pattern.

    SQLite cannot drop a foreign key in place: create the new table under a
    temporary name, copy rows with an explicit column list (physical column
    order varies across in-place-migrated databases), drop the old table, and
    rename. Caller owns the surrounding transaction and FK-off window.
    """
    tmp = f"{table}_v5_new"
    conn.execute(create_sql.replace(f"CREATE TABLE IF NOT EXISTS {table} ", f"CREATE TABLE {tmp} ", 1))
    conn.execute(f"INSERT INTO {tmp} ({copy_cols}) SELECT {copy_cols} FROM {table}")
    conn.execute(f"DROP TABLE {table}")
    conn.execute(f"ALTER TABLE {tmp} RENAME TO {table}")


def _migrate_v4_to_v5(conn: Any) -> None:
    """Rebuild runs/steps without the false cross-store FK declarations.

    v4 declared ``runs.parent_run_id`` and ``steps.child_run_id`` as
    ``REFERENCES runs(id)``, but delegated child runners store cross-database
    lineage there — the referenced run can live in a different sqlite file.
    Rows are copied verbatim (including cross-store ids); ``PRAGMA
    foreign_keys`` must be OFF during the rebuild, so it runs before the
    transaction opens (the pragma is a no-op inside one).
    """
    _ensure_v3_columns(conn)
    _ensure_v4_objects(conn)

    (prev_fk,) = conn.execute("PRAGMA foreign_keys").fetchone()
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Dropping steps also drops its FTS sync triggers; the steps_fts
            # table itself survives and stays valid because step ids are copied.
            _rebuild_table(conn, "steps", _CREATE_STEPS, _STEPS_COPY_COLS)
            _rebuild_table(conn, "runs", _CREATE_RUNS, _RUNS_COPY_COLS)
            _create_indexes(conn)
            _create_fts(conn)
            conn.execute("UPDATE _schema_version SET version = 5")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    finally:
        if prev_fk:
            conn.execute("PRAGMA foreign_keys=ON")


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
