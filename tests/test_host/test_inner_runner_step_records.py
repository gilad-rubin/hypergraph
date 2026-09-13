"""Issue #356 — what a table fan-out's inner runner records, and what it does not.

The gap panda hit: a `HyperTable` node running its derivation recipe per page
inside a Host-executed run, whose per-node stats vanished when the notebook
kernel died. This suite pins BOTH halves of the honest answer.

**The recording lands.** A recipe run is a nested run in everything but
delegation — the table drives it with its OWN runner, so the enclosing
runner's checkpointer, which a `GraphNode` child inherits for free, never
reached it. Now it does: inside a durable Run the recipe records to the
same Run Home the outer graph does, one `runs` row per recipe run parented
to the Host Run, with its step records — and `node_timings` folds them,
because it walks `runs.parent_run_id`. Outside a durable Run nothing
changes: a table a notebook drives itself still records nothing.

**The identity does not.** The inner run's workflow id is generated, not
derived from the row it is deriving: `RunGraph` — the single effect a write
plan yields — carries the inputs but no address. So a re-executed materialize
node mints a fresh inner run rather than addressing the same one. That is why
the rest of the design in issue #386 (address the effect, then gate recording
behind an explicit `record_steps` tier) is written down rather than shipped: a
wrong address is not a migration away, it is a re-ingest away.

`test_an_inner_run_carries_no_address_today` is deliberately written so that
landing that design BREAKS it — an inner run that gains a derived address
should fail loudly, not pass quietly.
"""

from __future__ import annotations

import pytest

from hypergraph import AsyncRunner, Graph, RunHome, RunHomeClient, RunHomeReadModel, SyncRunner, node, serve
from hypergraph.materialization._lancedb_store import LanceDBStore
from tests.test_host._batch_interrupt import submit_ids, worker

pytest.importorskip("aiosqlite")

RECIPE_NODES = {"render_page", "measure"}
TABLE_DEFAULT = object()
"""Sentinel: build the table with no ``runner=`` at all, like most products do."""


