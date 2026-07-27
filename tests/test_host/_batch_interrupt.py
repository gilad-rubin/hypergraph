"""Shared harness for the issue #342 durable-Batch interrupt suites.

Deliberately small: one worker context manager and the four read/act
helpers every suite needs. The two fixtures they run against (`home`,
`ledger`) live in ``conftest.py``, because pytest resolves fixtures by
name and importing one shadows every test that uses it.
Everything else belongs to the suite that falsifies it — a helper used in
one file is not shared vocabulary, it is that file's setup.
"""

from __future__ import annotations

import asyncio
import contextlib

from hypergraph import WaitingCondition
from hypergraph.checkpointers.types import PauseSlot
from tests.test_host._ingestion_fixture import answer_value


@contextlib.asynccontextmanager
async def worker(host, worker_id: str = "w-342", **kwargs):
    """Run work_forever as a task; shut it down cleanly on exit."""
    task = asyncio.create_task(host.work_forever(worker_id, **kwargs))
    try:
        yield task
    finally:
        host.shutdown()
        await asyncio.wait_for(task, timeout=25)


async def until(check, timeout: float = 20.0, interval: float = 0.02):
    """Poll an async zero-arg callable until it returns something truthy."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        value = await check()
        if value:
            return value
        if loop.time() > deadline:
            raise AssertionError("timed out waiting for condition")
        await asyncio.sleep(interval)


async def collect(stream, *, timeout: float = 25.0, stop_when=None):
    """Drain a watch stream under a deadline, optionally detaching early.

    A watch stream ends on its own once its subject is accounted, so no
    healthy test needs the deadline — it exists so a stream that never
    ends reports a failure instead of hanging the suite forever.
    """
    collected: list = []

    async def drain() -> None:
        async for update in stream:
            collected.append(update)
            if stop_when is not None and stop_when(update):
                return

    async with contextlib.aclosing(stream):
        try:
            await asyncio.wait_for(drain(), timeout=timeout)
        except asyncio.TimeoutError:
            raise AssertionError(f"watch stream did not end within {timeout}s ({len(collected)} updates seen)") from None
    return collected


def paused_items(view) -> list[str]:
    return [key for key, item in view.items.items() if item.waiting is WaitingCondition.PAUSED]


async def batch_where(client, ref, predicate):
    async def check():
        view = await client.get(ref)
        return view if view is not None and predicate(view) else None

    return await until(check)


async def submit_ids(host, graph, ids, workflow_id, **kwargs):
    """THE public submission used everywhere below: graph-first + runner-shaped."""
    return await host.submit_batch(
        graph,
        {"work_item_id": list(ids)},
        map_over="work_item_id",
        key_by="work_item_id",
        workflow_id=workflow_id,
        **kwargs,
    )


async def answer_item(client, item, decision: str, target_doc_id: int | None = None) -> PauseSlot:
    """Answer one item through its OWN RunRef — no workflow-id string built."""
    slot = await client.get_run_slot(item.run_ref)
    return await client.answer(item.run_ref, pause_id=slot.pause_id, value=answer_value(decision, target_doc_id))
