"""Schema management for checkpointer databases."""

from __future__ import annotations

from typing import Any

SCHEMA_VERSION = 6


def detect_schema_version(conn: Any) -> int:
    """Return the schema version of an existing database.

    Returns:
        0 — empty database (no tables)
        3 — v3 schema (pre attempt ledger)
        4 — v4 schema (attempt ledger tables, false cross-store FKs)
        5 — v5 schema (cross-store lineage columns carry no FK)
        6 — current v6 schema (durable-host coordination + pending node boundaries)
    """
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

    if "_schema_version" in tables:
        row = conn.execute("SELECT version FROM _schema_version").fetchone()
        return row[0] if row else 0

    return 0


def create_v6_schema(conn: Any) -> None:
    """Create a fresh v6 schema on an empty database."""
    conn.execute(_CREATE_RUNS)
    conn.execute(_CREATE_STEPS)
    conn.execute(_CREATE_ATTEMPT_SERIES)
    conn.execute(_CREATE_ATTEMPT_RECORDS)
    _create_indexes(conn)
    _create_attempt_indexes(conn)
    _create_fts(conn)
    _ensure_v6_objects(conn)

    conn.execute("CREATE TABLE IF NOT EXISTS _schema_version (version INTEGER NOT NULL)")
    conn.execute("INSERT INTO _schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
    conn.commit()


# Backward-compatible alias: the fresh-create entry point used before v6.
create_v5_schema = create_v6_schema


def ensure_schema(conn: Any) -> None:
    """Detect schema version and create/migrate schema as needed."""
    version = detect_schema_version(conn)

    if version == SCHEMA_VERSION:
        _ensure_v3_columns(conn)
        _ensure_v4_objects(conn)
        _ensure_v6_objects(conn)
        return
    if version == 0:
        create_v6_schema(conn)
        return
    if version == 2:
        _migrate_v2_to_v3(conn)
        _migrate_v3_to_v4(conn)
        _migrate_v4_to_v5(conn)
        _migrate_v5_to_v6(conn)
        return
    if version == 3:
        _migrate_v3_to_v4(conn)
        _migrate_v4_to_v5(conn)
        _migrate_v5_to_v6(conn)
        return
    if version == 4:
        _migrate_v4_to_v5(conn)
        _migrate_v5_to_v6(conn)
        return
    if version == 5:
        _migrate_v5_to_v6(conn)
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


# === v6: durable-host coordination tables ===
#
# These tables are additive and inert for plain checkpointer use: nothing
# writes to them outside the durable host (hypergraph.host). A submission row
# is durable intent recorded BEFORE execution; the runs row is created later
# by the executing runner. run_updates is the per-Run durable sequence that
# RunHomeClient.watch replays; host_commands is the durable control channel
# (the host's stop and scheduled-answer verbs write it); host_settings holds Home-scoped
# coordination settings every process that opens the store agrees on.

_CREATE_HOST_SUBMISSIONS = """
CREATE TABLE IF NOT EXISTS host_submissions (
    workflow_id TEXT PRIMARY KEY,
    definition_name TEXT NOT NULL,
    def_version TEXT NOT NULL DEFAULT '',
    def_struct_hash TEXT NOT NULL DEFAULT '',
    inputs_json TEXT NOT NULL,
    start_at TEXT,
    state TEXT NOT NULL DEFAULT 'pending',
    recovery_attempts INTEGER NOT NULL DEFAULT 0,
    recovery_cap INTEGER NOT NULL DEFAULT 3,
    source_ref TEXT,
    created_at TEXT NOT NULL,
    claimed_at TEXT,
    finished_at TEXT,
    fingerprint TEXT,
    compat_state TEXT NOT NULL DEFAULT 'compatible',
    retry_of TEXT,
    forked_from TEXT,
    fork_reason TEXT,
    -- Retained for schema compatibility; the recovery brake no longer reads
    -- it (progress resets now happen at commit time via _after_run_mutation).
    last_progress_step_count INTEGER NOT NULL DEFAULT 0,
    -- Batch membership (ticket 05): set on child submissions only. item_key
    -- is the logical manifest key; the child workflow id stays
    -- "<batch_workflow_id>:<item_key>".
    batch_id TEXT,
    item_key TEXT
)
"""

_CREATE_RUN_UPDATES = """
CREATE TABLE IF NOT EXISTS run_updates (
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
)
"""

# ``source_ref`` is opaque caller provenance for audit only (ADR 0005 A11 /
# PRD 0017 US58-59): nothing authenticates on it and no dedup predicate reads
# it. ``pause_id``/``due_at``/``outcome`` belong to the ONE scheduled verb
# Hypergraph has (ticket 14 / ADR 0008): a scheduled *pause answer*. They are
# deliberately pause-shaped rather than a generic scheduler — there is no
# recurrence, no cron expression, and no caller-chosen verb.
_CREATE_HOST_COMMANDS = """
CREATE TABLE IF NOT EXISTS host_commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    verb TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    source_ref TEXT,
    created_at TEXT NOT NULL,
    applied_at TEXT,
    -- Scheduled pause answers only (verb = 'schedule_answer'):
    pause_id TEXT,
    due_at TEXT,
    outcome TEXT
)
"""

# Batch tables (ticket 05): host_batches is the immutable manifest pinned at
# acceptance (identity, items, tolerance, start intent, fingerprint);
# batch_updates is the per-Batch durable sequence (bseq) that
# RunHomeClient.watch(batch_ref) replays — same gap-free discipline as
# run_updates. Children are ordinary host_submissions rows linked by
# host_submissions.batch_id. host_batches.retry_of (ticket 06) records Batch
# lineage when an item-scoped rerun mints a new manifest from a source Batch.
_CREATE_HOST_BATCHES = """
CREATE TABLE IF NOT EXISTS host_batches (
    batch_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL UNIQUE,
    definition_name TEXT NOT NULL,
    def_version TEXT NOT NULL DEFAULT '',
    def_struct_hash TEXT NOT NULL DEFAULT '',
    items_json TEXT NOT NULL,
    tolerance_json TEXT,
    start_at TEXT,
    fingerprint TEXT NOT NULL,
    source_ref TEXT,
    created_at TEXT NOT NULL,
    retry_of TEXT
)
"""

_CREATE_BATCH_UPDATES = """
CREATE TABLE IF NOT EXISTS batch_updates (
    batch_id TEXT NOT NULL,
    bseq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    item_key TEXT,
    payload TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    PRIMARY KEY (batch_id, bseq)
)
"""

# Home-scoped coordination settings (ticket 12): one row per setting, shared by
# every process that opens this store. `max_active_runs` lives here rather than
# on a Python object because the worker that enforces the cap and the operator
# that tunes or reads it are usually different processes holding different
# RunHome instances — a per-instance attribute would make them disagree.
# NULL value means "explicitly unset" (unlimited), which is also what a missing
# row means.
_CREATE_HOST_SETTINGS = """
CREATE TABLE IF NOT EXISTS host_settings (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT
)
"""


# Pending node boundaries (ticket 08 / PRD 0013). This is a CORE checkpointer
# table, not a host-only one: any checkpointed run records the superstep's
# runnable node boundaries here before the first sibling dispatches, so a
# process death between siblings cannot forget the unfinished ones.
#
# The primary key is exactly the tuple ``steps`` is unique on, so recovery
# joins boundary intent against the execution journal with no guessing:
# a matching steps row means committed; no steps row and no dispatched_at
# means pending; dispatched_at without a steps row is reserved for declared
# effects (PRD 0014) and nothing sets it yet.
#
# Deliberately NO foreign key to runs(id): a claimed run lost before its
# first committed step has its history-less runs row deleted and restarts
# fresh (see host RunHome._delete_history_less_run). Boundary intent is not
# execution history and must never block that reset.
_CREATE_PENDING_NODES = """
CREATE TABLE IF NOT EXISTS pending_nodes (
    run_id TEXT NOT NULL,
    superstep INTEGER NOT NULL,
    node_name TEXT NOT NULL,
    node_type TEXT,
    created_at TEXT NOT NULL,
    dispatched_at TEXT,
    PRIMARY KEY (run_id, superstep, node_name)
)
"""


def _ensure_pending_node_objects(conn: Any) -> None:
    """Ensure the pending node-boundary table exists (safe idempotent guard)."""
    conn.execute(_CREATE_PENDING_NODES)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pending_nodes_run ON pending_nodes(run_id, superstep)")


# Durable pause slots (ticket 13 / PRD 0010). Like pending_nodes this is a
# CORE checkpointer table, not a host-only one: any checkpointed run that
# pauses writes its occurrence here in the SAME transaction as the paused
# step's records and the runs-row transition to 'paused'.
#
# The primary key is the node address `<run_id>:<superstep>:<node_name>`, so a
# loop's repeated pauses own distinct rows and settlement can compare-and-set
# on the exact occurrence a caller observed. `settled_at IS NULL` is the CAS
# guard; `answer` holds the settled value as the durable resume input for
# `response_key`.
#
# Deliberately NO foreign key to runs(id), for the same reason pending_nodes
# has none: a history-less claimed run may have its runs row deleted and
# restart fresh, and durable pause truth must never block that reset.
#
# Pause slots are NOT pruned by retention compaction. Unlike a node boundary
# (whose state is DERIVED from steps), a slot carries the question and the
# human answer — user-visible truth in its own right, not a projection of the
# journal.
_CREATE_PAUSE_SLOTS = """
CREATE TABLE IF NOT EXISTS pause_slots (
    pause_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    superstep INTEGER NOT NULL,
    node_name TEXT NOT NULL,
    node_path TEXT,
    response_key TEXT NOT NULL,
    question TEXT NOT NULL DEFAULT '{}',
    answer_schema TEXT NOT NULL DEFAULT '{}',
    options TEXT,
    created_at TEXT NOT NULL,
    settled_at TEXT,
    answer TEXT
)
"""


def _ensure_pause_slot_objects(conn: Any) -> None:
    """Ensure the durable pause-slot table exists (safe idempotent guard)."""
    conn.execute(_CREATE_PAUSE_SLOTS)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pause_slots_run ON pause_slots(run_id)")


def _create_host_indexes(conn: Any) -> None:
    conn.execute("CREATE INDEX IF NOT EXISTS idx_host_submissions_state ON host_submissions(state)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_host_submissions_definition ON host_submissions(definition_name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_host_submissions_batch ON host_submissions(batch_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_host_commands_run ON host_commands(run_id, id)")
    # The due-row scan (ticket 14) reads unapplied rows of one verb in id
    # order; the same shape ``start_at`` eligibility uses for delayed starts.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_host_commands_due ON host_commands(verb, applied_at, due_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_host_batches_workflow ON host_batches(workflow_id)")


# Columns appended to host_submissions after the initial v6 cut (tickets
# 03/04/05). The v6 DDL above already carries them; this guarded ALTER list
# migrates dev databases that were created at v6 before the columns existed.
_HOST_SUBMISSIONS_ADDED_COLUMNS = (
    ("fingerprint", "fingerprint TEXT"),
    ("compat_state", "compat_state TEXT NOT NULL DEFAULT 'compatible'"),
    ("retry_of", "retry_of TEXT"),
    ("forked_from", "forked_from TEXT"),
    ("fork_reason", "fork_reason TEXT"),
    ("last_progress_step_count", "last_progress_step_count INTEGER NOT NULL DEFAULT 0"),
    ("batch_id", "batch_id TEXT"),
    ("item_key", "item_key TEXT"),
)

# Columns appended to host_batches after its initial cut (ticket 06). The v6
# DDL above already carries it; this guarded ALTER migrates dev databases
# that were created at v6 before the column existed.
_HOST_BATCHES_ADDED_COLUMNS = (("retry_of", "retry_of TEXT"),)

# Columns appended to host_commands after its initial cut (ticket 14, the
# scheduled pause answer). Same guarded-ALTER discipline as above.
_HOST_COMMANDS_ADDED_COLUMNS = (
    ("pause_id", "pause_id TEXT"),
    ("due_at", "due_at TEXT"),
    ("outcome", "outcome TEXT"),
)


def _add_missing_columns(conn: Any, table: str, columns: tuple[tuple[str, str], ...]) -> None:
    """ALTER in every one of ``columns`` the table does not already carry."""
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, ddl in columns:
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def _ensure_v6_objects(conn: Any) -> None:
    """Ensure v6 tables exist (safe idempotent guard).

    Covers the durable-host coordination tables and the core pending
    node-boundary and pause-slot tables. Every ``ensure_schema`` path reaches
    this, so dev databases created at an earlier v6 cut pick the new objects
    up in place.
    """
    conn.execute(_CREATE_HOST_SUBMISSIONS)
    conn.execute(_CREATE_RUN_UPDATES)
    conn.execute(_CREATE_HOST_COMMANDS)
    conn.execute(_CREATE_HOST_BATCHES)
    conn.execute(_CREATE_BATCH_UPDATES)
    conn.execute(_CREATE_HOST_SETTINGS)
    _add_missing_columns(conn, "host_submissions", _HOST_SUBMISSIONS_ADDED_COLUMNS)
    _add_missing_columns(conn, "host_batches", _HOST_BATCHES_ADDED_COLUMNS)
    _add_missing_columns(conn, "host_commands", _HOST_COMMANDS_ADDED_COLUMNS)
    _create_host_indexes(conn)
    _ensure_pending_node_objects(conn)
    _ensure_pause_slot_objects(conn)
    conn.commit()


def _migrate_v5_to_v6(conn: Any) -> None:
    """In-place migration from schema v5 to v6 (adds host coordination tables)."""
    _ensure_v6_objects(conn)
    conn.execute("UPDATE _schema_version SET version = 6")
    conn.commit()
