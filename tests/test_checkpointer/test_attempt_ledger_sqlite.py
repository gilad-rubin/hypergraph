"""SQLite-only attempt-ledger contract tests (#229).

Assertion map (validation contract, wave A):
    A6  write-through independence      test_reservation_writes_through_under_exit_durability,
                                        test_reservation_visible_to_second_connection_before_close
    A12 migration                       test_pre_ledger_database_migrates_in_place
    (sync mirrors)                      TestSyncMirrors
    (raw-SQL falsifiers)                test_superstep_drift_creates_no_second_series_row,
                                        test_retention_deletes_closed_series_rows
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from hypergraph.checkpointers import (
    AttemptLedgerError,
    AttemptStatus,
    StepRecord,
    StepStatus,
)

pytest.importorskip("aiosqlite")

from hypergraph.checkpointers import SqliteCheckpointer  # noqa: E402

FP = "policy-fp-v1"
RUN = "wf-1"
NODE = "call_model"


def _step(
    run_id: str = RUN,
    superstep: int = 0,
    node_name: str = NODE,
    index: int = 0,
    values: dict | None = None,
    attempt_series_id: str | None = None,
) -> StepRecord:
    return StepRecord(
        run_id=run_id,
        superstep=superstep,
        node_name=node_name,
        index=index,
        status=StepStatus.COMPLETED,
        input_versions={"prompt": 1},
        values={"answer": 42} if values is None else values,
        completed_at=datetime.now(timezone.utc),
        attempt_series_id=attempt_series_id,
    )


# === A6: write-through independence from StepRecord durability ===


async def test_reservation_writes_through_under_exit_durability(tmp_path):
    path = str(tmp_path / "exit.db")
    cp = SqliteCheckpointer(path, durability="exit", retention="latest")
    await cp.create_run(RUN, graph_name="g")
    series = await cp.open_attempt_series(RUN, NODE, policy_fingerprint=FP, max_attempts=3)
    await cp.begin_attempt(series.id, policy_fingerprint=FP, scheduled_superstep=0)

    # The runner would be buffering StepRecords until exit; nothing flushed.
    # Kill before flush: no save_step calls ever happen.
    await cp.close()

    reopened = SqliteCheckpointer(path)
    try:
        assert await reopened.get_steps(RUN) == []
        records = await reopened.get_attempt_records(series.id)
        assert [record.attempt_number for record in records] == [1]
        open_series = await reopened.get_open_attempt_series(RUN, NODE)
        assert open_series is not None
        assert open_series.id == series.id
    finally:
        await reopened.close()


async def test_reservation_visible_to_second_connection_before_close(tmp_path):
    path = str(tmp_path / "wal.db")
    cp = SqliteCheckpointer(path, durability="exit", retention="latest")
    try:
        await cp.create_run(RUN, graph_name="g")
        series = await cp.open_attempt_series(RUN, NODE, policy_fingerprint=FP, max_attempts=3)
        await cp.begin_attempt(series.id, policy_fingerprint=FP, scheduled_superstep=0)

        # Probe from an entirely separate connection while the checkpointer is
        # still live: the STARTED reservation must already be committed.
        probe = sqlite3.connect(path)
        try:
            (count,) = probe.execute(
                "SELECT COUNT(*) FROM attempt_records WHERE series_id = ? AND status = 'started'",
                (series.id,),
            ).fetchone()
        finally:
            probe.close()
        assert count == 1
    finally:
        await cp.close()


# === A12: pre-ledger database migrates in place ===

# Frozen snapshot of the released v3 schema (do NOT sync with _migrate.py —
# drift between this snapshot and the module is exactly what A12 guards).
_V3_RUNS = """
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

_V3_STEPS = """
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
    UNIQUE(run_id, superstep, node_name)
)
"""

_V3_STEP_COLUMNS = [
    "id",
    "run_id",
    "step_index",
    "superstep",
    "node_name",
    "node_type",
    "status",
    "duration_ms",
    "cached",
    "error",
    "decision",
    "input_versions",
    "values_data",
    "child_run_id",
    "partial",
    "created_at",
    "completed_at",
]

_V3_FTS = [
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


def _create_v3_database(path: str) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(_V3_RUNS)
        conn.execute(_V3_STEPS)
        for sql in _V3_FTS:
            conn.execute(sql)
        conn.execute("CREATE TABLE IF NOT EXISTS _schema_version (version INTEGER NOT NULL)")
        conn.execute("INSERT INTO _schema_version (version) VALUES (3)")
        conn.execute(
            "INSERT INTO runs (id, graph_name, status, created_at) VALUES (?, ?, ?, ?)",
            ("legacy-run", "legacy-graph", "completed", "2026-01-01T00:00:00+00:00"),
        )
        for superstep, name, payload in [(0, "prepare", {"y": 1}), (1, "finish", {"z": 2})]:
            conn.execute(
                "INSERT INTO steps (run_id, step_index, superstep, node_name, status, input_versions, values_data, created_at, completed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "legacy-run",
                    superstep,
                    superstep,
                    name,
                    "completed",
                    "{}",
                    json.dumps(payload).encode("utf-8"),
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:00:01+00:00",
                ),
            )
        conn.commit()
    finally:
        conn.close()


