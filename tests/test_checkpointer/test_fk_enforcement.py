"""FK enforcement contract for the SQLite checkpointer (#281).

The schema must declare ONLY same-store foreign keys. Cross-store lineage
columns (``runs.parent_run_id``, ``steps.child_run_id``) legitimately point at
runs stored in a DIFFERENT database when a delegated child runner owns its own
checkpointer (#235 / PR #279), so they carry no FK. With the false FKs gone,
``PRAGMA foreign_keys=ON`` runs on every connection (sync + async) as
defense-in-depth for the same-store references.

Assertion map (validation contract, wave 2):
    cross-store survival     TestCrossStoreLineageWithEnforcementOn
    orphan probes            TestSameStoreViolationsRejected
    v4 -> v5 migration       TestV4ToV5Migration
    frozen v5 FK map         test_fresh_v5_schema_declares_only_same_store_fks
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from hypergraph import AsyncRunner, Graph, node
from hypergraph.checkpointers import StepRecord, StepStatus

pytest.importorskip("aiosqlite")

from hypergraph.checkpointers import SqliteCheckpointer  # noqa: E402


def _step(
    run_id: str,
    *,
    node_name: str = "work",
    superstep: int = 0,
    index: int = 0,
    child_run_id: str | None = None,
) -> StepRecord:
    return StepRecord(
        run_id=run_id,
        superstep=superstep,
        node_name=node_name,
        index=index,
        status=StepStatus.COMPLETED,
        input_versions={},
        values={"out": 1},
        completed_at=datetime.now(timezone.utc),
        child_run_id=child_run_id,
    )


async def _async_fk_pragma(cp: SqliteCheckpointer) -> int:
    cursor = await cp._db.execute("PRAGMA foreign_keys")
    (value,) = await cursor.fetchone()
    return int(value)


def _sync_fk_pragma(cp: SqliteCheckpointer) -> int:
    (value,) = cp._sync_db().execute("PRAGMA foreign_keys").fetchone()
    return int(value)


def _fk_map(conn: sqlite3.Connection, table: str) -> set[tuple[str, str, str]]:
    """Return {(from_column, referenced_table, to_column)} for a table."""
    return {(row[3], row[2], row[4]) for row in conn.execute(f"PRAGMA foreign_key_list({table})")}


# === Cross-store lineage must survive with enforcement ON ===


class TestCrossStoreLineageWithEnforcementOn:
    """Delegated child stores hold lineage ids that live in ANOTHER database."""

    async def test_cross_store_parent_run_id_survives_async(self, tmp_path):
        """A child store accepts parent_run_id pointing at a run it does not hold."""
        cp = SqliteCheckpointer(str(tmp_path / "child.db"))
        try:
            await cp.initialize()
            assert await _async_fk_pragma(cp) == 1, "async connection must enforce foreign keys"

            run = await cp.create_run("wf/child_wf", parent_run_id="wf")
            assert run.parent_run_id == "wf"
            stored = await cp.get_run_async("wf/child_wf")
            assert stored is not None
            assert stored.parent_run_id == "wf"
        finally:
            await cp.close()

    async def test_cross_store_child_run_id_survives_async(self, tmp_path):
        """A parent store accepts child_run_id pointing at a run in a child store."""
        cp = SqliteCheckpointer(str(tmp_path / "parent.db"))
        try:
            await cp.initialize()
            assert await _async_fk_pragma(cp) == 1

            await cp.create_run("wf")
            await cp.save_step(_step("wf", node_name="child_wf", child_run_id="wf/child_wf"))
            steps = await cp.get_steps("wf")
            assert steps[0].child_run_id == "wf/child_wf"
        finally:
            await cp.close()

    def test_cross_store_lineage_survives_sync(self, tmp_path):
        """Sync parity: the sync connection enforces FKs yet accepts cross-store ids."""
        cp = SqliteCheckpointer(str(tmp_path / "child.db"))
        try:
            assert _sync_fk_pragma(cp) == 1, "sync connection must enforce foreign keys"

            cp.create_run_sync("wf/child_wf", parent_run_id="wf")
            cp.save_step_sync(_step("wf/child_wf", node_name="grand", child_run_id="wf/child_wf/grand"))

            stored = cp.get_run("wf/child_wf")
            assert stored is not None
            assert stored.parent_run_id == "wf"
            assert cp.steps("wf/child_wf")[0].child_run_id == "wf/child_wf/grand"
        finally:
            if cp._sync_conn is not None:
                cp._sync_conn.close()

    async def test_delegated_child_runner_lineage_with_fk_on(self, tmp_path):
        """End-to-end: a delegated child runner writes cross-store lineage in both stores."""

        @node(output_name="doubled")
        def double(x: int) -> int:
            return x * 2

        @node(output_name="final")
        def consume(doubled: int) -> int:
            return doubled + 1

        child_cp = SqliteCheckpointer(str(tmp_path / "child.db"), durability="sync")
        parent_cp = SqliteCheckpointer(str(tmp_path / "parent.db"), durability="sync")
        try:
            child = Graph([double], name="child")
            parent = Graph(
                [child.as_node(name="child_wf", runner=AsyncRunner(checkpointer=child_cp)), consume],
                name="parent",
            )
            runner = AsyncRunner(checkpointer=parent_cp)
            result = await runner.run(parent, {"x": 5}, workflow_id="wf")
            assert result.values["final"] == 11

            # Both stores enforce FKs...
            assert await _async_fk_pragma(parent_cp) == 1
            assert await _async_fk_pragma(child_cp) == 1

            # ...while holding references into the OTHER store.
            child_run = await child_cp.get_run_async("wf/child_wf")
            assert child_run is not None
            assert child_run.parent_run_id == "wf"
            assert await child_cp.get_run_async("wf") is None  # parent row is elsewhere

            parent_steps = {s.node_name: s for s in await parent_cp.get_steps("wf")}
            assert parent_steps["child_wf"].child_run_id == "wf/child_wf"
            assert await parent_cp.get_run_async("wf/child_wf") is None  # child row is elsewhere
        finally:
            await parent_cp.close()
            await child_cp.close()


# === Same-store violations are now rejected by sqlite ===


class TestSameStoreViolationsRejected:
    """With the PRAGMA on, orphan same-store references fail loudly."""

    async def test_orphan_step_rejected_async(self, tmp_path):
        cp = SqliteCheckpointer(str(tmp_path / "runs.db"))
        try:
            await cp.initialize()
            with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
                await cp._db.execute(
                    "INSERT INTO steps (run_id, step_index, superstep, node_name, status) VALUES (?, ?, ?, ?, ?)",
                    ("no-such-run", 0, 0, "ghost", "completed"),
                )
        finally:
            await cp.close()

    def test_orphan_step_rejected_sync(self, tmp_path):
        cp = SqliteCheckpointer(str(tmp_path / "runs.db"))
        try:
            db = cp._sync_db()
            with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
                db.execute(
                    "INSERT INTO steps (run_id, step_index, superstep, node_name, status) VALUES (?, ?, ?, ?, ?)",
                    ("no-such-run", 0, 0, "ghost", "completed"),
                )
        finally:
            if cp._sync_conn is not None:
                cp._sync_conn.close()

    def test_orphan_fork_and_retry_lineage_rejected_sync(self, tmp_path):
        """forked_from / retry_of stay same-store FKs — bogus ids are rejected."""
        cp = SqliteCheckpointer(str(tmp_path / "runs.db"))
        try:
            db = cp._sync_db()
            with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
                db.execute(
                    "INSERT INTO runs (id, forked_from) VALUES (?, ?)",
                    ("orphan-fork", "no-such-run"),
                )
            with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
                db.execute(
                    "INSERT INTO runs (id, retry_of) VALUES (?, ?)",
                    ("orphan-retry", "no-such-run"),
                )
        finally:
            if cp._sync_conn is not None:
                cp._sync_conn.close()

    def test_orphan_attempt_ledger_rows_rejected_sync(self, tmp_path):
        """attempt_series.run_id and attempt_records.series_id stay enforced."""
        cp = SqliteCheckpointer(str(tmp_path / "runs.db"))
        try:
            db = cp._sync_db()
            with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
                db.execute(
                    "INSERT INTO attempt_series (id, run_id, node_name, policy_fingerprint, max_attempts, opened_at) VALUES (?, ?, ?, ?, ?, ?)",
                    ("series-x", "no-such-run", "call", "fp", 3, "2026-01-01T00:00:00+00:00"),
                )
            with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
                db.execute(
                    "INSERT INTO attempt_records (series_id, attempt_number, scheduled_superstep, status, started_at) VALUES (?, ?, ?, ?, ?)",
                    ("no-such-series", 1, 0, "started", "2026-01-01T00:00:00+00:00"),
                )
        finally:
            if cp._sync_conn is not None:
                cp._sync_conn.close()


# === Migration: v4 databases rebuild runs/steps without the false FKs ===

# Frozen snapshot of the released v4 schema (do NOT sync with _migrate.py —
# drift between this snapshot and the module is exactly what this guards).
_V4_RUNS = """
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

