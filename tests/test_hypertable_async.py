"""Async HyperTable mutation behavior."""

from __future__ import annotations

import asyncio
import threading

import pytest

from hypergraph import Graph, node
from hypergraph.materialization._lancedb_store import LanceDBStore
from hypergraph.runners import AsyncRunner


@node(output_name="clean_text")
async def clean(text: str) -> str:
    return text.strip().lower()


@node(output_name="word_count")
def count_words(clean_text: str) -> int:
    return len(clean_text.split())


@pytest.fixture
def table(tmp_path):
    return Graph([clean, count_words]).as_table(identity="doc_id", store=LanceDBStore(str(tmp_path / "async_store")), runner=AsyncRunner())


class ThreadRecordingStore(LanceDBStore):
    """Records which thread each store write runs on."""

    def __init__(self, path: str, seen: list[int]) -> None:
        super().__init__(path)
        self.seen = seen

    def write_rows(self, *args, **kwargs):
        self.seen.append(threading.get_ident())
        return super().write_rows(*args, **kwargs)

    def delete_rows(self, *args, **kwargs):
        self.seen.append(threading.get_ident())
        return super().delete_rows(*args, **kwargs)


class ThreadSafeRecordingStore(ThreadRecordingStore):
    thread_safe = True


def _recording_table(tmp_path, store: ThreadRecordingStore):
    return Graph([clean, count_words]).as_table(identity="doc_id", store=store, runner=AsyncRunner())


@pytest.mark.asyncio
async def test_thread_safe_store_write_io_leaves_the_event_loop(tmp_path) -> None:
    """A store declaring thread_safe=True gets its write-plan store IO run on
    worker threads: a synchronous store client must not stall every other
    coroutine for the duration of each segment's IO."""
    seen: list[int] = []
    table = _recording_table(tmp_path, ThreadSafeRecordingStore(str(tmp_path / "s"), seen))
    loop_thread = threading.get_ident()

    receipt = await table.insert(doc_id="d1", text="Hello World")
    await table.delete("d1")

    assert receipt.id == "d1"
    assert seen, "no store writes recorded"
    assert all(thread != loop_thread for thread in seen)


@pytest.mark.asyncio
async def test_default_store_write_io_stays_on_the_calling_thread(tmp_path) -> None:
    """thread_safe defaults to False: a store may keep thread-affine state
    (a sqlite3 connection opened with check_same_thread), so every store call
    must stay inline on the thread that invoked the table API."""
    seen: list[int] = []
    table = _recording_table(tmp_path, ThreadRecordingStore(str(tmp_path / "s"), seen))
    loop_thread = threading.get_ident()

    receipt = await table.insert(doc_id="d1", text="Hello World")
    await table.delete("d1")

    assert receipt.id == "d1"
    assert seen, "no store writes recorded"
    assert all(thread == loop_thread for thread in seen)


@pytest.mark.asyncio
async def test_cancelled_write_settles_before_cancellation_surfaces(tmp_path) -> None:
    """A worker thread cannot be interrupted, so cancelling an async write
    must wait for the in-flight store step to settle — the caller must never
    observe cancellation while the store is still mutating."""
    import time

    started = threading.Event()
    finished = threading.Event()

    class SlowStore(LanceDBStore):
        thread_safe = True

        def write_rows(self, *args, **kwargs):
            started.set()
            time.sleep(0.3)
            result = super().write_rows(*args, **kwargs)
            finished.set()
            return result

    table = Graph([clean, count_words]).as_table(identity="doc_id", store=SlowStore(str(tmp_path / "s")), runner=AsyncRunner())
    task = asyncio.create_task(table.insert(doc_id="d1", text="Hello World"))
    assert await asyncio.to_thread(started.wait, 5)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert finished.is_set(), "cancellation surfaced while the store write was still running"


@pytest.mark.asyncio
async def test_async_insert_update_set_delete(table) -> None:
    """AsyncRunner-bound tables expose awaitable mutations with derived outputs."""

    await table.insert(doc_id="d1", text="Hello World", active=False)
    assert table.get("d1")["clean_text"] == "hello world"

    await table.update("d1", text="one two three")
    assert table.get("d1")["word_count"] == 3

    assert await table.set([("doc_id", "eq", "d1")], active=True, station="NICU") == 1
    assert table.get("d1")["station"] == "NICU"

    await table.delete("d1")
    assert table.get("d1") is None


@pytest.mark.asyncio
async def test_async_sync_reconciles_rows(table) -> None:
    """sync() is awaitable and returns the same reconciliation result as sync tables."""

    result = await table.sync(
        [
            {"doc_id": "d1", "text": "unchanged"},
            {"doc_id": "d2", "text": "will change"},
        ]
    )
    assert result.inserted == 2

    result = await table.sync(
        [
            {"doc_id": "d1", "text": "unchanged"},
            {"doc_id": "d2", "text": "changed text"},
            {"doc_id": "d3", "text": "brand new"},
        ]
    )

    assert result.skipped == 1
    assert result.updated == 1
    assert result.inserted == 1
    assert table.get("d2")["word_count"] == 2


@pytest.mark.asyncio
async def test_async_backfill_populates_null_columns(tmp_path) -> None:
    """backfill() runs the graph under AsyncRunner — a newly added column is derived,
    not silently left null. Regression: recompute/backfill had no async dispatch and
    fed an un-awaited coroutine to _extract_outputs, deriving nothing."""
    store = LanceDBStore(str(tmp_path / "async_backfill_store"))

    table_v1 = Graph([clean]).as_table(identity="doc_id", store=store, runner=AsyncRunner())
    await table_v1.insert(doc_id="d1", text="hello world")
    await table_v1.insert(doc_id="d2", text="one two three")

    table_v2 = Graph([clean, count_words]).as_table(identity="doc_id", store=store, runner=AsyncRunner())
    await table_v2.rederive("word_count", missing_only=True)

    assert table_v2.get("d1")["word_count"] == 2
    assert table_v2.get("d2")["word_count"] == 3


@pytest.mark.asyncio
async def test_async_recompute_rederives_column(tmp_path) -> None:
    """recompute() is awaitable under AsyncRunner and re-derives through the async
    graph instead of leaving a stale value (or raising on an un-awaitable None)."""
    store = LanceDBStore(str(tmp_path / "async_recompute_store"))
    table = Graph([clean, count_words]).as_table(identity="doc_id", store=store, runner=AsyncRunner())

    await table.insert(doc_id="d1", text="hello world")
    assert table.get("d1")["word_count"] == 2

    await table.rederive("word_count")
    assert table.get("d1")["word_count"] == 2
