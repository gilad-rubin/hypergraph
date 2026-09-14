"""The checkpointer says each thing once, and says it the same way twice.

Three claims are pinned here, because all three are invisible at runtime
until they are already wrong:

1. No two methods of ``sqlite.py`` spell the same statement. A statement in
   two places is two places to change and one place to forget.
2. Rows are decoded against the column list the SELECT projects, by name. A
   list that drifts from its decoder fails loudly instead of shifting every
   field by one.
3. The async half and the sync half store the SAME rows, for the ordinary
   run/step path and for the attempt ledger.

Plus the two narrower guarantees this file is the natural home for: the
retention policy has one implementation across backends, and ``__del__``
still knows how to shut an orphaned aiosqlite connection down.
"""

from __future__ import annotations

import ast
import logging
import re
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from hypergraph.checkpointers import (
    CheckpointPolicy,
    MemoryCheckpointer,
    SqliteCheckpointer,
    StepRecord,
    StepStatus,
    WorkflowStatus,
)
from hypergraph.checkpointers import memory as memory_module
from hypergraph.checkpointers import sqlite as sqlite_module
from hypergraph.checkpointers._retention import (
    BASELINE_NODE_NAME,
    BASELINE_NODE_TYPE,
    RETENTION_ROW_COLS,
    plan_retention,
)
from hypergraph.checkpointers._rows import (
    ATTEMPT_RECORD_COLS,
    ATTEMPT_SERIES_COLS,
    NODE_BOUNDARY_COLS,
    PAUSE_SLOT_COLS,
    RUNS_COLS,
    STEPS_COLS,
    row_to_attempt_record,
    row_to_attempt_series,
    row_to_node_boundary,
    row_to_pause_slot,
    row_to_run,
    row_to_step,
)
from hypergraph.checkpointers.serializers import JsonSerializer
from hypergraph.checkpointers.types import AttemptError, AttemptStatus, PauseSlot

aiosqlite = pytest.importorskip("aiosqlite")

_SQLITE_SOURCE = Path(sqlite_module.__file__).read_text()
_CHECKPOINTER_DIR = Path(sqlite_module.__file__).parent


# === 1. One statement, one place ===


_SQL_START = re.compile(r"^\s*(SELECT|INSERT|UPDATE|DELETE|BEGIN|PRAGMA)\b", re.IGNORECASE)


def _normalize(text: str) -> str:
    return " ".join(text.split())


def _statement_texts(node: ast.AST) -> set[str]:
    """Every SQL-looking literal under ``node``, f-strings templated.

    An f-string is rendered whole and NOT descended into: its leading
    ``"SELECT "`` piece is a fragment of one statement, not a second one.
    """
    found: set[str] = set()
    pending: list[ast.AST] = [node]
    while pending:
        current = pending.pop()
        if isinstance(current, ast.JoinedStr):
            rendered = "".join(part.value if isinstance(part, ast.Constant) else f"{{{ast.unparse(part.value)}}}" for part in current.values)
            if _SQL_START.match(rendered):
                found.add(_normalize(rendered))
            continue
        if isinstance(current, ast.Constant) and isinstance(current.value, str) and _SQL_START.match(current.value):
            found.add(_normalize(current.value))
        pending.extend(ast.iter_child_nodes(current))
    return found


def test_no_statement_is_spelled_in_two_functions():
    """A statement lives in a module constant or one builder — never in two."""
    module = ast.parse(_SQLITE_SOURCE)
    owners: dict[str, list[str]] = defaultdict(list)
    for node in ast.walk(module):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for statement in _statement_texts(node):
                owners[statement].append(node.name)

    duplicated = {statement: sorted(set(names)) for statement, names in owners.items() if len(set(names)) > 1}
    assert duplicated == {}, f"SQL spelled in more than one function: {duplicated}"


def test_the_duplicate_statement_check_can_actually_fail():
    """The detector above is not vacuously green."""
    module = ast.parse("def a():\n    db.execute('SELECT 1 FROM runs WHERE id = ?')\ndef b():\n    db.execute('SELECT 1 FROM runs WHERE id = ?')\n")
    owners: dict[str, list[str]] = defaultdict(list)
    for node in ast.walk(module):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for statement in _statement_texts(node):
                owners[statement].append(node.name)
    assert owners["SELECT 1 FROM runs WHERE id = ?"] == ["a", "b"]