_V4_STEPS = """
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

_V4_ATTEMPT_SERIES = """
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

_V4_ATTEMPT_RECORDS = """
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

_V4_FTS = [
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS steps_fts USING fts5(
        node_name, error, content='steps', content_rowid='id'
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS steps_fts_insert AFTER INSERT ON steps BEGIN
        INSERT INTO steps_fts(rowid, node_name, error)
        VALUES (new.id, new.node_name, new.error);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS steps_fts_update AFTER UPDATE ON steps BEGIN
        INSERT INTO steps_fts(steps_fts, rowid, node_name, error)
        VALUES ('delete', old.id, old.node_name, old.error);
        INSERT INTO steps_fts(rowid, node_name, error)
        VALUES (new.id, new.node_name, new.error);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS steps_fts_delete AFTER DELETE ON steps BEGIN
        INSERT INTO steps_fts(steps_fts, rowid, node_name, error)
        VALUES ('delete', old.id, old.node_name, old.error);
    END
    """,
]


def _create_v4_database(path: str) -> None:
    """Build a released-v4 database holding same-store AND cross-store lineage."""
    conn = sqlite3.connect(path)
    try:
        conn.execute(_V4_RUNS)
        conn.execute(_V4_STEPS)
        conn.execute(_V4_ATTEMPT_SERIES)
        conn.execute(_V4_ATTEMPT_RECORDS)
        for sql in _V4_FTS:
            conn.execute(sql)
        conn.execute("CREATE TABLE IF NOT EXISTS _schema_version (version INTEGER NOT NULL)")
        conn.execute("INSERT INTO _schema_version (version) VALUES (4)")

        conn.execute(
            "INSERT INTO runs (id, graph_name, status, created_at) VALUES (?, ?, ?, ?)",
            ("parent", "main", "completed", "2026-01-01T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO runs (id, graph_name, status, created_at, forked_from, fork_superstep) VALUES (?, ?, ?, ?, ?, ?)",
            ("fork-1", "main", "completed", "2026-01-02T00:00:00+00:00", "parent", 1),
        )
        conn.execute(
            "INSERT INTO runs (id, graph_name, status, created_at, retry_of, retry_index) VALUES (?, ?, ?, ?, ?, ?)",
            ("retry-1", "main", "completed", "2026-01-03T00:00:00+00:00", "parent", 1),
        )
        # Cross-store lineage: the referenced parent lives in ANOTHER database.
        # Writable pre-#281 because the FK PRAGMA was off; must survive verbatim.
        conn.execute(
            "INSERT INTO runs (id, graph_name, status, created_at, parent_run_id) VALUES (?, ?, ?, ?, ?)",
            ("wf/child_wf", "child", "completed", "2026-01-04T00:00:00+00:00", "other-store-parent"),
        )

        conn.execute(
            "INSERT INTO attempt_series (id, run_id, node_name, policy_fingerprint, max_attempts, opened_at, committed_superstep, closed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("series-1", "parent", "prepare", "fp", 3, "2026-01-01T00:00:00+00:00", 0, "2026-01-01T00:00:02+00:00"),
        )
        conn.execute(
            "INSERT INTO attempt_records (series_id, attempt_number, scheduled_superstep, status, started_at, completed_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("series-1", 1, 0, "succeeded", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:01+00:00"),
        )

        conn.execute(
            "INSERT INTO steps (run_id, step_index, superstep, node_name, status, input_versions, values_data, created_at, completed_at, attempt_series_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "parent",
                0,
                0,
                "prepare",
                "completed",
                "{}",
                json.dumps({"y": 1}).encode(),
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:01+00:00",
                "series-1",
            ),
        )
        # Cross-store child pointer: the child run lives in ANOTHER database.
        conn.execute(
            "INSERT INTO steps (run_id, step_index, superstep, node_name, status, input_versions, values_data, created_at, completed_at, child_run_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "parent",
                1,
                1,
                "child_wf",
                "completed",
                "{}",
                json.dumps({"z": 2}).encode(),
                "2026-01-01T00:01:00+00:00",
                "2026-01-01T00:01:01+00:00",
                "wf/other-child",
            ),
        )
        conn.commit()
    finally:
        conn.close()