async def test_pre_ledger_database_migrates_in_place(tmp_path):
    path = str(tmp_path / "legacy.db")
    _create_v3_database(path)

    cp = SqliteCheckpointer(path)
    try:
        # Existing rows survive untouched.
        run = await cp.get_run_async("legacy-run")
        assert run is not None
        assert run.graph_name == "legacy-graph"
        steps = await cp.get_steps("legacy-run")
        assert [step.node_name for step in steps] == ["prepare", "finish"]
        assert steps[0].values == {"y": 1}
        assert steps[1].values == {"z": 2}
        assert all(step.attempt_series_id is None for step in steps)

        # New objects appeared; existing columns are unchanged and the new
        # steps column is a nullable append.
        probe = sqlite3.connect(path)
        try:
            tables = {row[0] for row in probe.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            assert "attempt_series" in tables
            assert "attempt_records" in tables
            step_cols = [row[1] for row in probe.execute("PRAGMA table_info(steps)")]
            assert step_cols == [*_V3_STEP_COLUMNS, "attempt_series_id"]
            new_col = next(row for row in probe.execute("PRAGMA table_info(steps)") if row[1] == "attempt_series_id")
            assert new_col[3] == 0  # notnull flag: nullable
            assert new_col[4] is None  # no default
            (version,) = probe.execute("SELECT version FROM _schema_version").fetchone()
            assert version == 4
        finally:
            probe.close()

        # The migrated database is fully ledger-capable.
        series = await cp.open_attempt_series("legacy-run", NODE, policy_fingerprint=FP, max_attempts=2)
        first = await cp.begin_attempt(series.id, policy_fingerprint=FP, scheduled_superstep=2)
        await cp.close_attempt_series(
            series.id,
            first.attempt_number,
            AttemptStatus.SUCCEEDED,
            step_record=_step(run_id="legacy-run", superstep=2, index=2, attempt_series_id=series.id),
        )
        closed = await cp.get_attempt_series(series.id)
        assert closed is not None
        assert closed.closed_at is not None
    finally:
        await cp.close()


# === Raw-SQL falsifiers ===


async def test_superstep_drift_creates_no_second_series_row(tmp_path):
    path = str(tmp_path / "drift.db")
    cp = SqliteCheckpointer(path)
    await cp.create_run(RUN, graph_name="g")
    series = await cp.open_attempt_series(RUN, NODE, policy_fingerprint=FP, max_attempts=3)
    await cp.begin_attempt(series.id, policy_fingerprint=FP, scheduled_superstep=1)
    await cp.close()

    reopened = SqliteCheckpointer(path)
    try:
        await reopened.resolve_stranded_attempts(series.id)
        await reopened.begin_attempt(series.id, policy_fingerprint=FP, scheduled_superstep=2)
        probe = sqlite3.connect(path)
        try:
            (count,) = probe.execute(
                "SELECT COUNT(*) FROM attempt_series WHERE run_id = ? AND node_name = ?",
                (RUN, NODE),
            ).fetchone()
        finally:
            probe.close()
        assert count == 1
    finally:
        await reopened.close()


async def test_retention_deletes_closed_series_rows(tmp_path):
    path = str(tmp_path / "retention.db")
    cp = SqliteCheckpointer(path, retention="latest")
    try:
        await cp.create_run(RUN, graph_name="g")
        series = await cp.open_attempt_series(RUN, NODE, policy_fingerprint=FP, max_attempts=2)
        first = await cp.begin_attempt(series.id, policy_fingerprint=FP, scheduled_superstep=0)
        await cp.close_attempt_series(
            series.id,
            first.attempt_number,
            AttemptStatus.SUCCEEDED,
            step_record=_step(attempt_series_id=series.id),
        )
        await cp.save_step(_step(superstep=4, index=9, values={"answer": 2}))

        probe = sqlite3.connect(path)
        try:
            (series_count,) = probe.execute("SELECT COUNT(*) FROM attempt_series").fetchone()
            (record_count,) = probe.execute("SELECT COUNT(*) FROM attempt_records").fetchone()
        finally:
            probe.close()
        assert series_count == 0
        assert record_count == 0
    finally:
        await cp.close()


# === Sync mirrors (SyncRunner seam parity) ===


class _FailingStepInsertConnection:
    """Proxy that fails INSERTs into steps while armed."""

    def __init__(self, real):
        self._real = real
        self.armed = False

    def execute(self, sql, params=()):  # noqa: ANN001
        if self.armed and "insert into steps" in " ".join(sql.split()).lower():
            raise RuntimeError("injected persistence failure")
        return self._real.execute(sql, params)

    def __getattr__(self, name):  # noqa: ANN001
        return getattr(self._real, name)


class TestSyncMirrors:
    def test_sync_lifecycle_matches_async_reads(self, tmp_path):
        path = str(tmp_path / "sync.db")
        cp = SqliteCheckpointer(path)
        try:
            cp.create_run_sync(RUN, graph_name="g")
            series = cp.open_attempt_series_sync(RUN, NODE, policy_fingerprint=FP, max_attempts=3)

            first = cp.begin_attempt_sync(series.id, policy_fingerprint=FP, scheduled_superstep=0)
            assert first.attempt_number == 1
            cp.record_attempt_outcome_sync(series.id, 1, AttemptStatus.FAILED, sampled_delay=0.5)

            second = cp.begin_attempt_sync(series.id, policy_fingerprint=FP, scheduled_superstep=0)
            cp.close_attempt_series_sync(
                series.id,
                second.attempt_number,
                AttemptStatus.SUCCEEDED,
                step_record=_step(attempt_series_id=series.id),
            )

            closed = cp.get_attempt_series_sync(series.id)
            assert closed is not None
            assert closed.closed_at is not None
            assert closed.committed_superstep == 0
            records = cp.get_attempt_records_sync(series.id)
            assert [record.status for record in records] == [AttemptStatus.FAILED, AttemptStatus.SUCCEEDED]
            assert records[0].sampled_delay == 0.5
            assert cp.get_open_attempt_series_sync(RUN, NODE) is None
            assert cp.remaining_attempts_sync(series.id) == 1
        finally:
            import asyncio

            asyncio.run(cp.close())

    def test_sync_stranded_resolution(self, tmp_path):
        path = str(tmp_path / "sync-stranded.db")
        cp = SqliteCheckpointer(path)
        try:
            cp.create_run_sync(RUN, graph_name="g")
            series = cp.open_attempt_series_sync(RUN, NODE, policy_fingerprint=FP, max_attempts=3)
            cp.begin_attempt_sync(series.id, policy_fingerprint=FP, scheduled_superstep=0)

            resolved = cp.resolve_stranded_attempts_sync(series.id)
            assert [record.status for record in resolved] == [AttemptStatus.OUTCOME_UNKNOWN]
            assert cp.remaining_attempts_sync(series.id) == 2
        finally:
            import asyncio

            asyncio.run(cp.close())

    def test_sync_close_rolls_back_on_step_write_failure(self, tmp_path):
        path = str(tmp_path / "sync-atomic.db")

        proxy_holder: dict[str, _FailingStepInsertConnection] = {}

        class _FaultableCheckpointer(SqliteCheckpointer):
            def _sync_db(self):
                real = super()._sync_db()
                if "proxy" not in proxy_holder:
                    proxy_holder["proxy"] = _FailingStepInsertConnection(real)
                return proxy_holder["proxy"]

        cp = _FaultableCheckpointer(path)
        try:
            cp.create_run_sync(RUN, graph_name="g")
            series = cp.open_attempt_series_sync(RUN, NODE, policy_fingerprint=FP, max_attempts=2)
            first = cp.begin_attempt_sync(series.id, policy_fingerprint=FP, scheduled_superstep=0)

            proxy_holder["proxy"].armed = True
            with pytest.raises(RuntimeError, match="injected persistence failure"):
                cp.close_attempt_series_sync(
                    series.id,
                    first.attempt_number,
                    AttemptStatus.SUCCEEDED,
                    step_record=_step(attempt_series_id=series.id),
                )
            proxy_holder["proxy"].armed = False

            # Rolled back: series still open, attempt unsettled, no step row.
            open_series = cp.get_open_attempt_series_sync(RUN, NODE)
            assert open_series is not None
            assert open_series.id == series.id
            records = cp.get_attempt_records_sync(series.id)
            assert [record.status for record in records] == [AttemptStatus.STARTED]
            assert cp.steps(RUN) == []
        finally:
            import asyncio

            asyncio.run(cp.close())

    def test_sync_begin_rejects_fingerprint_mismatch(self, tmp_path):
        path = str(tmp_path / "sync-fp.db")
        cp = SqliteCheckpointer(path)
        try:
            cp.create_run_sync(RUN, graph_name="g")
            series = cp.open_attempt_series_sync(RUN, NODE, policy_fingerprint=FP, max_attempts=2)
            with pytest.raises(AttemptLedgerError, match="fingerprint"):
                cp.begin_attempt_sync(series.id, policy_fingerprint="other", scheduled_superstep=0)
            assert cp.get_attempt_records_sync(series.id) == []
        finally:
            import asyncio

            asyncio.run(cp.close())
