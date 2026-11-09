"""A value a graph binds is part of its definition, never an input of its run.

A mapped batch step inside a durable run checkpoints each item as a child run. The inner graph's own binds reach that
child as values (they are the GraphNode's parent-facing addresses); stored as inputs, a live part (a model client, an
opened corpus) refused every child ("cannot be stored by this checkpointer's serializer"), and its repr, which carries
an address, made each item's signature differ in the process that rebuilt the graph after a restart. Each child is now
rebuilt from its definition, as a top-level run is: the live part is checkpointable, and a restart over new live parts
restores the items that finished.
"""

from __future__ import annotations

import pytest

from hypergraph import AsyncRunner, Graph, node
from hypergraph.checkpointers import SqliteCheckpointer


class Live:
    """A live part, built anew by each process: no store can hold it."""

    def __init__(self, broken: tuple[str, ...] = ()) -> None:
        self.broken = broken
        self.calls: list[str] = []

    def __reduce__(self):  # noqa: ANN204
        raise TypeError("a live part cannot be stored")


@node(output_name="outcome")
async def answer(live: Live, query: str) -> str:
    live.calls.append(query)
    if query in live.broken:
        raise RuntimeError(f"the provider refused {query!r}")
    return f"answered {query}"


@node(output_name="filed")
def file_items(outcomes: list[str]) -> int:
    """The step after the batch: it reads every item's outcome."""
    return len(outcomes)


def evaluation(live: Live) -> Graph:
    """Questions answered as a batch step over a graph that binds a live part, then a step after the batch."""
    case = Graph([answer], name="case").bind(live=live)
    return Graph(
        [case.as_node(name="questions").map_over("query").rename_outputs(outcome="outcomes"), file_items],
        name="evaluation",
    )


@pytest.fixture
async def checkpointer(tmp_path):
    held = SqliteCheckpointer(str(tmp_path / "runs.db"))
    yield held
    await held.close()


async def test_a_batch_step_over_a_graph_holding_a_live_part_checkpoints_each_item(checkpointer) -> None:
    live = Live()
    result = await AsyncRunner(checkpointer=checkpointer).run(evaluation(live), {"query": ["a", "b"]}, workflow_id="eval")

    assert result["outcomes"] == ["answered a", "answered b"] and result["filed"] == 2
    children = {run.id: run for run in checkpointer.runs() if run.id.startswith("eval/questions/")}
    assert sorted(children) == ["eval/questions/0", "eval/questions/1"]
    assert all(run.status.value == "completed" for run in children.values())


async def test_a_restart_over_new_live_parts_restores_the_items_that_finished(checkpointer) -> None:
    runner = AsyncRunner(checkpointer=checkpointer)
    first = Live(broken=("b",))
    with pytest.raises(RuntimeError, match="refused 'b'"):
        await runner.run(evaluation(first), {"query": ["a", "b"]}, workflow_id="eval-restart")
    assert first.calls == ["a", "b"]

    # The process that takes the run over rebuilds the graph over its own live part.
    second = Live()
    result = await runner.run(evaluation(second), workflow_id="eval-restart")

    assert result["outcomes"] == ["answered a", "answered b"] and result["filed"] == 2
    assert second.calls == ["b"], "the item that finished before the restart ran again"


async def test_a_different_value_for_a_bound_name_is_still_an_input(checkpointer) -> None:
    """Only the graph's own object is its definition: a value handed in over a bind is an override, and is stored
    as an input like any other (so an unstorable one still refuses, by name)."""
    case = Graph([answer], name="case").bind(live=Live())
    with pytest.raises(TypeError, match="live"):
        await AsyncRunner(checkpointer=checkpointer).run(case, {"query": "a", "live": Live()}, workflow_id="override")