# === 2. Rows decoded by name ===


def test_no_positional_row_length_guards_remain():
    """``len(row) > N`` masked the invariant instead of enforcing it."""
    offenders = {path.name: re.findall(r"len\(\s*row\w*\s*\)\s*[<>]=?\s*\d+", path.read_text()) for path in sorted(_CHECKPOINTER_DIR.glob("*.py"))}
    assert {name: hits for name, hits in offenders.items() if hits} == {}


@pytest.mark.parametrize(
    ("decoder", "columns", "label"),
    [
        (row_to_run, RUNS_COLS, "RUNS_COLS"),
        (row_to_pause_slot, PAUSE_SLOT_COLS, "PAUSE_SLOT_COLS"),
        (row_to_node_boundary, NODE_BOUNDARY_COLS, "NODE_BOUNDARY_COLS"),
        (row_to_attempt_series, ATTEMPT_SERIES_COLS, "ATTEMPT_SERIES_COLS"),
        (row_to_attempt_record, ATTEMPT_RECORD_COLS, "ATTEMPT_RECORD_COLS"),
    ],
)
def test_a_row_of_the_wrong_width_is_refused_by_name(decoder, columns, label):
    """A column list that drifts from its decoder fails loudly, not silently."""
    width = len(columns.split(","))
    short = tuple(None for _ in range(width - 1))
    with pytest.raises(RuntimeError, match=rf"{label} names {width}"):
        decoder(short)

    widened = tuple(None for _ in range(width + 1))
    with pytest.raises(RuntimeError, match=rf"has {width + 1} column\(s\)"):
        decoder(widened)


def test_a_widened_steps_row_is_refused():
    """The step decoder takes a serializer, so it gets its own case."""
    width = len(STEPS_COLS.split(","))
    with pytest.raises(RuntimeError, match=rf"STEPS_COLS names {width}"):
        row_to_step(JsonSerializer(), tuple(None for _ in range(width + 1)))


@pytest_asyncio.fixture
async def store(tmp_path):
    checkpointer = SqliteCheckpointer(str(tmp_path / "builders.db"))
    await checkpointer.initialize()
    yield checkpointer
    await checkpointer.close()


async def test_every_projection_matches_the_list_its_decoder_zips(store):
    """What each SELECT actually projects IS the constant the decoder uses."""
    db = store._sync_db()
    steps_sql, _ = sqlite_module._steps_query("wf", superstep=None, show_internal=True)
    search_sql, _ = store._search_query("x", field=None, limit=1)
    projections = [
        (sqlite_module._RUN_BY_ID_SQL, ("wf",), RUNS_COLS),
        (steps_sql, ("wf",), STEPS_COLS),
        (search_sql, ("x", 1), STEPS_COLS),
        (sqlite_module._PAUSE_SLOT_CURRENT_SQL, ("wf",), PAUSE_SLOT_COLS),
        (sqlite_module._PAUSE_SLOT_BY_ID_SQL, ("wf", "p"), PAUSE_SLOT_COLS),
        (sqlite_module._NODE_BOUNDARY_SELECT_SQL, ("wf",), NODE_BOUNDARY_COLS),
        (sqlite_module._ATTEMPT_SERIES_BY_ID_SQL, ("s",), ATTEMPT_SERIES_COLS),
        (sqlite_module._ATTEMPT_SERIES_OPEN_SQL, ("wf", "n"), ATTEMPT_SERIES_COLS),
        (sqlite_module._ATTEMPT_RECORDS_SQL, ("s",), ATTEMPT_RECORD_COLS),
        (sqlite_module._ATTEMPT_RECORD_SQL, ("s", 1), ATTEMPT_RECORD_COLS),
        (sqlite_module._ATTEMPT_LIVE_SQL, ("s",), ATTEMPT_RECORD_COLS),
        (sqlite_module._RETENTION_ROWS_SQL, ("wf",), RETENTION_ROW_COLS),
    ]
    for sql, params, columns in projections:
        cursor = db.execute(sql, params)
        projected = [description[0] for description in cursor.description]
        assert projected == [name.strip() for name in columns.split(",")], sql


