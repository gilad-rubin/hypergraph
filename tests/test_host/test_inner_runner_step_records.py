"""Issue #386 — what a table fan-out's inner runner records, and what it does not.

The gap panda hit: a `HyperTable` node running its derivation recipe per page
inside a Host-executed run, whose per-node stats vanished when the notebook
kernel died. This suite pins BOTH halves of the honest answer.

**The seam exists.** A table whose runner holds the Run Home commits one
`runs` row per inner recipe run, parented to the Host Run, with its step
records — and `node_timings` folds them, because it walks
`runs.parent_run_id`.

**The identity does not.** The inner run's workflow id is generated, not
derived from the row it is deriving: `RunGraph` — the single effect a write
plan yields — carries the inputs but no address. So a re-executed materialize
node mints a fresh inner run rather than addressing the same one. That is why
the recording is opt-in-by-accident today and why the design in issue #386
(address the effect, then gate recording behind an explicit `record_steps`
tier) is written down rather than shipped: a wrong address is not a migration
away, it is a re-ingest away.

The assertions below are deliberately written so that landing that design
BREAKS them — an inner run that gains a derived address should fail
`test_an_inner_run_carries_no_address_today` loudly, not pass quietly.
"""

from __future__ import annotations

import pytest

from hypergraph import AsyncRunner, Graph, RunHomeReadModel, node, serve
from hypergraph.materialization._lancedb_store import LanceDBStore
from tests.test_host._batch_interrupt import batch_where, submit_ids, worker

pytest.importorskip("aiosqlite")


def ingest_graph(tmp_path, *, checkpointer, name: str = "ingest_document") -> Graph:
    """A document graph whose middle node is a HyperTable page fan-out."""

    @node(output_name="text")
    def render_page(page: str) -> str:
        return f"rendered-{page}"

    @node(output_name="length")
    def measure(text: str) -> int:
        return len(text)

    table = Graph([render_page, measure], name="page_recipe").as_table(
        identity="page_id",
        store=LanceDBStore(str(tmp_path / "table")),
        runner=AsyncRunner(checkpointer=checkpointer) if checkpointer is not None else AsyncRunner(),
    )

    @node(output_name="page_id")
    def stage(work_item_id: str) -> str:
        return work_item_id

    @node(output_name="page")
    def pick_page(work_item_id: str) -> str:
        return f"{work_item_id}-p1"

    @node(output_name="published")
    def publish(materialization) -> str:
        return f"published:{materialization.id}"

    materialize = table.as_node(name="materialize_pages", output_name="materialization")
    return Graph([stage, pick_page, materialize, publish], name=name).with_runner(AsyncRunner())


async def settled_ingest(host, graph):
    receipt = await submit_ids(host, graph, ["doc-1"], "sweep")
    async with worker(host):
        await batch_where(host.client, receipt.batch_ref, lambda view: view.settled)
    return receipt


async def test_a_recipe_runner_without_a_checkpointer_records_nothing_inner(tmp_path, home, ledger):
    """THE gap, stated: no checkpointer, no durable evidence the recipe ran."""
    graph = ingest_graph(tmp_path, checkpointer=None)
    host = serve(graph, home=home, deployment_version="v1")
    await settled_ingest(host, graph)

    timings = await RunHomeReadModel(host.client).node_timings()

    assert {timing.node_name for timing in timings.nodes} == {"stage", "pick_page", "materialize_pages", "publish"}
    # The recipe's own nodes ran — the table derived the page — but nothing
    # about their cost survived the process.
    assert not {"render_page", "measure"} & {timing.node_name for timing in timings.nodes}
    assert {step.workflow_id for step in timings.steps} == {"sweep:doc-1"}


async def test_a_recipe_runner_holding_the_run_home_records_inner_steps_under_the_host_run(tmp_path, home, ledger):
    """THE seam, stated: the fan-out's per-node cost, durable and attributed."""
    graph = ingest_graph(tmp_path, checkpointer=home)
    host = serve(graph, home=home, deployment_version="v1")
    await settled_ingest(host, graph)

    timings = await RunHomeReadModel(host.client).node_timings()

    by_name = {timing.node_name: timing for timing in timings.nodes}
    assert by_name["render_page"].executions == 1
    assert by_name["measure"].executions == 1
    assert all(timing.average_ms is not None for timing in (by_name["render_page"], by_name["measure"]))

    inner = [step for step in timings.steps if step.node_name in {"render_page", "measure"}]
    # Attributed to the Host Run that drove them, and to its manifest item —
    # the join a product cannot make from `host_submissions` alone, because
    # an inner run has no submission of its own.
    assert {step.root_workflow_id for step in inner} == {"sweep:doc-1"}
    assert {step.item_key for step in inner} == {"doc-1"}
    assert {step.workflow_id for step in inner} != {"sweep:doc-1"}

    # And the Host Run's own totals stay the Host Run's: the inner nodes are
    # not counted into the parent's node_count.
    assert timings.runs[0].workflow_id == "sweep:doc-1"
    assert timings.runs[0].node_count == 4


async def test_an_inner_run_carries_no_address_today(tmp_path, home, ledger):
    """The unshipped half of #386, pinned so it cannot change silently.

    ``RunGraph`` yields a graph and its inputs — never which row, column, or
    branch it derives — so the driver has nothing to derive a workflow id
    from and the runner generates one. Landing the addressed-effect design
    should make this test FAIL and be rewritten to assert the address.
    """
    graph = ingest_graph(tmp_path, checkpointer=home)
    host = serve(graph, home=home, deployment_version="v1")
    await settled_ingest(host, graph)

    inner_runs = [run for run in home.runs(limit=None) if run.parent_run_id == "sweep:doc-1"]

    assert [run.graph_name for run in inner_runs] == ["page_recipe"]
    # A GraphNode child is `<parent>/<node>`; a recipe run is a generated id.
    assert not inner_runs[0].id.startswith("sweep:doc-1")
