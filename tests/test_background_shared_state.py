"""Shared-state safety for overlapping background executions."""

from __future__ import annotations

import asyncio
import contextlib
import gc
import threading
import weakref
from collections import OrderedDict
from typing import Any

import pytest

from hypergraph import (
    AsyncRunner,
    EventProcessor,
    Graph,
    InMemoryCache,
    RunEndEvent,
    RunStatus,
    SyncRunner,
    node,
)
from hypergraph.checkpointers import (
    CheckpointPolicy,
    SqliteCheckpointer,
    WorkflowStatus,
)


@pytest.mark.parametrize("in_memory", [False, True], ids=["file", "memory"])
def test_sync_background_sqlite_is_readable_and_closable_from_caller(
    tmp_path,
    in_memory: bool,
) -> None:
    """A worker-created sync connection remains owned by its checkpointer."""

    @node(output_name="doubled")
    def double(value: int) -> int:
        return value * 2

    path = ":memory:" if in_memory else str(tmp_path / "background.db")
    checkpointer = SqliteCheckpointer(
        path,
        policy=CheckpointPolicy(durability="sync"),
    )

    try:
        handle = SyncRunner(checkpointer=checkpointer).start_run(
            Graph([double]),
            value=21,
            workflow_id="background-run",
        )
        result = handle.result()

        persisted = checkpointer.get_run("background-run")
        steps = checkpointer.steps("background-run")

        assert result["doubled"] == 42
        assert persisted is not None
        assert persisted.status is WorkflowStatus.COMPLETED
        assert [step.node_name for step in steps] == ["double"]
    finally:
        asyncio.run(checkpointer.close())


def test_two_sync_background_runs_share_one_sqlite_checkpointer(tmp_path) -> None:
    """Distinct live workflow IDs persist separate truthful rows."""
    first_entered = threading.Event()
    release_first = threading.Event()

    @node(output_name="doubled")
    def gated_double(value: int) -> int:
        if value == 1:
            first_entered.set()
            if not release_first.wait(timeout=5):
                raise AssertionError("first background run never released")
        return value * 2

    checkpointer = SqliteCheckpointer(
        str(tmp_path / "two-background-runs.db"),
        policy=CheckpointPolicy(durability="sync"),
    )
    runner = SyncRunner(checkpointer=checkpointer)
    handles = []

    try:
        handles.append(
            runner.start_run(
                Graph([gated_double]),
                value=1,
                workflow_id="background-1",
            )
        )
        assert first_entered.wait(timeout=5)
        handles.append(
            runner.start_run(
                Graph([gated_double]),
                value=2,
                workflow_id="background-2",
            )
        )
        release_first.set()
        results = [handle.result() for handle in handles]

        assert [result["doubled"] for result in results] == [2, 4]
        assert [
            checkpointer.get_run("background-1").status,
            checkpointer.get_run("background-2").status,
        ] == [WorkflowStatus.COMPLETED, WorkflowStatus.COMPLETED]
    finally:
        release_first.set()
        for handle in handles:
            with contextlib.suppress(Exception):
                handle.result(raise_on_failure=False)
        asyncio.run(checkpointer.close())


class _EvictionRaceOrderedDict(OrderedDict[str, Any]):
    """Pause one hit between membership and LRU movement."""

    def __init__(
        self,
        *args: Any,
        existing_key: str,
        membership_checked: threading.Event,
        eviction_finished: threading.Event,
    ) -> None:
        super().__init__(*args)
        self._existing_key = existing_key
        self._membership_checked = membership_checked
        self._eviction_finished = eviction_finished
        self._coordinated = False

    def __contains__(self, key: object) -> bool:
        present = super().__contains__(key)
        if key == self._existing_key and present and not self._coordinated:
            self._coordinated = True
            self._membership_checked.set()
            self._eviction_finished.wait(timeout=1)
        return present

    def popitem(self, last: bool = True) -> tuple[str, Any]:
        item = super().popitem(last=last)
        self._eviction_finished.set()
        return item


def test_overlapping_sync_handles_keep_in_memory_lru_operations_atomic() -> None:
    """An eviction cannot split another handle's compound cache hit."""
    membership_checked = threading.Event()
    eviction_finished = threading.Event()
    cache = InMemoryCache(max_size=1)

    @node(output_name="doubled", cache=True)
    def double(value: int) -> int:
        return value * 2

    graph = Graph([double])
    runner = SyncRunner(cache=cache)
    assert runner.run(graph, value=1)["doubled"] == 2

    existing_key = next(iter(cache._data))
    cache._data = _EvictionRaceOrderedDict(
        cache._data,
        existing_key=existing_key,
        membership_checked=membership_checked,
        eviction_finished=eviction_finished,
    )

    cached = runner.start_run(graph, value=1, workflow_id="cached")
    assert membership_checked.wait(timeout=5)
    uncached = runner.start_run(graph, value=2, workflow_id="uncached")

    assert cached.result()["doubled"] == 2
    assert uncached.result()["doubled"] == 4


class _RunEndRecorder(EventProcessor):
    def __init__(self, terminal: asyncio.Event) -> None:
        self._terminal = terminal
        self.events: list[RunEndEvent] = []

    def on_event(self, event: object) -> None:
        if isinstance(event, RunEndEvent):
            self.events.append(event)
            self._terminal.set()


async def test_async_runner_retains_task_after_handle_is_discarded() -> None:
    """Runner ownership keeps execution alive without a caller-held handle."""
    entered = asyncio.Event()
    node_finished = asyncio.Event()
    terminal = asyncio.Event()
    recorder = _RunEndRecorder(terminal)
    waiter_ref: weakref.ReferenceType[asyncio.Future[None]] | None = None

    @node(output_name="doubled")
    async def gated_double(value: int) -> int:
        nonlocal waiter_ref
        waiter = asyncio.get_running_loop().create_future()
        waiter_ref = weakref.ref(waiter)
        entered.set()
        await waiter
        node_finished.set()
        return value * 2

    runner = AsyncRunner()
    handle = runner.start_run(
        Graph([gated_double]),
        value=21,
        event_processors=[recorder],
    )
    await asyncio.wait_for(entered.wait(), timeout=5)

    del handle
    for _ in range(3):
        gc.collect()
        await asyncio.sleep(0)

    assert waiter_ref is not None
    waiter = waiter_ref()
    assert waiter is not None, "discarding the handle collected the live execution"
    waiter.set_result(None)
    await asyncio.wait_for(node_finished.wait(), timeout=5)
    await asyncio.wait_for(terminal.wait(), timeout=5)

    assert len(recorder.events) == 1
    assert recorder.events[0].status.value == RunStatus.COMPLETED.value