# === 3. The two halves store the same rows ===


_VOLATILE = {
    "runs": {"created_at", "completed_at"},
    "steps": {"id", "created_at", "completed_at", "attempt_series_id"},
    "pending_nodes": {"created_at", "dispatched_at", "settled_at"},
    "pause_slots": {"created_at", "settled_at"},
    "attempt_series": {"id", "opened_at", "closed_at"},
    "attempt_records": {"series_id", "started_at", "completed_at"},
}


def _dump(checkpointer: SqliteCheckpointer, table: str) -> list[dict[str, Any]]:
    """Every row of one table, with generated ids and clock values masked."""
    cursor = checkpointer._sync_db().execute(f"SELECT * FROM {table} ORDER BY rowid")
    names = [description[0] for description in cursor.description]
    volatile = _VOLATILE[table]
    return [
        {name: ("<volatile>" if name in volatile and value is not None else value) for name, value in zip(names, row, strict=True)}
        for row in cursor.fetchall()
    ]


def _snapshot(checkpointer: SqliteCheckpointer) -> dict[str, list[dict[str, Any]]]:
    return {table: _dump(checkpointer, table) for table in _VOLATILE}


def _step(run_id: str, superstep: int, node_name: str, **kwargs: Any) -> StepRecord:
    return StepRecord(
        run_id=run_id,
        superstep=superstep,
        node_name=node_name,
        index=superstep,
        status=kwargs.pop("status", StepStatus.COMPLETED),
        input_versions={"x": 1},
        values=kwargs.pop("values", {node_name: superstep}),
        duration_ms=1.5,
        **kwargs,
    )


async def test_run_and_step_paths_store_the_same_rows_sync_and_async(tmp_path):
    """The legacy path: create, pending boundaries, steps, status, pause."""
    async_store = SqliteCheckpointer(str(tmp_path / "async.db"))
    sync_store = SqliteCheckpointer(str(tmp_path / "sync.db"))
    slot = PauseSlot(
        run_id="wf",
        superstep=1,
        node_name="ask",
        node_path="ask",
        response_key="answer",
        question={"text": "which?"},
        answer_schema={"type": "string"},
        options=("a", "b"),
    )
    try:
        await async_store.create_run("wf", graph_name="g", config={"k": 1}, inputs={"x": 1})
        await async_store.save_step(_step("wf", 0, "load"))
        await async_store.update_run_status("wf", WorkflowStatus.ACTIVE, duration_ms=5.0, node_count=1, error_count=0)
        await async_store.record_pause(slot)
        await async_store.settle_pause("wf", pause_id=slot.pause_id, value="a")

        sync_store.create_run_sync("wf", graph_name="g", config={"k": 1}, inputs={"x": 1})
        sync_store.save_step_sync(_step("wf", 0, "load"))
        sync_store.update_run_status_sync("wf", WorkflowStatus.ACTIVE, duration_ms=5.0, node_count=1, error_count=0)
        sync_store.record_pause_sync(slot)
        sync_store.settle_pause_sync("wf", pause_id=slot.pause_id, value="a")

        assert _snapshot(async_store) == _snapshot(sync_store)
        assert await async_store.get_state("wf") == sync_store.state("wf")
        assert await async_store.get_run_inputs("wf") == sync_store.get_run_inputs_sync("wf")
    finally:
        await async_store.close()
        await sync_store.close()


