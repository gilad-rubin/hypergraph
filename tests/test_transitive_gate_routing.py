"""Transitive routing across chained gates (issue #220).

For a chain ``gate_a -> gate_b -> process`` where every node can read a graph
input directly, terminating the route at ``gate_a`` must also block ``gate_b``
and ``process``. Data readiness must not bypass an unselected or terminated
control path — and flipping the gate decisions must flip the outcome (no
over-blocking).
"""

from __future__ import annotations

import pytest

from hypergraph import END, AsyncRunner, Graph, SyncRunner, node, route
from hypergraph.events import EventProcessor
from hypergraph.events.types import NodeEndEvent, NodeStartEvent

RUNNER_KINDS = ("sync", "async")


class ListProcessor(EventProcessor):
    """Collects all events for assertion."""

    def __init__(self):
        self.events: list = []

    def on_event(self, event):
        self.events.append(event)

    def node_names(self, *event_types) -> set[str]:
        types = event_types or (NodeStartEvent, NodeEndEvent)
        return {e.node_name for e in self.events if isinstance(e, types)}


async def run_graph(runner_kind: str, graph: Graph, inputs: dict, **kwargs):
    if runner_kind == "sync":
        return SyncRunner().run(graph, inputs, **kwargs)
    return await AsyncRunner().run(graph, inputs, **kwargs)


def build_chain_graph() -> Graph:
    """gate_a -> gate_b -> process, all three read graph input ``x``."""

    @node(output_name="processed")
    def process(x: int) -> int:
        return x * 10

    @route(targets=["process", END])
    def gate_b(x: int, b_end: bool):
        return END if b_end else "process"

    @route(targets=["gate_b", END])
    def gate_a(x: int, a_end: bool):
        return END if a_end else "gate_b"

    return Graph([gate_a, gate_b, process], name="chain")


# ---------------------------------------------------------------------------
# C1 — END at the first gate blocks the whole downstream chain
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("runner_kind", RUNNER_KINDS)
async def test_end_at_first_gate_blocks_terminal_node(runner_kind):
    graph = build_chain_graph()
    listener = ListProcessor()

    result = await run_graph(
        runner_kind,
        graph,
        {"x": 1, "a_end": True, "b_end": False},
        event_processors=[listener],
    )

    assert result.completed
    assert "processed" not in result.values, "process must not run when gate_a returns END"
    executed = listener.node_names()
    assert "process" not in executed
    assert "gate_b" not in executed
    assert "gate_a" in executed


# ---------------------------------------------------------------------------
# C2 — falsifier: both gates select, terminal node runs normally
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("runner_kind", RUNNER_KINDS)
async def test_both_gates_select_terminal_node_executes(runner_kind):
    graph = build_chain_graph()
    listener = ListProcessor()

    result = await run_graph(
        runner_kind,
        graph,
        {"x": 1, "a_end": False, "b_end": False},
        event_processors=[listener],
    )

    assert result.completed
    assert result["processed"] == 10
    executed = listener.node_names()
    assert {"gate_a", "gate_b", "process"} <= executed


# ---------------------------------------------------------------------------
# C3 — mid-chain END: first gate selects, second gate terminates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("runner_kind", RUNNER_KINDS)
async def test_mid_chain_end_blocks_terminal_node(runner_kind):
    graph = build_chain_graph()
    listener = ListProcessor()

    result = await run_graph(
        runner_kind,
        graph,
        {"x": 1, "a_end": False, "b_end": True},
        event_processors=[listener],
    )

    assert result.completed
    assert "processed" not in result.values
    executed = listener.node_names()
    assert "process" not in executed
    assert {"gate_a", "gate_b"} <= executed


# ---------------------------------------------------------------------------
# C4 — a blocked node leaves no execution artifacts on any surface:
#      result values, events, checkpoint steps, progress rows.
# ---------------------------------------------------------------------------


def _progress_row_names(progress) -> set[str]:
    return {state.name for state in progress._tracker.node_bars.values()}


async def test_blocked_node_has_no_artifacts_async_runner():
    from hypergraph.checkpointers import MemoryCheckpointer
    from hypergraph.events.rich_progress import RichProgressProcessor

    graph = build_chain_graph()
    listener = ListProcessor()
    progress = RichProgressProcessor(force_mode="non-tty", transient=True)
    checkpointer = MemoryCheckpointer()
    runner = AsyncRunner(checkpointer=checkpointer)

    result = await runner.run(
        graph,
        {"x": 1, "a_end": True, "b_end": False},
        workflow_id="wf-blocked",
        event_processors=[listener, progress],
    )

    assert result.completed
    steps = await checkpointer.get_steps("wf-blocked")
    step_names = {step.node_name for step in steps}
    log_names = {record.node_name for record in result.log.steps} if result.log else set()

    for blocked in ("gate_b", "process"):
        assert blocked not in result.values
        assert "processed" not in result.values
        assert blocked not in listener.node_names()
        assert blocked not in step_names, f"no checkpoint StepRecord for {blocked}"
        assert blocked not in log_names, f"no RunLog record for {blocked}"
        assert blocked not in _progress_row_names(progress), f"no progress row for {blocked}"

    # The other direction: the node that DID run has every artifact.
    assert "gate_a" in listener.node_names()
    assert "gate_a" in step_names
    assert "gate_a" in log_names
    assert "gate_a" in _progress_row_names(progress)


