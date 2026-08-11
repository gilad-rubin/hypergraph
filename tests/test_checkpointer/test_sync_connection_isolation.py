"""One synchronous connection per THREAD — WAL's promise, kept.

A single shared sync connection deadlocked the durable Host. Three parties,
one cycle:

1. the event loop opens a ``BEGIN IMMEDIATE`` on the ASYNC connection and
   holds SQLite's write lock across its ``await``s (every RunHome write
   transaction is shaped that way — that is what ``_txn_lock`` serializes);
2. the Run executing in a ``to_thread`` worker takes the sync lock and waits
   for that same write lock;
3. the loop then makes one of the documented synchronous reads —
   ``get_run``, ``state``, ``values``, ``result_sync`` — and waits for the
   sync lock the Run thread is holding.

Nothing can commit, because the only thread that could commit the async
transaction is the one blocked in step 3. The cycle broke only when
``busy_timeout`` expired thirty seconds later and the RUN failed with
"database is locked" — a run that had not executed a single node, recorded
as a failure and settled as finished.

WAL exists precisely so a reader never waits for a writer. Sharing one
connection across threads is what took that away; a connection per thread
gives it back, and leaves SQLite's write lock as the only cross-thread
arbitration — the same one two worker PROCESSES on a Run Home already use.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time

import pytest

from hypergraph.checkpointers._migrate import ensure_schema
from hypergraph.checkpointers.sqlite import SqliteCheckpointer

aiosqlite = pytest.importorskip("aiosqlite")

#: How long the async write transaction is held open, in seconds. Long
#: enough that a run thread's write is provably waiting on it, short enough
#: that the test costs about a second.
_HOLD = 1.0


class TestASyncReadNeverWaitsForAnotherThreadsWrite:
    async def test_a_loop_thread_read_is_prompt_while_a_run_thread_writes(self, tmp_path):
        """The deadlock, reproduced exactly, with no load and no timing luck."""
        store = SqliteCheckpointer(str(tmp_path / "runs.db"), durability="sync")
        try:
            await store._ensure_db()
            store.create_run_sync("wf-read", graph_name="probe")

            # (1) The loop holds a write transaction open across an await.
            txn = store._txn_lock()
            await txn.acquire()
            await store._db.execute("BEGIN IMMEDIATE")
            await store._db.execute("UPDATE runs SET status = 'active' WHERE id = 'wf-read'")

            # (2) A run thread commits a checkpoint write and waits for the
            # write lock the transaction above is holding.
            entered = threading.Event()
            failures: list[BaseException] = []
            waited: list[float] = []

            def run_thread() -> None:
                entered.set()
                started = time.monotonic()
                try:
                    store.create_run_sync("wf-write", graph_name="probe")
                except BaseException as error:  # noqa: BLE001 - reported as a failed assertion
                    failures.append(error)
                waited.append(time.monotonic() - started)

            writer = threading.Thread(target=run_thread, name="run-thread")
            writer.start()
            assert entered.wait(30), "the run thread never started"
            await asyncio.sleep(_HOLD / 2)

            # (3) THE READ. On the event-loop thread, as a notebook, a UI
            # poll or a `*_sync` client call would make it.
            started = time.monotonic()
            assert store.get_run("wf-read") is not None
            blocked = time.monotonic() - started

            await asyncio.sleep(_HOLD / 2)
            await store._db.commit()
            txn.release()
            writer.join(timeout=60)

            assert waited, "the run thread never finished"
            assert waited[0] >= _HOLD / 4, "the run thread was not actually waiting on the write lock — the scenario did not happen"
            assert blocked < _HOLD, f"a sync read on the event-loop thread waited {blocked:.1f}s behind another thread's write"
            assert not failures, f"the run thread's checkpoint write failed: {failures[0]!r}"
        finally:
            await store.close()

    async def test_a_connection_opened_under_a_live_writer_waits_out_the_lock(self, tmp_path):
        """Say how long you will wait BEFORE taking a database-wide lock.

        ``PRAGMA journal_mode`` locks the whole database, and it ran before
        ``PRAGMA busy_timeout`` on every connection this store opens — so a
        second Home opened at the instant another writer held the lock died
        on the spot with "database is locked" instead of waiting the
        thirty seconds it was configured to wait.
        """
        path = tmp_path / "runs.db"
        # Schema only, in the default rollback-journal mode: the store opened
        # below is the one that has to convert this file to WAL.
        raw = sqlite3.connect(str(path))
        ensure_schema(raw)
        raw.close()

        holder = sqlite3.connect(str(path), check_same_thread=False)
        holder.execute("PRAGMA busy_timeout=60000")
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("INSERT INTO runs (id, status) VALUES ('wf', 'active')")

        released = threading.Thread(target=lambda: (time.sleep(_HOLD / 2), holder.commit(), holder.close()))
        released.start()
        try:
            opened = SqliteCheckpointer(str(path))
            try:
                started = time.monotonic()
                await opened._ensure_db()  # must WAIT for the writer, not refuse
                waited = time.monotonic() - started
                assert waited >= _HOLD / 4, "the writer's lock was not in the way — the scenario did not happen"
                assert opened.get_run("wf") is not None
            finally:
                await opened.close()
        finally:
            released.join(timeout=30)

    async def test_each_thread_gets_its_own_connection_and_close_closes_them_all(self, tmp_path):
        """Per-thread only pays off if teardown still reaches every one."""
        store = SqliteCheckpointer(str(tmp_path / "runs.db"))
        store.create_run_sync("wf", graph_name="probe")
        elsewhere: list[sqlite3.Connection] = []

        def read_from_another_thread() -> None:
            store.get_run("wf")
            elsewhere.append(store._sync_conn)

        thread = threading.Thread(target=read_from_another_thread)
        thread.start()
        thread.join()

        assert elsewhere[0] is not None
        assert elsewhere[0] is not store._sync_conn, "two threads must not share one connection"

        await store.close()

        with pytest.raises(sqlite3.ProgrammingError):
            elsewhere[0].execute("SELECT 1")
