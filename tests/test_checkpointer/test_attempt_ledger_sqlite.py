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

import asyncio
import json
import sqlite3
import threading
from datetime import datetime, timezone

import pytest

from hypergraph import AsyncRunner, Graph, node
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


async def test_reservation_writes_through_while_step_record_is_buffered(tmp_path):
    """With durability="exit" a REAL StepRecord sits in the runner's buffer.

    Mid-run, a second connection must already see the committed STARTED
    reservation while the earlier node's buffered StepRecord is genuinely
    pending — and the buffered step must land after the exit flush.
    """
    path = str(tmp_path / "exit.db")
    cp = SqliteCheckpointer(path, durability="exit", retention="latest")
    probe: dict[str, object] = {}

    @node(output_name="doubled")
    def produce(x: int) -> int:
        return x * 2

    @node(output_name="witness")
    async def reserve_and_probe(doubled: int) -> int:
        # `produce` already executed: its StepRecord exists but is buffered.
        series = await cp.open_attempt_series("wf-exit", NODE, policy_fingerprint=FP, max_attempts=3)
        await cp.begin_attempt(series.id, policy_fingerprint=FP, scheduled_superstep=1)
        second = sqlite3.connect(path)
        try:
            (reserved,) = second.execute(
                "SELECT COUNT(*) FROM attempt_records WHERE series_id = ? AND status = 'started'",
                (series.id,),
            ).fetchone()
            (steps_flushed,) = second.execute(
                "SELECT COUNT(*) FROM steps WHERE run_id = 'wf-exit'"
            ).fetchone()
        finally:
            second.close()
        probe["series_id"] = series.id
        probe["reserved_durably_mid_run"] = reserved
        probe["steps_flushed_mid_run"] = steps_flushed
        return doubled

    runner = AsyncRunner(checkpointer=cp)
    await runner.run(Graph([produce, reserve_and_probe]), {"x": 2}, workflow_id="wf-exit")
    await cp.close()

    # Mid-run: reservation durable, buffered StepRecord genuinely pending.
    assert probe["reserved_durably_mid_run"] == 1
    assert probe["steps_flushed_mid_run"] == 0

    # After the exit flush the buffered steps landed; the reservation persists.
    second = sqlite3.connect(path)
    try:
        (steps_after,) = second.execute("SELECT COUNT(*) FROM steps WHERE run_id = 'wf-exit'").fetchone()
        (reserved_after,) = second.execute(
            "SELECT COUNT(*) FROM attempt_records WHERE series_id = ? AND status = 'started'",
            (probe["series_id"],),
        ).fetchone()
    finally:
        second.close()
    assert steps_after >= 1
    assert reserved_after == 1


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


# === FA1: concurrency probes (from the wave-A review verdict) ===


def _pause_on_sql(cp, fragment: str, monkeypatch) -> tuple[asyncio.Event, asyncio.Event]:
    """Pause the next async statement matching ``fragment`` until released.

    Returns (entered, hold): ``entered`` fires when the statement is reached;
    the statement executes only after ``hold`` is set.
    """
    entered = asyncio.Event()
    hold = asyncio.Event()
    real_execute = cp._db.execute

    def wrapper(sql, *args, **kwargs):
        async def _run():
            if fragment.lower() in " ".join(sql.split()).lower() and not entered.is_set():
                entered.set()
                await hold.wait()
            return await real_execute(sql, *args, **kwargs)

        return _run()

    monkeypatch.setattr(cp._db, "execute", wrapper)
    return entered, hold


async def test_competing_settle_cannot_overwrite_settled_attempt(tmp_path, monkeypatch):
    """Review probe (close interleaving): two settles can never both win.

    A close that validated a stale snapshot must not blindly overwrite a
    settle committed in between (previous final state: attempt=FAILED over a
    committed SUCCEEDED close)."""
    path = str(tmp_path / "close-race.db")
    cp = SqliteCheckpointer(path)
    try:
        await cp.create_run(RUN, graph_name="g")
        series = await cp.open_attempt_series(RUN, NODE, policy_fingerprint=FP, max_attempts=3)
        first = await cp.begin_attempt(series.id, policy_fingerprint=FP, scheduled_superstep=0)

        # C1: async close, paused just before it writes the final outcome.
        entered, hold = _pause_on_sql(cp, "UPDATE attempt_records", monkeypatch)
        close_task = asyncio.create_task(
            cp.close_attempt_series(
                series.id,
                first.attempt_number,
                AttemptStatus.SUCCEEDED,
                step_record=_step(attempt_series_id=series.id),
            )
        )
        await entered.wait()

        # C2: competing settle over the sync connection while C1 is in flight.
        outcome: dict[str, str] = {}

        def compete() -> None:
            try:
                cp.record_attempt_outcome_sync(series.id, first.attempt_number, AttemptStatus.FAILED)
                outcome["settle"] = "won"
            except Exception as error:  # noqa: BLE001
                outcome["settle"] = f"raised:{type(error).__name__}"

        thread = threading.Thread(target=compete)
        thread.start()
        await asyncio.sleep(0.3)
        hold.set()

        close_error: str | None = None
        try:
            await close_task
        except Exception as error:  # noqa: BLE001
            close_error = type(error).__name__
        thread.join(timeout=10)
        assert not thread.is_alive()

        close_won = close_error is None
        settle_won = outcome["settle"] == "won"
        # Exactly one writer may win; the loser must fail loudly.
        assert close_won != settle_won, f"both settles claimed success: close_error={close_error}, settle={outcome['settle']}"

        records = await cp.get_attempt_records(series.id)
        closed = await cp.get_attempt_series(series.id)
        steps = await cp.get_steps(RUN)
        if close_won:
            assert [record.status for record in records] == [AttemptStatus.SUCCEEDED]
            assert closed is not None and closed.closed_at is not None
            assert any(step.attempt_series_id == series.id for step in steps)
        else:
            assert [record.status for record in records] == [AttemptStatus.FAILED]
            assert closed is not None and closed.closed_at is None
            assert all(step.attempt_series_id != series.id for step in steps)
    finally:
        await cp.close()