def ingest_graph(tmp_path, *, table_runner=TABLE_DEFAULT, outer_runner=None, boom: bool = False) -> Graph:
    """A document graph whose middle node is a HyperTable page fan-out."""

    @node(output_name="text")
    def render_page(page: str) -> str:
        if boom:
            raise RuntimeError(f"page unreadable: {page}")
        return f"rendered-{page}"

    @node(output_name="length")
    def measure(text: str) -> int:
        return len(text)

    table_options = {} if table_runner is TABLE_DEFAULT else {"runner": table_runner}
    table = Graph([render_page, measure], name="page_recipe").as_table(
        identity="page_id",
        store=LanceDBStore(str(tmp_path / "table")),
        **table_options,
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
    return Graph([stage, pick_page, materialize, publish], name="ingest_document").with_runner(outer_runner or AsyncRunner())


async def settled_ingest(host, graph, workflow_id: str = "sweep"):
    receipt = await submit_ids(host, graph, ["doc-1"], workflow_id)
    async with worker(host):
        await host.client.follow(receipt.batch_ref, deadline=20)
    return receipt


def assert_fan_out_recorded(timings, *, workflow_id: str = "sweep:doc-1", item_key: str = "doc-1") -> None:
    """The fan-out's per-node cost, durable and attributed to its Host Run."""
    by_name = {timing.node_name: timing for timing in timings.nodes}
    assert by_name["render_page"].executions == 1
    assert by_name["measure"].executions == 1
    assert all(timing.average_ms is not None for timing in (by_name["render_page"], by_name["measure"]))

    inner = [step for step in timings.steps if step.node_name in RECIPE_NODES]
    # Attributed to the Host Run that drove them, and to its manifest item —
    # the join a product cannot make from `host_submissions` alone, because
    # an inner run has no submission of its own.
    assert {step.root_workflow_id for step in inner} == {workflow_id}
    assert {step.item_key for step in inner} == {item_key}
    assert {step.workflow_id for step in inner} != {workflow_id}

    # And the Host Run's own totals stay the Host Run's: the inner nodes are
    # not counted into the parent's node_count.
    host_run = next(run for run in timings.runs if run.workflow_id == workflow_id)
    assert host_run.node_count == 4


async def test_a_recipe_runner_without_a_checkpointer_inherits_the_host_run_home(tmp_path, home, ledger):
    """THE gap, closed: the table's own runner offers the Home nothing, and
    the widest, most expensive part of the ingestion is recorded anyway."""
    graph = ingest_graph(tmp_path, table_runner=AsyncRunner())
    host = serve(graph, home=home, deployment_version="v1")
    await settled_ingest(host, graph)

    timings = await RunHomeReadModel(host.client).node_timings()

    assert {timing.node_name for timing in timings.nodes} == {"stage", "pick_page", "materialize_pages", "publish", *RECIPE_NODES}
    assert_fan_out_recorded(timings)


async def test_the_default_table_runner_is_recorded_too(tmp_path, home, ledger):
    """`as_table()` without a `runner=` is the shape most products write.

    Its default is a SyncRunner, driven off the event loop by the async
    materialization executor — so this is also the harder case: the Run
    Home is written from a worker thread while the loop keeps running.
    """
    graph = ingest_graph(tmp_path)
    host = serve(graph, home=home, deployment_version="v1")
    await settled_ingest(host, graph)

    timings = await RunHomeReadModel(host.client).node_timings()

    assert_fan_out_recorded(timings)


async def test_a_sync_host_run_records_its_fan_out_the_same_way(tmp_path, home, ledger):
    """Sync/async parity: the outer graph carries a SyncRunner this time."""
    graph = ingest_graph(tmp_path, table_runner=SyncRunner(), outer_runner=SyncRunner())
    host = serve(graph, home=home, deployment_version="v1")
    await settled_ingest(host, graph)

    timings = await RunHomeReadModel(host.client).node_timings()

    assert_fan_out_recorded(timings)


async def test_a_recipe_runner_holding_the_run_home_records_inner_steps_under_the_host_run(tmp_path, home, ledger):
    """An explicit `checkpointer=` still wins: the product said where these go."""
    graph = ingest_graph(tmp_path, table_runner=AsyncRunner(checkpointer=home))
    host = serve(graph, home=home, deployment_version="v1")
    await settled_ingest(host, graph)

    timings = await RunHomeReadModel(host.client).node_timings()

    assert_fan_out_recorded(timings)


async def test_a_table_driven_outside_a_durable_run_still_records_nothing(tmp_path, home, ledger):
    """The scope boundary: inheritance belongs to the Run, not to the table.

    The same table object, derived by hand in this process, writes no Run
    Home row at all — a notebook exploring a recipe is not durable work
    and must not start committing runs because a host exists somewhere.
    """
    graph = ingest_graph(tmp_path, table_runner=AsyncRunner())
    serve(graph, home=home, deployment_version="v1")
    table = graph.nodes["materialize_pages"].table

    await table.insert(page_id="loose-1", page="loose-1-p1")

    assert home.runs(limit=None) == []


async def test_an_unreadable_page_reports_its_error_rather_than_vanishing(tmp_path, home, ledger):
    """The failure path: a recipe node that raises is durable evidence now.

    Before, a page that could not be rendered left the same trace as a page
    that was never attempted — none. The honest status is that `render_page`
    ran once and errored once, under the Host Run that drove it.
    """
    graph = ingest_graph(tmp_path, table_runner=AsyncRunner(), boom=True)
    host = serve(graph, home=home, deployment_version="v1")
    await settled_ingest(host, graph)

    timings = await RunHomeReadModel(host.client).node_timings()

    by_name = {timing.node_name: timing for timing in timings.nodes}
    assert (by_name["render_page"].executions, by_name["render_page"].errors) == (1, 1)
    assert "measure" not in by_name  # never reached: nothing to report
    failed = [step for step in timings.steps if step.node_name == "render_page"]
    assert [step.error for step in failed] != [None]
    assert {step.root_workflow_id for step in failed} == {"sweep:doc-1"}


async def test_the_fan_out_s_timings_survive_the_process_that_wrote_them(tmp_path, home, ledger):
    """The restart contract: close the Home, reopen it, read the same facts.

    A fresh `RunHome` + `RunHomeClient` over the same file — no graph code,
    no table, no runner — is what an operator's second process actually
    holds. The numbers it reads are the numbers the sweep committed.
    """
    graph = ingest_graph(tmp_path, table_runner=AsyncRunner())
    host = serve(graph, home=home, deployment_version="v1")
    await settled_ingest(host, graph)
    read = RunHomeReadModel(host.client)
    before = await read.node_timings()
    uri = home.uri

    await home.close()

    reopened = RunHome.open(uri)
    try:
        after = await RunHomeReadModel(RunHomeClient(reopened)).node_timings()
    finally:
        await reopened.close()

    assert after.to_dict() == before.to_dict()
    assert_fan_out_recorded(after)
    assert len([step for step in after.steps if step.node_name in RECIPE_NODES]) == 2


async def test_a_second_worker_records_its_own_fan_out_into_the_same_home(tmp_path, home, ledger):
    """The restart contract, the other way round: the WORKER goes away.

    The first host settles one document and its Run Home is closed. A second
    host opens the same file and serves the same Definition — a new process,
    as far as the database is concerned — and drives a second document. Both
    sweeps' recipe nodes are in the one aggregate, each under its own Host
    Run, and the first sweep's numbers did not move.
    """
    graph = ingest_graph(tmp_path, table_runner=AsyncRunner())
    first = serve(graph, home=home, deployment_version="v1")
    await settled_ingest(first, graph, "sweep")
    before = await RunHomeReadModel(first.client).node_timings()
    uri = home.uri

    await home.close()

    restarted = RunHome.open(uri)
    try:
        second = serve(graph, home=restarted, deployment_version="v1")
        receipt = await submit_ids(second, graph, ["doc-2"], "sweep-restarted")
        async with worker(second, worker_id="w-restarted"):
            await second.client.follow(receipt.batch_ref, deadline=20)
        read = RunHomeReadModel(second.client)
        restarted_sweep = await read.node_timings(batch=receipt.batch_ref)
        both = await read.node_timings()
    finally:
        await restarted.close()

    assert_fan_out_recorded(restarted_sweep, workflow_id="sweep-restarted:doc-2", item_key="doc-2")
    assert {step.root_workflow_id for step in both.steps if step.node_name in RECIPE_NODES} == {"sweep:doc-1", "sweep-restarted:doc-2"}
    # The first process's facts are untouched by the second's.
    kept = [step for step in both.steps if step.root_workflow_id == "sweep:doc-1"]
    assert [step.to_dict() for step in kept] == [step.to_dict() for step in before.steps]


async def test_an_inner_run_carries_no_address_today(tmp_path, home, ledger):
    """The unshipped half of #386, pinned so it cannot change silently.

    ``RunGraph`` yields a graph and its inputs — never which row, column, or
    branch it derives — so the driver has nothing to derive a workflow id
    from and the runner generates one. Landing the addressed-effect design
    should make this test FAIL and be rewritten to assert the address.
    """
    graph = ingest_graph(tmp_path, table_runner=AsyncRunner())
    host = serve(graph, home=home, deployment_version="v1")
    await settled_ingest(host, graph)

    inner_runs = [run for run in home.runs(limit=None) if run.parent_run_id == "sweep:doc-1"]

    assert [run.graph_name for run in inner_runs] == ["page_recipe"]
    # A GraphNode child is `<parent>/<node>`; a recipe run is a generated id.
    assert not inner_runs[0].id.startswith("sweep:doc-1")