def test_blocked_node_has_no_artifacts_sync_runner(tmp_path):
    pytest.importorskip("aiosqlite")
    from hypergraph.checkpointers import SqliteCheckpointer
    from hypergraph.checkpointers._migrate import ensure_schema
    from hypergraph.events.rich_progress import RichProgressProcessor

    checkpointer = SqliteCheckpointer(str(tmp_path / "chain.db"))
    db = checkpointer._sync_db()
    ensure_schema(db)
    try:
        graph = build_chain_graph()
        listener = ListProcessor()
        progress = RichProgressProcessor(force_mode="non-tty", transient=True)
        runner = SyncRunner(checkpointer=checkpointer)

        result = runner.run(
            graph,
            {"x": 1, "a_end": True, "b_end": False},
            workflow_id="wf-blocked-sync",
            event_processors=[listener, progress],
        )

        assert result.completed
        rows = db.execute(
            "SELECT node_name FROM steps WHERE run_id = ?",
            ("wf-blocked-sync",),
        ).fetchall()
        step_names = {row[0] for row in rows}
        log_names = {record.node_name for record in result.log.steps} if result.log else set()

        for blocked in ("gate_b", "process"):
            assert "processed" not in result.values
            assert blocked not in listener.node_names()
            assert blocked not in step_names, f"no checkpoint StepRecord for {blocked}"
            assert blocked not in log_names, f"no RunLog record for {blocked}"
            assert blocked not in _progress_row_names(progress), f"no progress row for {blocked}"

        assert "gate_a" in listener.node_names()
        assert "gate_a" in step_names
        assert "gate_a" in log_names
        assert "gate_a" in _progress_row_names(progress)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# C6 — no collateral damage: independent (ungated) paths keep running
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("runner_kind", RUNNER_KINDS)
async def test_ungated_sibling_still_runs_when_chain_terminates(runner_kind):
    """A node with no controlling gate runs regardless of the gate chain."""

    @node(output_name="processed")
    def process(x: int) -> int:
        return x * 10

    @node(output_name="side_out")
    def side(x: int) -> int:
        return x + 100

    @route(targets=["process", END])
    def gate_b(x: int, b_end: bool):
        return END if b_end else "process"

    @route(targets=["gate_b", END])
    def gate_a(x: int, a_end: bool):
        return END if a_end else "gate_b"

    graph = Graph([gate_a, gate_b, process, side], name="chain_with_side")

    result = await run_graph(runner_kind, graph, {"x": 1, "a_end": True, "b_end": False})

    assert result.completed
    assert result["side_out"] == 101, "ungated node must keep running"
    assert "processed" not in result.values


@pytest.mark.parametrize("runner_kind", RUNNER_KINDS)
async def test_gated_node_with_ungated_data_feed_is_still_blocked(runner_kind):
    """A node whose ONLY control path is a dead gate chain stays blocked even
    when an ungated node produces its data input."""

    @node(output_name="prepared")
    def prep(x: int) -> int:
        return x + 1

    @node(output_name="final")
    def consume(prepared: int) -> int:
        return prepared * 2

    @route(targets=["consume", END])
    def gate_b(x: int, b_end: bool):
        return END if b_end else "consume"

    @route(targets=["gate_b", END])
    def gate_a(x: int, a_end: bool):
        return END if a_end else "gate_b"

    graph = Graph([gate_a, gate_b, prep, consume], name="chain_with_feed")
    listener = ListProcessor()

    result = await run_graph(
        runner_kind,
        graph,
        {"x": 1, "a_end": True, "b_end": False},
        event_processors=[listener],
    )

    assert result.completed
    assert result["prepared"] == 2, "ungated producer still runs"
    assert "final" not in result.values, "gate-targeted consumer must stay blocked"
    assert "consume" not in listener.node_names()


@pytest.mark.parametrize("runner_kind", RUNNER_KINDS)
@pytest.mark.parametrize("head_end", [True, False])
async def test_multi_target_gate_behind_terminated_gate(runner_kind, head_end):
    """A multi-target gate chained behind a routing gate: END upstream blocks
    every fanned-out target; selection activates all of them."""

    @node(output_name="left_out")
    def left(x: int) -> int:
        return x + 1

    @node(output_name="right_out")
    def right(x: int) -> int:
        return x + 2

    @route(targets=["left", "right"], multi_target=True)
    def fanout(x: int) -> list[str]:
        return ["left", "right"]

    @route(targets=["fanout", END])
    def gate_head(x: int, head_end: bool):
        return END if head_end else "fanout"

    graph = Graph([gate_head, fanout, left, right], name="chain_multi")

    result = await run_graph(runner_kind, graph, {"x": 1, "head_end": head_end})

    assert result.completed
    if head_end:
        assert "left_out" not in result.values
        assert "right_out" not in result.values
    else:
        assert result["left_out"] == 2
        assert result["right_out"] == 3


# ---------------------------------------------------------------------------
# C7 — chained gates in a cycle: gate-driven re-activation still re-executes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("runner_kind", RUNNER_KINDS)
async def test_chained_gate_cycle_refires_until_end(runner_kind):
    """increment -> gate_outer -> gate_inner -> increment loops until END.

    The transitive-blocking fix must not latch a gated node as permanently
    blocked: each iteration's routing decision re-activates the chain.
    """

    @node(output_name="count")
    def increment(count: int) -> int:
        return count + 1

    @route(targets=["increment"])
    def gate_inner(count: int) -> str:
        return "increment"

    @route(targets=["gate_inner", END])
    def gate_outer(count: int):
        return "gate_inner" if count < 3 else END

    graph = Graph(
        [increment, gate_outer, gate_inner],
        name="chained_gate_cycle",
        entrypoint="increment",
    )
    listener = ListProcessor()

    result = await run_graph(runner_kind, graph, {"count": 0}, event_processors=[listener])

    assert result.completed
    assert result["count"] == 3
    increments = [e for e in listener.events if isinstance(e, NodeEndEvent) and e.node_name == "increment"]
    assert len(increments) == 3, "gate-controlled node must re-fire on each routed iteration"