async def test_attempt_series_paths_store_the_same_rows_sync_and_async(tmp_path):
    """The attempt ledger: open, begin, record an outcome, retry, close."""
    async_store = SqliteCheckpointer(str(tmp_path / "async.db"))
    sync_store = SqliteCheckpointer(str(tmp_path / "sync.db"))
    error = AttemptError(type_name="ValueError", message="boom")
    try:
        await async_store.create_run("wf")
        series = await async_store.open_attempt_series("wf", "flaky", policy_fingerprint="fp", max_attempts=3)
        first = await async_store.begin_attempt(series.id, policy_fingerprint="fp", scheduled_superstep=0)
        await async_store.record_attempt_outcome(series.id, first.attempt_number, AttemptStatus.FAILED, error=error)
        second = await async_store.begin_attempt(series.id, policy_fingerprint="fp", scheduled_superstep=1)
        await async_store.close_attempt_series(
            series.id,
            second.attempt_number,
            AttemptStatus.SUCCEEDED,
            step_record=_step("wf", 1, "flaky", attempt_series_id=series.id),
        )

        sync_store.create_run_sync("wf")
        sync_series = sync_store.open_attempt_series_sync("wf", "flaky", policy_fingerprint="fp", max_attempts=3)
        sync_first = sync_store.begin_attempt_sync(sync_series.id, policy_fingerprint="fp", scheduled_superstep=0)
        sync_store.record_attempt_outcome_sync(sync_series.id, sync_first.attempt_number, AttemptStatus.FAILED, error=error)
        sync_second = sync_store.begin_attempt_sync(sync_series.id, policy_fingerprint="fp", scheduled_superstep=1)
        sync_store.close_attempt_series_sync(
            sync_series.id,
            sync_second.attempt_number,
            AttemptStatus.SUCCEEDED,
            step_record=_step("wf", 1, "flaky", attempt_series_id=sync_series.id),
        )

        assert _snapshot(async_store) == _snapshot(sync_store)
        assert await async_store.remaining_attempts(series.id) == sync_store.remaining_attempts_sync(sync_series.id)
    finally:
        await async_store.close()
        await sync_store.close()


async def test_retention_compaction_prunes_the_same_tables_sync_and_async(tmp_path):
    """A compaction pass drives one statement stream, so both halves prune alike."""
    policy = CheckpointPolicy(durability="sync", retention="latest")
    async_store = SqliteCheckpointer(str(tmp_path / "async.db"), policy=policy)
    sync_store = SqliteCheckpointer(str(tmp_path / "sync.db"), policy=policy)
    try:
        await async_store.create_run("wf")
        sync_store.create_run_sync("wf")
        for superstep in range(3):
            await async_store.record_pending_nodes([_pending("wf", superstep)])
            await async_store.save_step(_step("wf", superstep, "loop", values={f"v{superstep}": superstep}))
            sync_store.record_pending_nodes_sync([_pending("wf", superstep)])
            sync_store.save_step_sync(_step("wf", superstep, "loop", values={f"v{superstep}": superstep}))

        assert _snapshot(async_store) == _snapshot(sync_store)
        carriers = [step for step in sync_store.steps("wf", show_internal=True) if step.node_type == BASELINE_NODE_TYPE]
        assert len(carriers) == 1
        assert await async_store.get_state("wf") == sync_store.state("wf") == {"v0": 0, "v1": 1, "v2": 2}
    finally:
        await async_store.close()
        await sync_store.close()


def _pending(run_id: str, superstep: int):
    from hypergraph.checkpointers.types import PendingNode

    return PendingNode(run_id=run_id, superstep=superstep, node_name="loop", node_type="FunctionNode")


# === 4. One retention policy, both backends ===


def test_memory_carries_no_retention_policy_of_its_own():
    """The sentinels and the keep/drop arithmetic live in _retention only."""
    source = Path(memory_module.__file__).read_text()
    assert BASELINE_NODE_NAME not in source, "the carrier sentinel is spelled in memory.py"
    assert BASELINE_NODE_TYPE not in source, "the carrier type is spelled in memory.py"
    assert "cutoff" not in source, "windowing arithmetic belongs to plan_retention"
    assert "plan_retention" in source


@pytest.mark.parametrize(
    "policy",
    [
        CheckpointPolicy(retention="latest"),
        CheckpointPolicy(retention="windowed", window=2),
    ],
)
async def test_memory_and_sqlite_keep_the_same_steps(tmp_path, policy):
    """Same policy, same kept addresses, same restored state."""
    sqlite_store = SqliteCheckpointer(str(tmp_path / "retention.db"), policy=policy)
    memory_store = MemoryCheckpointer()
    memory_store.policy = policy
    try:
        await sqlite_store.create_run("wf")
        await memory_store.create_run("wf")
        for superstep in range(5):
            record = _step("wf", superstep, f"node_{superstep % 2}", values={f"v{superstep}": superstep})
            await sqlite_store.save_step(record)
            await memory_store.save_step(record)

        def addresses(steps):
            return sorted((step.superstep, step.node_name) for step in steps)

        assert addresses(await sqlite_store.get_steps("wf", show_internal=True)) == addresses(await memory_store.get_steps("wf", show_internal=True))
        assert await sqlite_store.get_state("wf") == await memory_store.get_state("wf")
    finally:
        await sqlite_store.close()