async def test_async_sync_reservation_race_reserves_exactly_once(tmp_path, monkeypatch):
    """Review probe (reservation race): max_attempts=1 → exactly ONE reservation.

    Previous final state: attempts #1 AND #2 recorded, remaining == -1, and a
    genuinely LIVE reservation converted to OUTCOME_UNKNOWN."""
    path = str(tmp_path / "reservation-race.db")
    cp = SqliteCheckpointer(path)
    try:
        await cp.create_run(RUN, graph_name="g")
        series = await cp.open_attempt_series(RUN, NODE, policy_fingerprint=FP, max_attempts=1)

        # C1: async reservation paused between its budget check and its insert.
        entered, hold = _pause_on_sql(cp, "COALESCE(MAX(attempt_number)", monkeypatch)
        begin_task = asyncio.create_task(cp.begin_attempt(series.id, policy_fingerprint=FP, scheduled_superstep=0))
        await entered.wait()

        outcome: dict[str, object] = {}

        def compete() -> None:
            try:
                record = cp.begin_attempt_sync(series.id, policy_fingerprint=FP, scheduled_superstep=0)
                outcome["sync"] = f"reserved:#{record.attempt_number}"
            except Exception as error:  # noqa: BLE001
                outcome["sync"] = f"raised:{type(error).__name__}"

        thread = threading.Thread(target=compete)
        thread.start()
        await asyncio.sleep(0.3)
        hold.set()

        async_error: str | None = None
        try:
            await begin_task
        except Exception as error:  # noqa: BLE001
            async_error = type(error).__name__
        thread.join(timeout=10)
        assert not thread.is_alive()

        async_won = async_error is None
        sync_won = str(outcome["sync"]).startswith("reserved")
        assert async_won != sync_won, f"reservations: async_error={async_error}, sync={outcome['sync']}"

        records = await cp.get_attempt_records(series.id)
        # Exactly one reservation ever; never remaining == -1; no live
        # reservation was converted to OUTCOME_UNKNOWN.
        assert [(record.attempt_number, record.status) for record in records] == [(1, AttemptStatus.STARTED)]
        assert await cp.remaining_attempts(series.id) == 0
    finally:
        await cp.close()


async def test_foreign_keys_enforced_on_both_connections(tmp_path):
    """Review probe (retention interleaving): an orphan attempt record must be
    structurally impossible — foreign keys ON for both connections."""
    path = str(tmp_path / "fk.db")
    cp = SqliteCheckpointer(path)
    try:
        await cp.create_run(RUN, graph_name="g")

        cursor = await cp._db.execute("PRAGMA foreign_keys")
        (fk_async,) = await cursor.fetchone()
        db = cp._sync_db()
        (fk_sync,) = db.execute("PRAGMA foreign_keys").fetchone()
        assert fk_async == 1
        assert fk_sync == 1

        # The probe's end state — a record without its series — must be rejected.
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO attempt_records (series_id, attempt_number, scheduled_superstep, status, started_at) "
                "VALUES ('ghost-series', 1, 0, 'started', '2026-01-01T00:00:00+00:00')"
            )
        db.rollback()
    finally:
        await cp.close()


async def test_retention_prune_not_observed_half_applied(tmp_path, monkeypatch):
    """Review probe (retention interleaving): a concurrent ledger reader on the
    shared async connection must never observe the half-deleted state
    (records already gone, series row still present)."""
    path = str(tmp_path / "retention-race.db")
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

        # save_step supersedes the linked step -> retention prunes the closed
        # series. Pause between the record-delete and the series-delete.
        entered, hold = _pause_on_sql(cp, "DELETE FROM attempt_series", monkeypatch)
        save_task = asyncio.create_task(cp.save_step(_step(superstep=4, index=9, values={"answer": 2})))
        await entered.wait()

        async def observe() -> tuple[object, list]:
            return (await cp.get_attempt_series(series.id), await cp.get_attempt_records(series.id))

        observe_task = asyncio.create_task(observe())
        await asyncio.sleep(0.3)
        hold.set()
        observed_series, observed_records = await observe_task
        await save_task

        half_applied = observed_series is not None and observed_records == []
        assert not half_applied, "reader observed records deleted while the series row was still present"

        # Final state: fully pruned, no orphans.
        assert await cp.get_attempt_series(series.id) is None
        assert await cp.get_attempt_records(series.id) == []
        probe = sqlite3.connect(path)
        try:
            (orphans,) = probe.execute(
                "SELECT COUNT(*) FROM attempt_records WHERE series_id NOT IN (SELECT id FROM attempt_series)"
            ).fetchone()
        finally:
            probe.close()
        assert orphans == 0
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
