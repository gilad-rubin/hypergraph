"""Pin per-item interrupts in runner-level ``.map()`` (superposition PRD 0027 F2).

A mapped batch is N independent runs: one item pausing at an ``@interrupt``
must not poison its siblings. These tests pin the per-item statuses
(``[completed, paused, completed]``) and that the paused item carries its
identifiable resume handle (the hierarchical child ``workflow_id``) plus the
full pause payload.

Deliberately NOT pinned — in-place resume of a paused map child. Resuming
``run(graph, ..., workflow_id="batch/1")`` is rejected ('/' is reserved for
hierarchy), and re-running the whole ``.map()`` does not thread resume values
into a paused child's checkpoint. That in-map resume API is intentionally not
built: in the superposition model a batch is N independent admissions, so
resuming a parked item means re-running THAT item as its own fresh run with
the answer supplied up-front — which the second half of the pin demonstrates.
An in-place map-child resume would be a convenience no current consumer needs.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from hypergraph import AsyncRunner, Graph, RunStatus, interrupt, node
from hypergraph.checkpointers import CheckpointPolicy, SqliteCheckpointer

aiosqlite = pytest.importorskip("aiosqlite")


@node(output_name="draft")
def make_draft(x: int) -> str:
    return f"draft-{x}"


@interrupt(output_name="decision")
def approval(draft: str, x: int) -> str:
    # Auto-resolves for every item except x == 2, which pauses for a human.
    return None if x == 2 else "auto"


@node(output_name="result")
def finalize(decision: str, x: int) -> str:
    return f"{x}:{decision}"


@pytest_asyncio.fixture
async def checkpointer(tmp_path):
    cp = SqliteCheckpointer(str(tmp_path / "cp.db"))
    cp.policy = CheckpointPolicy(durability="sync", retention="full")
    yield cp
    await cp.close()


@pytest.mark.asyncio
async def test_map_pauses_one_item_without_poisoning_the_batch(checkpointer):
    runner = AsyncRunner(checkpointer=checkpointer)
    graph = Graph([make_draft, approval, finalize])

    batch = await runner.map(graph, {"x": [1, 2, 3]}, map_over="x", workflow_id="batch")

    assert [r.status for r in batch.results] == [
        RunStatus.COMPLETED,
        RunStatus.PAUSED,
        RunStatus.COMPLETED,
    ]
    assert batch.paused is True
    assert [r.get("result") for r in batch.results] == ["1:auto", None, "3:auto"]

    # The paused item is individually identifiable: it carries its own
    # hierarchical workflow id and the full pause payload.
    paused = batch.results[1]
    assert paused.workflow_id == "batch/1"
    assert paused.pause is not None
    assert paused.pause.node_name == "approval"
    assert paused.pause.response_key == "decision"
    assert paused.pause.value == "draft-2"


@pytest.mark.asyncio
async def test_paused_item_is_re_drivable_alone_as_a_fresh_run(checkpointer):
    runner = AsyncRunner(checkpointer=checkpointer)
    graph = Graph([make_draft, approval, finalize])

    batch = await runner.map(graph, {"x": [1, 2, 3]}, map_over="x", workflow_id="batch")
    paused = batch.results[1]
    assert paused.status == RunStatus.PAUSED

    # The supported resume path: re-drive THAT item's inputs as an independent
    # fresh run with the answer seeded up-front — the superposition door model
    # (re-drive the graph fresh, no checkpointer, interrupts seeded from
    # durable truth). On a checkpointer-free runner ``is_resuming`` is always
    # True, so a seeded interrupt output auto-resolves instead of pausing; a
    # checkpointer-bearing runner deliberately does NOT consume up-front seeds
    # on a fresh workflow (the false-auto-resolve guard in the interrupt
    # executor), which is why the re-drive happens on a fresh runner.
    resumed = await AsyncRunner().run(graph, {"x": 2, paused.pause.response_key: "human"})
    assert resumed.status == RunStatus.COMPLETED
    assert resumed["result"] == "2:human"