def test_plan_retention_decides_by_position_not_equality():
    """Two rows can compare equal; only one of them is the latest occurrence."""

    class _Row:
        def __init__(self, node_name: str, superstep: int) -> None:
            self.node_name = node_name
            self.superstep = superstep

        def __eq__(self, other: object) -> bool:  # every row equals every row
            return isinstance(other, _Row)

        __hash__ = None  # type: ignore[assignment]

    rows = [_Row("a", 0), _Row("a", 1), _Row("b", 1)]
    plan = plan_retention(rows, "latest", None)
    assert plan is not None
    assert [(row.node_name, row.superstep) for row in plan.kept_rows] == [("a", 1), ("b", 1)]
    assert [(row.node_name, row.superstep) for row in plan.dropped_rows] == [("a", 0)]


# === 5. __del__ can still close an orphaned aiosqlite connection ===


async def test_aiosqlite_still_exposes_the_internals_del_depends_on(tmp_path):
    """Canary: if an upgrade renames either attribute, fail HERE, loudly."""
    connection = await aiosqlite.connect(str(tmp_path / "canary.db"))
    try:
        assert hasattr(connection, sqlite_module._AIOSQLITE_RAW_CONNECTION)
        assert hasattr(connection, sqlite_module._AIOSQLITE_RUNNING_FLAG)
        assert isinstance(getattr(connection, sqlite_module._AIOSQLITE_RAW_CONNECTION), sqlite3.Connection)
        assert getattr(connection, sqlite_module._AIOSQLITE_RUNNING_FLAG) is True
    finally:
        await connection.close()


async def test_a_refused_raw_close_still_stops_the_worker_and_says_so(caplog):
    """sqlite3 refuses a cross-thread close — the flag must clear regardless.

    The old blanket ``suppress(Exception)`` hid exactly this: the raw close
    was being refused on every ordinary finalization and nobody could tell.
    """

    class _RefusesCrossThreadClose:
        def __init__(self) -> None:
            self._connection = object()
            self._running = True

    connection = _RefusesCrossThreadClose()
    connection._connection = _raising_connection()

    with caplog.at_level(logging.DEBUG, logger=sqlite_module.__name__):
        sqlite_module._close_orphaned_aiosqlite(connection)

    assert connection._running is False
    assert "could not close the raw sqlite3 connection" in caplog.text
    assert "ProgrammingError" in caplog.text


def _raising_connection():
    class _Raises:
        def close(self) -> None:
            raise sqlite3.ProgrammingError("SQLite objects created in a thread can only be used in that same thread.")

    return _Raises()


async def test_a_closable_connection_is_closed_and_the_worker_stopped():
    """When the close does go through, both facts are recorded on the handle."""

    class _Closable:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    class _Connection:
        def __init__(self, raw: _Closable) -> None:
            self._connection = raw
            self._running = True

    raw = _Closable()
    connection = _Connection(raw)

    sqlite_module._close_orphaned_aiosqlite(connection)

    assert raw.closed is True
    assert connection._connection is None
    assert connection._running is False


def test_missing_aiosqlite_internals_are_reported_not_swallowed(caplog):
    """A renamed internal must leave a trace, not a silent no-op."""

    class _RenamedConnection:
        pass

    with caplog.at_level(logging.DEBUG, logger=sqlite_module.__name__):
        sqlite_module._close_orphaned_aiosqlite(_RenamedConnection())

    assert sqlite_module._AIOSQLITE_RAW_CONNECTION in caplog.text
    assert sqlite_module._AIOSQLITE_RUNNING_FLAG in caplog.text
    assert "await close()" in caplog.text