class TestV4ToV5Migration:
    async def test_v4_database_migrates_dropping_false_fks(self, tmp_path):
        path = str(tmp_path / "legacy-v4.db")
        _create_v4_database(path)

        cp = SqliteCheckpointer(path)
        try:
            # Existing rows survive untouched — including cross-store lineage.
            child = await cp.get_run_async("wf/child_wf")
            assert child is not None
            assert child.parent_run_id == "other-store-parent"
            fork = await cp.get_run_async("fork-1")
            assert fork is not None
            assert fork.forked_from == "parent"
            retry = await cp.get_run_async("retry-1")
            assert retry is not None
            assert retry.retry_of == "parent"

            steps = await cp.get_steps("parent")
            assert [s.node_name for s in steps] == ["prepare", "child_wf"]
            assert steps[0].values == {"y": 1}
            assert steps[0].attempt_series_id == "series-1"
            assert steps[1].child_run_id == "wf/other-child"

            records = await cp.get_attempt_records("series-1")
            assert len(records) == 1

            # The rebuilt DDL declares only same-store FKs, version bumped.
            probe = sqlite3.connect(path)
            try:
                (version,) = probe.execute("SELECT version FROM _schema_version").fetchone()
                assert version == 5
                assert _fk_map(probe, "runs") == {("forked_from", "runs", "id"), ("retry_of", "runs", "id")}
                assert _fk_map(probe, "steps") == {("run_id", "runs", "id"), ("attempt_series_id", "attempt_series", "id")}
                # Step ids (FTS rowids) are preserved by the rebuild.
                ids = [row[0] for row in probe.execute("SELECT id FROM steps ORDER BY id")]
                assert ids == [1, 2]
            finally:
                probe.close()

            # FTS survives the rebuild: old rows searchable, new writes indexed.
            assert [s.node_name for s in cp.search("prepare")] == ["prepare"]
            await cp.create_run("fresh")
            await cp.save_step(_step("fresh", node_name="analyze"))
            assert [s.node_name for s in cp.search("analyze")] == ["analyze"]

            # Enforcement is live on the migrated database.
            assert await _async_fk_pragma(cp) == 1
            with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
                await cp._db.execute(
                    "INSERT INTO steps (run_id, step_index, superstep, node_name, status) VALUES (?, ?, ?, ?, ?)",
                    ("no-such-run", 0, 0, "ghost", "completed"),
                )
        finally:
            await cp.close()

    def test_v4_migration_is_idempotent_and_sync_visible(self, tmp_path):
        """Opening twice (sync path) leaves one healthy v5 database."""
        from hypergraph.checkpointers._migrate import ensure_schema

        path = str(tmp_path / "legacy-v4.db")
        _create_v4_database(path)

        conn = sqlite3.connect(path)
        try:
            ensure_schema(conn)
            ensure_schema(conn)  # second call must be a no-op
            (version,) = conn.execute("SELECT version FROM _schema_version").fetchone()
            assert version == 5
            (run_count,) = conn.execute("SELECT COUNT(*) FROM runs").fetchone()
            assert run_count == 4
        finally:
            conn.close()

        cp = SqliteCheckpointer(path)
        try:
            assert _sync_fk_pragma(cp) == 1
            run = cp.get_run("wf/child_wf")
            assert run is not None
            assert run.parent_run_id == "other-store-parent"
        finally:
            if cp._sync_conn is not None:
                cp._sync_conn.close()


def test_fresh_v5_schema_declares_only_same_store_fks(tmp_path):
    """Frozen FK map for the current DDL: cross-store columns carry no FK."""
    path = str(tmp_path / "fresh.db")
    cp = SqliteCheckpointer(path)
    assert cp.runs() == []  # trigger schema creation
    cp._sync_conn.close()
    cp._sync_conn = None

    conn = sqlite3.connect(path)
    try:
        assert _fk_map(conn, "runs") == {("forked_from", "runs", "id"), ("retry_of", "runs", "id")}
        assert _fk_map(conn, "steps") == {("run_id", "runs", "id"), ("attempt_series_id", "attempt_series", "id")}
        assert _fk_map(conn, "attempt_series") == {("run_id", "runs", "id")}
        assert _fk_map(conn, "attempt_records") == {("series_id", "attempt_series", "id")}
    finally:
        conn.close()
