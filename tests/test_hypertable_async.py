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
async def test_second_cancel_during_write_drain_still_settles(tmp_path) -> None:
    """A second CancelledError arriving while a cancelled write is DRAINING
    must restart the wait — never surface while the store thread is still
    mutating. (Guards the drain loop in ``to_thread_settled``.)"""
    started = threading.Event()
    proceed = threading.Event()
    finished = threading.Event()

    class ParkedStore(LanceDBStore):
        thread_safe = True

        def write_rows(self, *args, **kwargs):
            started.set()
            proceed.wait(15)
            result = super().write_rows(*args, **kwargs)
            finished.set()
            return result

    table = Graph([clean, count_words]).as_table(identity="doc_id", store=ParkedStore(str(tmp_path / "s")), runner=AsyncRunner())
    task = asyncio.create_task(table.insert(doc_id="d1", text="Hello World"))
    assert await asyncio.to_thread(started.wait, 15)

    task.cancel()
    for _ in range(5):
        await asyncio.sleep(0)
    task.cancel()  # lands inside the drain wait
    for _ in range(5):
        await asyncio.sleep(0)
    assert not task.done(), "second cancel escaped the drain while the store thread was parked"

    proceed.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert finished.is_set(), "cancellation surfaced while the store write was still running"


@pytest.mark.asyncio
@pytest.mark.parametrize(("configured_window", "expected"), [(None, 16), (4, 4)])
async def test_child_pages_fill_the_configured_fanout_window(tmp_path, configured_window, expected) -> None:
    """Independent child pages run concurrently; the default window is 16."""
    active = 0
    high_water = 0
    filled = asyncio.Event()
    release = asyncio.Event()

    @node(output_name="pages")
    def split(text: str) -> list[dict[str, object]]:
        return [{"page_id": str(index), "body": f"{text}-{index}"} for index in range(20)]

    @node(output_name="embedding")
    async def embed(body: str) -> str:
        nonlocal active, high_water
        active += 1
        high_water = max(high_water, active)
        if active >= expected:
            filled.set()
        try:
            await release.wait()
            return body.upper()
        finally:
            active -= 1

    pages = Graph([embed], name="page")
    graph = Graph([split, pages.as_node().map_over("pages", identity="page_id")])
    kwargs = {} if configured_window is None else {"page_max_concurrency": configured_window}
    table = graph.as_table(
        identity="doc_id",
        store=LanceDBStore(str(tmp_path / "fanout")),
        runner=AsyncRunner(),
        **kwargs,
    )

    task = asyncio.create_task(table.insert(doc_id="d1", text="page"))
    await asyncio.wait_for(filled.wait(), timeout=5)
    assert high_water == expected
    release.set()
    await asyncio.wait_for(task, timeout=10)
    assert len(table.child("page").rows(parent="d1")) == 20


@pytest.mark.asyncio
async def test_child_page_failure_cancels_and_drains_the_window(tmp_path) -> None:
    """One failed page cannot leave sibling child graphs running after insert returns."""
    active = 0
    started = 0
    high_water = 0
    window_filled = asyncio.Event()
    never = asyncio.Event()

    @node(output_name="pages")
    def split(text: str) -> list[dict[str, str]]:
        return [{"page_id": str(index), "body": f"{text}-{index}"} for index in range(8)]

    @node(output_name="embedding")
    async def embed(body: str) -> str:
        nonlocal active, high_water, started
        active += 1
        started += 1
        high_water = max(high_water, active)
        if started == 4:
            window_filled.set()
        try:
            await window_filled.wait()
            if body.endswith("-0"):
                raise RuntimeError("page failed")
            await never.wait()
            return body
        finally:
            active -= 1

    pages = Graph([embed], name="page")
    table = Graph([split, pages.as_node().map_over("pages", identity="page_id")]).as_table(
        identity="doc_id",
        store=LanceDBStore(str(tmp_path / "failed-fanout")),
        runner=AsyncRunner(),
        page_max_concurrency=4,
    )

    with pytest.raises(RuntimeError, match="page failed"):
        await asyncio.wait_for(table.insert(doc_id="d1", text="page"), timeout=5)
    assert active == 0
    assert high_water == 4
    assert started < 8, "failure must cancel queued pages rather than draining the whole input"


@pytest.mark.asyncio
async def test_second_cancel_cannot_interrupt_child_window_drain(tmp_path) -> None:
    """Repeated caller cancellation cannot return while a sibling is settling."""
    both_started = asyncio.Event()
    sibling_settling = asyncio.Event()
    sibling_settled = asyncio.Event()
    proceed = asyncio.Event()
    started = 0

    @node(output_name="pages")
    def split(text: str) -> list[dict[str, str]]:
        return [{"page_id": str(index), "body": f"{text}-{index}"} for index in range(2)]

    @node(output_name="embedding")
    async def embed(body: str) -> str:
        nonlocal started
        started += 1
        if started == 2:
            both_started.set()
        await both_started.wait()
        if body.endswith("-0"):
            raise RuntimeError("page failed")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            sibling_settling.set()
            await proceed.wait()
            sibling_settled.set()
            raise

    pages = Graph([embed], name="page")
    table = Graph([split, pages.as_node().map_over("pages", identity="page_id")]).as_table(
        identity="doc_id",
        store=LanceDBStore(str(tmp_path / "repeated-cancel")),
        runner=AsyncRunner(),
        page_max_concurrency=2,
    )
    task = asyncio.create_task(table.insert(doc_id="d1", text="page"))
    await asyncio.wait_for(sibling_settling.wait(), timeout=5)

    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    proceed.set()
    with pytest.raises(RuntimeError, match="page failed"):
        await asyncio.wait_for(task, timeout=5)
    assert sibling_settled.is_set()


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
