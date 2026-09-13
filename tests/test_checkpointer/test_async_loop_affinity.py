"""Issue #408, guard 3 — an async store says which loop it belongs to.

``_txn_lock()`` hands out an ``asyncio.Lock``, and a Lock only checks its
loop on the CONTENDED branch: a caller on the wrong event loop worked
perfectly until two coroutines happened to overlap, and then failed
somewhere unrelated to the mistake ("... is bound to a different event
loop", from inside a transaction).

What this file falsifies:

1. The second loop is refused on its first operation, uncontended.
2. The refusal names both loops and what to do instead.
3. One loop is unchanged: the lock is still created lazily, once, and reused.
4. ``close()`` releases the loop, so the documented reopen-after-close path
   still works — including on a different loop.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

import pytest

from hypergraph.checkpointers import SqliteCheckpointer
from hypergraph.checkpointers.sqlite import WrongEventLoopError

aiosqlite = pytest.importorskip("aiosqlite")


async def on_a_second_loop(work: Callable[[], Coroutine[Any, Any, Any]]) -> Any:
    """Run ``work()`` to completion on a FRESH event loop, in another thread."""
    return await asyncio.to_thread(lambda: asyncio.run(work()))


@pytest.fixture
def store(tmp_path):
    return SqliteCheckpointer(str(tmp_path / "runs.db"))


class TestLoopAffinity:
    async def test_a_second_loop_is_refused_on_its_first_uncontended_operation(self, store):
        await store.list_runs()  # binds the store to THIS loop

        with pytest.raises(WrongEventLoopError):
            await on_a_second_loop(store.list_runs)

        await store.close()

    async def test_the_refusal_names_both_loops_and_the_fix(self, store):
        await store.create_run("r-1", graph_name="g")
        mine = asyncio.get_running_loop()

        with pytest.raises(WrongEventLoopError) as excinfo:
            await on_a_second_loop(lambda: store.create_run("r-2", graph_name="g"))

        message = str(excinfo.value)
        assert hex(id(mine)) in message, message
        assert "How to fix:" in message
        assert "close()" in message
        # The wrong-loop write never happened.
        assert [run.id for run in await store.list_runs()] == ["r-1"]
        await store.close()

    async def test_one_loop_is_unchanged_and_still_creates_one_lazy_lock(self, store):
        assert store._async_txn_lock is None, "no lock before the first async operation"

        await store.create_run("r-1", graph_name="g")
        lock = store._async_txn_lock
        await store.list_runs()

        assert lock is not None and store._async_txn_lock is lock
        assert len(await store.list_runs()) == 1
        await store.close()

    async def test_close_releases_the_loop_so_the_store_reopens_on_another_one(self, store):
        await store.create_run("r-1", graph_name="g")
        await store.close()
        assert store._async_txn_lock is None

        async def reopen_and_read():
            runs = await store.list_runs()
            await store.close()
            return [run.id for run in runs]

        assert await on_a_second_loop(reopen_and_read) == ["r-1"]
        # ...and it comes back to this one just as well.
        assert [run.id for run in await store.list_runs()] == ["r-1"]
        await store.close()

    async def test_a_run_home_carries_the_same_guard(self, tmp_path):
        from hypergraph import RunHome

        home = RunHome.open(f"file:{tmp_path / 'home.db'}")
        await home.list_runs()

        with pytest.raises(WrongEventLoopError, match="event loop"):
            await on_a_second_loop(home.list_runs)

        await home.close()
